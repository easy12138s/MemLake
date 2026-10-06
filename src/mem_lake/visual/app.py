"""可视化控制台应用工厂：路由装配、会话状态初始化、监控采样器生命周期。"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mem_lake.config import Settings, get_settings
from mem_lake.visual.api import graph, graph_tree, keys, login, logout, me, node_detail, overview, systems
from mem_lake.visual.auth import LoginGuard
from mem_lake.visual.monitor import MetricsSampler
from mem_lake.visual.monitor_api import monitor_series, monitor_snapshot, monitor_tail

_STATIC_DIR = Path(__file__).parent / "static"


async def _index(request: Request) -> FileResponse:
    """控制台 shell 页面。"""
    return FileResponse(_STATIC_DIR / "index.html")


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """监控采样器随应用启停（uvicorn 默认运行 lifespan）。

    采样循环单轮异常自愈（monitor.MetricsSampler._run 内捕获），
    此处只负责启动与取消，不吞采样器自身的致命错误。
    """
    sampler = MetricsSampler()
    app.state.visual_sampler = sampler
    sampler.start()
    try:
        yield
    finally:
        await sampler.stop()


def create_visual_app(settings: Settings | None = None) -> Starlette:
    """构建可视化控制台 Starlette 应用。

    settings 缺省取 get_settings()；会话密钥未配置时每次进程启动随机生成
    （重启后需重新登录，观测台可接受）。
    """
    settings = settings or get_settings()
    app = Starlette(
        routes=[
            Route("/", _index, methods=["GET"]),
            Route("/api/login", login, methods=["POST"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/me", me, methods=["GET"]),
            Route("/api/overview", overview, methods=["GET"]),
            Route("/api/graph", graph, methods=["GET"]),
            Route("/api/graph/tree", graph_tree, methods=["GET"]),
            Route("/api/node/{node_id}", node_detail, methods=["GET"]),
            Route("/api/systems", systems, methods=["GET"]),
            Route("/api/keys", keys, methods=["GET"]),
            Route("/api/monitor/snapshot", monitor_snapshot, methods=["GET"]),
            Route("/api/monitor/series", monitor_series, methods=["GET"]),
            Route("/api/monitor/tail", monitor_tail, methods=["GET"]),
            Mount("/static", StaticFiles(directory=_STATIC_DIR), name="static"),
        ],
        lifespan=_lifespan,
    )
    app.state.visual_settings = settings
    app.state.visual_secret = settings.VISUAL_SESSION_SECRET or secrets.token_hex(16)
    app.state.visual_guard = LoginGuard()
    return app
