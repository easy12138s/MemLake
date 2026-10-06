"""可视化控制台只读 API 端点（login/logout/me；overview 见后续任务）。"""

import uuid
from typing import Any, cast

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
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    count_nodes_by_status,
    count_system_mounts,
    get_node,
    get_nodes_by_ids,
    list_embedded_node_ids,
    list_graph_nodes,
    list_systems,
    node_has_embedding,
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


def _graph_params(request: Request) -> dict[str, Any]:
    """解析 /api/graph 查询参数；非法值抛 ValueError（handler 统一 400）。"""
    settings = request.app.state.visual_settings
    params: dict[str, Any] = {}
    for name in ("system_id", "project_id"):
        raw = request.query_params.get(name)
        if raw:
            try:
                params[name] = uuid.UUID(raw)
            except ValueError as e:
                raise ValueError(f"非法 {name}") from e
    types_raw = request.query_params.get("types", "")
    if types_raw:
        params["node_types"] = tuple(
            t.strip() for t in types_raw.split(",") if t.strip()
        )
    status_raw = request.query_params.get("status", "approved")
    if status_raw not in ("approved", "archived", "all"):
        raise ValueError("非法 status，合法值: approved/archived/all")
    params["status"] = None if status_raw == "all" else status_raw
    q = (request.query_params.get("q") or "").strip()
    if q:
        params["q"] = q
    try:
        limit = int(request.query_params.get("limit", "500"))
    except ValueError as e:
        raise ValueError("非法 limit") from e
    params["limit"] = max(1, min(limit, settings.VISUAL_GRAPH_MAX_NODES))
    return params


def _graph_node_payload(node: KnowledgeNode, embedded: set[uuid.UUID]) -> dict[str, Any]:
    """图页节点载荷裁剪：列表展示字段 + 正文前 100 字 + 向量就绪标记。"""
    return {
        "id": str(node.id),
        "type": node.type,
        "title": node.title,
        "status": node.status,
        "system_id": str(node.system_id) if node.system_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "requirement_key": node.requirement_key,
        "content_preview": node.content[:100],
        "vector_ready": node.id in embedded,
    }


async def graph(request: Request) -> JSONResponse:
    """网图数据：nodes + edges + truncated（会话保护，只读）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    try:
        params = _graph_params(request)
        async with readonly_session() as session:
            rows, truncated = await list_graph_nodes(session, **params)
            ids = [row.id for row in rows]
            embedded: set[uuid.UUID] = (
                await list_embedded_node_ids(session, node_ids=ids)
                if ids
                else set()
            )
            edges = await get_graph_store().subgraph_edges(session, ids)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(
        {
            "nodes": [_graph_node_payload(row, embedded) for row in rows],
            "edges": edges,
            "truncated": truncated,
        }
    )


async def node_detail(request: Request) -> JSONResponse:
    """节点详情：完整属性 + 向量就绪 + 关联边 + 一跳邻居（会话保护，只读）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    try:
        node_id = uuid.UUID(request.path_params["node_id"])
    except ValueError:
        return JSONResponse({"error": "非法节点 ID"}, status_code=400)
    async with readonly_session() as session:
        try:
            node = await get_node(session, node_id)
        except NodeNotFoundError:
            return JSONResponse({"error": "节点不存在"}, status_code=404)
        store = get_graph_store()
        vector_ready = await node_has_embedding(session, node_id)
        raw_neighbors = await store.neighbors(session, node_id, depth=1)
        nb_ids: list[uuid.UUID] = []
        for n in raw_neighbors:
            raw_id = (n.get("properties") or {}).get("id")
            try:
                nb_ids.append(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError):
                continue
        # 邻居信息以 PG 为真相源（图投影非真相源）：批量回查补 status/title
        rows = await get_nodes_by_ids(
            session, node_ids=nb_ids, status=None, include_deleted=True
        )
        neighbors = [
            {
                "id": str(row.id),
                "type": row.type,
                "title": row.title,
                "status": row.status,
                "system_id": str(row.system_id) if row.system_id else None,
                "project_id": str(row.project_id) if row.project_id else None,
            }
            for row in rows
        ]
        # 关联边 = {node} ∪ 邻居 子集中触及本节点的边
        all_edges = await store.subgraph_edges(session, [node_id, *nb_ids])
        node_id_str = str(node_id)
        edges = [
            e
            for e in all_edges
            if e["source"] == node_id_str or e["target"] == node_id_str
        ]
    return JSONResponse(
        {
            "id": str(node.id),
            "type": node.type,
            "title": node.title,
            "status": node.status,
            "requirement_key": node.requirement_key,
            "system_id": str(node.system_id) if node.system_id else None,
            "project_id": str(node.project_id) if node.project_id else None,
            "content": node.content,
            "properties": node.properties,
            "tags": node.tags,
            "version": node.version,
            "created_by": node.created_by,
            "created_at": node.created_at.isoformat(),
            "vector_ready": vector_ready,
            "edges": edges,
            "neighbors": neighbors,
        }
    )


async def systems(request: Request) -> JSONResponse:
    """system 域清单（图页过滤下拉用；批次四扩展挂载项目与绑定 Key 概览）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    async with readonly_session() as session:
        rows = await list_systems(session)
    return JSONResponse(
        {
            "systems": [
                {"id": str(s.id), "name": s.name, "description": s.description}
                for s in rows
            ]
        }
    )
