"""应用入口：FastMCP 实例创建与 ASGI 应用暴露。

启动方式：
- 开发：uvicorn mem_lake.main:app --host 0.0.0.0 --port 8000 --reload
- 生产：uvicorn mem_lake.main:app --host 0.0.0.0 --port 8000 --workers 4

对齐 PDD 6.1：FastMCP 4.0 http_app() 返回 Starlette ASGI 应用，
监听 host:port（默认 0.0.0.0:8000），X-MCP-Key 头认证。
"""

import logging

from mem_lake.config import get_settings
from mem_lake.gateway import create_mcp_server

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mem_lake.main")

# 全局 FastMCP 实例（模块级单例，uvicorn 多 worker 时每 worker 一个）
mcp = create_mcp_server()

# ASGI 应用（uvicorn 加载入口）
app = mcp.http_app()

logger.info(
    "Mem Lake MCP 网关已初始化，监听 %s:%d",
    get_settings().MCP_SERVER_HOST,
    get_settings().MCP_SERVER_PORT,
)


if __name__ == "__main__":
    # 直接 python -m mem_lake.main 启动（开发用）
    settings = get_settings()
    mcp.run(
        transport="http",
        host=settings.MCP_SERVER_HOST,
        port=settings.MCP_SERVER_PORT,
    )
