"""可视化控制台应用骨架：登录/会话/登出 API 测试（httpx ASGI 进程内）。"""
import httpx
from starlette.applications import Starlette

from mem_lake.config import Settings
from mem_lake.visual import api as visual_api
from mem_lake.visual import create_visual_app


def make_client(**overrides) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="test-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_me_requires_session():
    async with make_client() as client:
        r = await client.get("/api/me")
        assert r.status_code == 401


async def test_login_logout_flow():
    async with make_client() as client:
        # 错误口令
        r = await client.post(
            "/api/login", json={"username": "vu", "password": "wrong"}
        )
        assert r.status_code == 401
        # 正确口令 → 会话 Cookie 生效
        r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
        assert r.status_code == 200
        assert r.json()["username"] == "vu"
        r = await client.get("/api/me")
        assert r.status_code == 200
        assert r.json()["username"] == "vu"
        # 登出后失效
        r = await client.post("/api/logout")
        assert r.status_code == 200
        r = await client.get("/api/me")
        assert r.status_code == 401


async def test_login_lockout():
    async with make_client() as client:
        for _ in range(5):
            r = await client.post(
                "/api/login", json={"username": "vu", "password": "bad"}
            )
            assert r.status_code == 401
        # 第 6 次：正确口令也被锁定拒绝
        r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
        assert r.status_code == 429


async def test_login_invalid_body():
    async with make_client() as client:
        r = await client.post(
            "/api/login",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401


async def test_login_non_dict_json_body():
    async with make_client() as client:
        r = await client.post(
            "/api/login",
            content=b"[1, 2]",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401
        r = await client.post(
            "/api/login",
            content=b'"plain-string"',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401


async def test_overview_requires_session(monkeypatch):
    async with make_client() as client:
        r = await client.get("/api/overview")
        assert r.status_code == 401


async def test_overview_shape(monkeypatch):
    async def fake_stats(session, graph_store):
        return {"nodes_by_type": {"Requirement": 3}, "nodes_by_system": {},
                "edges_by_type": {"implements": 2}, "edges_by_system": {}}

    async def fake_quality(session, graph_store):
        return {"total_nodes": 3, "total_edges": 2, "orphan_nodes": 1,
                "duplicate_groups_count": 0, "duplicate_node_count": 0,
                "connected_components": 1}

    async def fake_nodes_by_status(session):
        return {"approved": 3}

    async def fake_batches(session):
        return 1

    async def fake_keys(session):
        return {"admin": {"active": 1}}

    async def fake_systems(session):
        return ["sys-1"]

    async def fake_mounts(session):
        return 2

    async def fake_tasks(session):
        return {"pending": 1, "running": 0, "done": 0, "failed": 0}

    async def fake_health():
        return True

    monkeypatch.setattr(visual_api, "get_graph_stats", fake_stats)
    monkeypatch.setattr(visual_api, "get_graph_quality_report", fake_quality)
    monkeypatch.setattr(visual_api, "count_nodes_by_status", fake_nodes_by_status)
    monkeypatch.setattr(visual_api, "count_pending_batches", fake_batches)
    monkeypatch.setattr(visual_api, "get_access_key_stats", fake_keys)
    monkeypatch.setattr(visual_api, "list_systems", fake_systems)
    monkeypatch.setattr(visual_api, "count_system_mounts", fake_mounts)
    monkeypatch.setattr(visual_api, "get_task_status_counts", fake_tasks)
    monkeypatch.setattr(visual_api, "embedding_health_ok", fake_health)

    async with make_client() as client:
        await client.post("/api/login", json={"username": "vu", "password": "vp"})
        r = await client.get("/api/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["graph"]["nodes_by_type"] == {"Requirement": 3}
        assert data["quality"]["orphan_nodes"] == 1
        assert data["nodes_by_status"] == {"approved": 3}
        assert data["batches_pending"] == 1
        assert data["keys"] == {"admin": {"active": 1}}
        assert data["systems"] == {"systems": 1, "project_mounts": 2}
        assert data["reindex_tasks"] == {"pending": 1, "running": 0, "done": 0, "failed": 0}
        assert data["health"] == {"database": True, "embedding": True}


async def test_static_routes():
    async with make_client() as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert "MemLake" in r.text
        r = await client.get("/static/app.css")
        assert r.status_code == 200
        r = await client.get("/static/vendor/echarts.min.js")
        assert r.status_code == 200
        assert len(r.content) > 100_000
