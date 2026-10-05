"""可视化控制台只读 API 端点（login/logout/me；overview 见后续任务）。"""

from starlette.requests import Request
from starlette.responses import JSONResponse

from mem_lake.visual.auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    check_credentials,
    create_session_token,
    verify_session_token,
)


def session_user(request: Request) -> str | None:
    """从请求 Cookie 解析已登录用户名；未认证返回 None（各端点共用）。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_session_token(token, request.app.state.visual_secret)


async def login(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.visual_settings
    ip = request.client.host if request.client else "unknown"
    if state.visual_guard.is_locked(ip):
        return JSONResponse({"error": "失败次数过多，请 5 分钟后重试"}, status_code=429)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    if not check_credentials(
        username,
        password,
        expected_user=settings.VISUAL_USERNAME,
        expected_pass=settings.VISUAL_PASSWORD,
    ):
        state.visual_guard.register_failure(ip)
        return JSONResponse({"error": "账号或口令错误"}, status_code=401)
    state.visual_guard.reset(ip)
    token = create_session_token(settings.VISUAL_USERNAME, state.visual_secret)
    resp = JSONResponse({"username": settings.VISUAL_USERNAME})
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


async def logout(request: Request) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


async def me(request: Request) -> JSONResponse:
    username = session_user(request)
    if username is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    return JSONResponse({"username": username})
