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
from uuid import uuid4

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import (
    get_access_token,
    get_http_headers,
    get_http_request,
)
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from mem_lake.auth.rbac import ROLE_TOOLSET, has_tool_access
from mem_lake.auth.service import authenticate_access_key
from mem_lake.config import get_settings
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.gateway.auth import (
    extract_access_key_from_headers,
    validate_protocol_version,
)
from mem_lake.observability.logging import (
    bind_request_context,
    clear_request_context,
)
from mem_lake.observability.metrics import MCP_TOOL_CALLS, MCP_TOOL_DURATION

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

    设计权衡（AUDIT §2.8）：每个请求执行一次 DB 查询 + bcrypt 校验（约
    200-300ms CPU），不设缓存——这是有意取舍：吊销/轮换 Access Key 后立即
    生效，不引入缓存失效窗口。当前单实例部署的吞吐上限受此约束（远低于
    MCP_RATE_LIMIT_QPS 标称值）；若未来需要高吞吐，可评估带短 TTL 的
    认证结果缓存（key_id + hash 前缀维度，吊销延迟可控）。

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
    on_list_tools hook：按角色过滤工具列表（最小权限），仅返回该角色
        有权调用的工具；未认证或无角色返回空列表，避免跨角色泄露工具形态。
    """

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> list:
        """列举工具时按角色过滤，避免非 admin 角色看到 admin 专属工具。"""
        tools = await call_next(context)
        access_token = get_access_token()
        if access_token is None:
            return []  # 未认证：不泄露任何工具
        role = access_token.claims.get("role")
        if not role:
            return []
        allowed = ROLE_TOOLSET.get(role, frozenset())
        return [t for t in tools if t.name in allowed]

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

    内存治理（AUDIT §2.9）：桶只在该 key 被访问时创建；除惰性归零删除外，
    采用两条主动防线限制 `_buckets` 无界驻留：
    1. 定期清扫：每 `_prune_interval` 次调用全表清理一次所有过期桶（避免
       RATE_LIMIT_WINDOW_SEC 内访问过一次、此后长期不活跃的 key 桶永久驻留）。
    2. 容量上限：`_max_buckets` 之内、且清扫后仍超限时触发一次清理再评估，
       仍超限则逐出最不活跃（最小窗口左边界）的桶，防止任意 key 数量打满内存。
    """

    def __init__(self, qps: int | None = None) -> None:
        settings = get_settings()
        self._qps = qps or settings.MCP_RATE_LIMIT_QPS
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        # 距上次全表清扫的调用计数与清扫阈值
        self._calls_since_prune = 0
        self._prune_interval = 1000
        # 桶容量上限：当前 qps=100 单实例下，任意活跃 key 数远超该值极不可达
        self._max_buckets = 10000

    def _prune(self, now: float) -> None:
        """全表清扫：删除所有过期/空桶，返回清理后的条数。"""
        expired_before = len(self._buckets)
        for key in list(self._buckets):
            bucket = self._buckets[key]
            while bucket and bucket[0] < now - RATE_LIMIT_WINDOW_SEC:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]
        if expired_before and len(self._buckets) < expired_before:
            logger.debug(
                "限流全表清扫：%d -> %d 桶", expired_before, len(self._buckets)
            )

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        access_token = get_access_token()
        if access_token is None:
            # 未认证请求不限流（由 auth 层拒绝）
            return await call_next(context)

        # 限流 key：Access Key ID
        client_key = access_token.client_id or "anonymous"
        now = time.time()
        bucket = self._buckets.get(client_key)

        # 定期全表清扫，覆盖「访问一次后长期停用」的不活跃 key 桶
        self._calls_since_prune += 1
        if self._calls_since_prune >= self._prune_interval:
            self._calls_since_prune = 0
            self._prune(now)

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

        # 记录本次请求（按需创建桶）；超容量上限时先清理再评估，仍超限则逐出最不活跃桶
        if bucket is None:
            if len(self._buckets) >= self._max_buckets:
                self._prune(now)
            if len(self._buckets) >= self._max_buckets:
                stale_key = min(
                    self._buckets,
                    key=lambda k: self._buckets[k][0] if self._buckets[k] else now,
                )
                del self._buckets[stale_key]
                logger.warning("限流桶达容量上限，已逐出最不活跃桶 %s", stale_key[:8])
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

        # 绑定请求上下文（结构化日志贯通字段），finally 清理
        project_id = arguments.get("project_id")
        bind_request_context(
            request_id=uuid4().hex[:12],
            operation_id=operation_id,
            actor=actor,
            project_id=str(project_id) if project_id else None,
        )

        try:
            result = await call_next(context)
            return result
        except Exception as e:
            result_status = "error"
            error_message = str(e)
            raise
        finally:
            duration_ms = (time.time() - t0) * 1000
            # Prometheus 上报（复用 t0 计时，不新增测量）
            MCP_TOOL_CALLS.labels(tool=tool_name, status=result_status).inc()
            MCP_TOOL_DURATION.labels(tool=tool_name).observe(time.time() - t0)

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
            finally:
                # 清理请求上下文绑定，防止跨请求泄漏到下一调用
                clear_request_context()
