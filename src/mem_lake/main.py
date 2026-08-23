"""应用入口：FastMCP 实例创建与 ASGI 应用暴露。

启动方式：
- 开发：uvicorn mem_lake.main:app --host 0.0.0.0 --port 8000 --reload
- 部署（Dockerfile.app CMD）：python -m mem_lake.main，单进程单 worker

多 worker 说明：限流（内存令牌桶）与后台任务（ACTIVE_TASKS 进程内集合）均为
单实例设计；如需 uvicorn --workers N 多进程部署，须先改造这两处为共享存储
（如 Redis），否则限流与任务防重入失效。
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
