"""网关中间件：Access Key 认证 + RBAC 鉴权 + 限流 + 审计。

对齐 PDD 3.1 限流与审计要求。FastMCP 4.0 Middleware 基类提供 hook 机制，
本模块实现 4 个中间件，按注册顺序执行：

1. AccessKeyAuthMiddleware（on_request）：提取 X-MCP-Key → 异步查 DB 认证 →
   构造 AccessToken 并设置到 request.scope["user"]，使 get_access_token() 正常工作
2. RBACMiddleware（on_call_tool）：校验当前角色是否有权调用该工具
3. RateLimitMiddleware（on_call_tool）：令牌桶限流，per Access Key，默认 100 QPS
4. AuditLogMiddleware（on_call_tool）：记录工具调用审计日志

写操作幂等由 DB 层唯一实现（approval/service.py _find_by_idempotency_key，
持久化、跨重启，无 600s 时间窗限制），不在中间件层重复。

中间件拒绝调用必须 raise ToolError（FastMCP 规范），不要返回 ToolResult。
"""

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import (
    get_access_token,
    get_http_headers,
    get_http_request,
)
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from mem_lake.auth.rbac import has_tool_access
from mem_lake.auth.service import authenticate_access_key
from mem_lake.config import get_settings
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.gateway.auth import (
    ACCESS_KEY_HEADER,
    extract_access_key_from_headers,
    validate_protocol_version,
)

logger = logging.getLogger("mem_lake.gateway.middleware")

# 限流时间窗口（秒），与 QPS 配合计算桶容量
RATE_LIMIT_WINDOW_SEC = 1


class AccessKeyAuthMiddleware(Middleware):
    """Access Key 认证中间件。

    on_request hook：提取 X-MCP-Key 头 → 异步查 DB 认证 → 构造 AccessToken
    并设置到 request.scope["user"] = AuthenticatedUser(access_token)。

    fastmcp.server.dependencies.get_access_token() 从 request.scope["user"]
    读取 AccessToken（优先级高于 SDK context var），因此本中间件直接设置
    scope["user"] 即可让 get_access_token() 正常工作。

    不使用 FastMCP 的 auth= 参数 + TokenVerifier 链路，因为 BearerAuthBackend
    硬编码读取 Authorization: Bearer 头，不支持 X-MCP-Key。

    协议版本校验（PDD 3.1）：MCP-Protocol-Version 头与 _meta.protocolVersion
    不一致 raise McpError(code=-32020)。
    """

    async def on_request(self, context: MiddlewareContext, call_next):
        """提取 X-MCP-Key → 异步认证 → 设置 request.scope["user"]。"""
        headers = get_http_headers()

        # 协议版本校验（PDD 3.1）
        message = context.message
        message_meta = getattr(message, "_meta", None) if message else None
        if isinstance(message_meta, dict):
            validate_protocol_version(headers, message_meta)

        # 提取 Access Key
        access_key_plain = extract_access_key_from_headers(headers)
        if not access_key_plain:
            # 未提供 Access Key，不设置 scope["user"]，由 RBACMiddleware 拒绝
            return await call_next(context)

        # 异步查 DB 认证
        try:
            async with AsyncSessionLocal() as session:
                auth_result = await authenticate_access_key(session, access_key_plain)
        except Exception:
            logger.exception("Access Key 认证查询失败")
            return await call_next(context)

        if auth_result is None:
            # 认证失败，不设置 scope["user"]，由 RBACMiddleware 拒绝
            return await call_next(context)

        # 构造 AccessToken
        access_token = AccessToken(
            token=access_key_plain,
            client_id=str(auth_result["key_id"]),
            scopes=[auth_result["role"]],
            claims={
                "role": auth_result["role"],
                "key_id": str(auth_result["key_id"]),
                "project_scope": auth_result["project_scope"],
            },
        )

        # 设置到 request.scope["user"]，使 get_access_token() 正常工作
        # get_access_token() 优先从 request.scope["user"] 读取（AuthenticatedUser）
        try:
            request = get_http_request()
            request.scope["user"] = AuthenticatedUser(access_token)
        except RuntimeError:
            # 非 HTTP 传输（如 stdio），无法设置 scope["user"]
            # 这种场景下 get_access_token() 会回退到 SDK context var（本场景不适用）
            logger.warning("无法获取 HTTP request，跳过 scope['user'] 设置")

        return await call_next(context)


