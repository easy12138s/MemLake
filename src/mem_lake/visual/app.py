"""可视化控制台应用工厂：路由装配与会话状态初始化。"""

import secrets

from starlette.applications import Starlette
from starlette.routing import Route

from mem_lake.config import Settings, get_settings
from mem_lake.visual.api import login, logout, me
from mem_lake.visual.auth import LoginGuard


def create_visual_app(settings: Settings | None = None) -> Starlette:
    """构建可视化控制台 Starlette 应用。

    settings 缺省取 get_settings()；会话密钥未配置时每次进程启动随机生成
    （重启后需重新登录，观测台可接受）。
    """
    settings = settings or get_settings()
    app = Starlette(
        routes=[
            Route("/api/login", login, methods=["POST"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/me", me, methods=["GET"]),
        ]
    )
    app.state.visual_settings = settings
    app.state.visual_secret = settings.VISUAL_SESSION_SECRET or secrets.token_hex(16)
    app.state.visual_guard = LoginGuard()
    return app
