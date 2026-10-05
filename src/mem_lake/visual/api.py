"""可视化控制台只读 API 端点（login/logout/me；overview 见后续任务）。"""

from typing import cast

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from mem_lake.approval.repository import count_pending_batches
from mem_lake.auth.service import get_access_key_stats
from mem_lake.config import get_settings
from mem_lake.gateway.background_tasks import get_task_status_counts
from mem_lake.gateway.dependencies import readonly_session
from mem_lake.knowledge.age_store import get_graph_store
from mem_lake.knowledge.graph_stats import get_graph_quality_report, get_graph_stats
from mem_lake.knowledge.repository import (
    count_nodes_by_status,
    count_system_mounts,
    list_systems,
)
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


async def embedding_health_ok() -> bool:
    """embedding 服务 /health 可达性探测（2s 超时，失败不抛）。"""
    settings = get_settings()
    url = f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}/health"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
            # resp.status_code 在 --follow-imports=skip 下被 mypy 视为 Any，cast 收敛为 bool
            return cast(bool, resp.status_code == 200)
    except httpx.HTTPError:
        return False


async def overview(request: Request) -> JSONResponse:
    """总览统计（只读聚合；会话保护）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    async with readonly_session() as session:
        graph = await get_graph_stats(session, get_graph_store())
        quality = await get_graph_quality_report(session, get_graph_store())
        nodes_by_status = await count_nodes_by_status(session)
        batches_pending = await count_pending_batches(session)
        keys = await get_access_key_stats(session)
        systems = await list_systems(session)
        mounts = await count_system_mounts(session)
        tasks = await get_task_status_counts(session)
    return JSONResponse(
        {
            "graph": {
                "nodes_by_type": graph["nodes_by_type"],
                "edges_by_type": graph["edges_by_type"],
            },
            "quality": quality,
            "nodes_by_status": nodes_by_status,
            "batches_pending": batches_pending,
            "keys": keys,
            "systems": {"systems": len(systems), "project_mounts": mounts},
            "reindex_tasks": tasks,
            "health": {
                "database": True,
                "embedding": await embedding_health_ok(),
            },
        }
    )