class RBACMiddleware(Middleware):
    """RBAC 鉴权中间件。

    on_call_tool hook：从 AccessToken.claims 提取角色，校验是否有权调用该工具。
    无权调用 raise ToolError("Access denied")。
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name
        access_token = get_access_token()

        if access_token is None:
            raise ToolError("未认证：缺少有效的 Access Key")

        role = access_token.claims.get("role")
        if not role:
            raise ToolError("认证信息缺少角色 claims")

        if not has_tool_access(role, tool_name):
            raise ToolError(
                f"权限拒绝：角色 '{role}' 无权调用工具 '{tool_name}'"
            )

        return await call_next(context)


class RateLimitMiddleware(Middleware):
    """令牌桶限流中间件。

    on_call_tool hook：per Access Key 限流，QPS 从 config.MCP_RATE_LIMIT_QPS 读取。
    超限 raise ToolError("Rate limit exceeded")。

    实现：滑动窗口 + deque，窗口大小 1 秒，最大请求数 = QPS。
    单实例内存限流为既定设计（docker compose 单实例部署，PDD 既定范围）。
    """

    def __init__(self, qps: int | None = None) -> None:
        settings = get_settings()
        self._qps = qps or settings.MCP_RATE_LIMIT_QPS
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        access_token = get_access_token()
        if access_token is None:
            # 未认证请求不限流（由 auth 层拒绝）
            return await call_next(context)

        # 限流 key：Access Key ID
        client_key = access_token.client_id or "anonymous"
        now = time.time()
        bucket = self._buckets.get(client_key)

        # 清理窗口外的请求记录；空桶删除 key，防止 dict 长期驻留
        if bucket is not None:
            while bucket and bucket[0] < now - RATE_LIMIT_WINDOW_SEC:
                bucket.popleft()
            if not bucket:
                del self._buckets[client_key]
                bucket = None

        if bucket is not None and len(bucket) >= self._qps:
            raise ToolError(
                f"限流：超过 {self._qps} QPS 限制（Access Key: {client_key[:8]}...）"
            )

        # 记录本次请求（按需创建桶）
        if bucket is None:
            bucket = self._buckets[client_key] = deque()
        bucket.append(now)
        return await call_next(context)


class AuditLogMiddleware(Middleware):
    """审计日志中间件。

    on_call_tool hook：记录工具调用审计日志（actor/tool/args/duration/result_status）。
    调用 audit.service.write_audit_log 写入 DB（独立 session，不影响工具事务）。

    PDD 3.1：所有写操作记录审计日志（操作人、时间、目标、结果、operation_id）。
    本中间件记录所有工具调用（含读操作），写操作的审计由 service 层补充业务细节。
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        tool_name = message.name
        arguments = getattr(message, "arguments", None) or {}
        operation_id = arguments.get("operation_id")

        access_token = get_access_token()
        actor = (
            access_token.claims.get("key_id", "anonymous")
            if access_token
            else "anonymous"
        )

        t0 = time.time()
        result_status = "success"
        error_message = None

        try:
            result = await call_next(context)
            return result
        except Exception as e:
            result_status = "error"
            error_message = str(e)
            raise
        finally:
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "TOOL_CALL actor=%s tool=%s status=%s duration=%.0fms op_id=%s",
                actor,
                tool_name,
                result_status,
                duration_ms,
                operation_id,
            )

            # 写审计日志（独立 session，不影响工具事务）
            try:
                async with AsyncSessionLocal() as session:
                    from mem_lake.audit.service import write_audit_log

                    await write_audit_log(
                        session,
                        actor=actor,
                        action="tool_call",
                        target_type="tool",
                        detail={
                            "tool_name": tool_name,
                            "operation_id": operation_id,
                            "result_status": result_status,
                            "error_message": error_message,
                            "duration_ms": round(duration_ms, 2),
                        },
                    )
                    await session.commit()
            except Exception:
                # 审计日志写入失败不影响工具调用结果
                logger.exception("审计日志写入失败")
