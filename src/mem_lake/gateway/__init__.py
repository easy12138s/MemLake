"""MCP 网关层：协议适配、认证鉴权、工具注册。

对外暴露：
- create_mcp_server()：创建 FastMCP 实例（含中间件 + lifespan + 工具注册）
- app：ASGI 应用（uvicorn 启动入口）

对齐 PDD 6.1：FastMCP 4.0 实例，X-MCP-Key 认证 + RBAC 鉴权 + 限流 + 幂等 + 审计。
"""

from mem_lake.gateway.server import create_mcp_server

__all__ = ["create_mcp_server"]
