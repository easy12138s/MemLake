"""Access Key 认证辅助：协议版本校验 + 头提取函数。

对齐 PDD 3.1 认证机制：X-MCP-Key 头传递 Access Key，由 AccessKeyAuthMiddleware
在 on_request hook 中异步查 DB + bcrypt 校验，构造 AccessToken 并设置到
request.scope["user"]，使 fastmcp.server.dependencies.get_access_token() 正常工作。

不使用 FastMCP 的 TokenVerifier + auth= 参数链路，因为 BearerAuthBackend 硬编码
读取 Authorization: Bearer 头，不支持 X-MCP-Key。

协议版本校验（PDD 3.1）：
MCP-Protocol-Version 头与正文 _meta.protocolVersion 不一致返回错误码 -32020。
-32020 为项目自定义错误码（落在 -32000~-32099 保留段合法，MCP 官方未定义此码）。
"""

from fastmcp.exceptions import McpError

# Access Key 头名（PDD 3.1 / 6.2）
ACCESS_KEY_HEADER = "X-MCP-Key"

# 协议版本头名（PDD 3.1 / 6.2）
PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"

# 支持的协议版本（单值硬校验：互操作性收紧策略——客户端 initialize 的
# protocolVersion 必须精确匹配本服务实现与测试过的版本，避免未验证的
# 协议行为差异引入隐性兼容问题；MCP 发布新版本时需升级并回归后更新此值）
SUPPORTED_PROTOCOL_VERSION = "2026-07-28"

# 协议版本不一致错误码（PDD 3.1，落在 -32000~-32099 保留段）
PROTOCOL_VERSION_MISMATCH_ERROR_CODE = -32020


def validate_protocol_version(
    headers: dict[str, str], message_meta: dict | None
) -> None:
    """校验 MCP-Protocol-Version 头与正文 _meta.protocolVersion 一致性。

    PDD 3.1：服务端校验 Header 与正文一致性，不一致返回 HTTP 400 与错误码 -32020。

    参数：
        headers: HTTP 请求头字典（小写键）
        message_meta: 请求正文 _meta 字段（含 protocolVersion/clientInfo 等）

    抛出 McpError(code=-32020) 如果：
    - Header 缺失 MCP-Protocol-Version
    - 正文 _meta 缺失 protocolVersion
    - 两者值不一致
    """
    header_version = headers.get(PROTOCOL_VERSION_HEADER.lower())
    if not header_version:
        raise McpError(
            code=PROTOCOL_VERSION_MISMATCH_ERROR_CODE,
            message=f"缺少 {PROTOCOL_VERSION_HEADER} 请求头",
        )

    if message_meta is None:
        raise McpError(
            code=PROTOCOL_VERSION_MISMATCH_ERROR_CODE,
            message="请求正文缺少 _meta 字段",
        )

    meta_version = message_meta.get("protocolVersion")
    if not meta_version:
        raise McpError(
            code=PROTOCOL_VERSION_MISMATCH_ERROR_CODE,
            message="请求正文 _meta 缺少 protocolVersion 字段",
        )

    if header_version != meta_version:
        raise McpError(
            code=PROTOCOL_VERSION_MISMATCH_ERROR_CODE,
            message=f"协议版本不一致：Header={header_version}, _meta={meta_version}",
        )

    if header_version != SUPPORTED_PROTOCOL_VERSION:
        raise McpError(
            code=PROTOCOL_VERSION_MISMATCH_ERROR_CODE,
            message=f"不支持的协议版本：{header_version}，当前支持：{SUPPORTED_PROTOCOL_VERSION}",
        )


def extract_access_key_from_headers(headers: dict[str, str]) -> str | None:
    """从 HTTP 请求头提取 Access Key 明文。

    PDD 3.1 / 6.2：X-MCP-Key 头传递 Access Key。
    返回 None 表示未提供（由调用方决定是否拒绝）。
    """
    return headers.get(ACCESS_KEY_HEADER.lower())
