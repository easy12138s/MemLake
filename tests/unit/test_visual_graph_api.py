"""图页端点单测：序列化契约与参数校验（monkeypatch 数据层，不依赖查询结果）。"""

import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette

from mem_lake.config import Settings
from mem_lake.visual import api as visual_api
from mem_lake.visual import create_visual_app


def make_client(base_url="http://test", **overrides) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="test-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=base_url
    )


async def _login(client):
    r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
    assert r.status_code == 200


async def test_graph_serialization_contract(monkeypatch):
    """载荷裁剪：正文前 100 字、vector_ready、None 置空、truncated/edges 透传。"""
    nid, nid2 = uuid.uuid4(), uuid.uuid4()
    node = SimpleNamespace(
        id=nid, type="Requirement", title="登录需求", status="approved",
        system_id=None, project_id=None, requirement_key="SYS-0001",
        content="x" * 300,
    )

    async def fake_list(session, **kwargs):
        return [node], True

    async def fake_embedded(session, *, node_ids):
        return {nid}

    class FakeStore:
        async def subgraph_edges(self, session, node_ids):
            assert node_ids == [nid]
            return [
                {"source": str(nid), "target": str(nid2),
                 "edge_type": "relates_to", "properties": {"created_by": "dev"}},
            ]

    monkeypatch.setattr(visual_api, "list_graph_nodes", fake_list)
    monkeypatch.setattr(visual_api, "list_embedded_node_ids", fake_embedded)
    monkeypatch.setattr(visual_api, "get_graph_store", lambda: FakeStore())

    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/graph")
        assert r.status_code == 200
        data = r.json()
        assert data["truncated"] is True
        assert data["nodes"] == [
            {
                "id": str(nid), "type": "Requirement", "title": "登录需求",
                "status": "approved", "system_id": None, "project_id": None,
                "requirement_key": "SYS-0001", "content_preview": "x" * 100,
                "vector_ready": True,
            }
        ]
        assert data["edges"][0]["edge_type"] == "relates_to"
        assert data["edges"][0]["source"] == str(nid)


async def test_graph_param_validation(monkeypatch):
    """非法 system_id/limit/status/types 一律 400。"""
    async with make_client() as client:
        await _login(client)
        for qs in ("system_id=zzz", "project_id=zzz", "limit=abc",
                    "status=bogus", "types=NotAType"):
            r = await client.get(f"/api/graph?{qs}")
            assert r.status_code == 400, qs


async def test_node_detail_serialization_contract(monkeypatch):
    """详情契约：完整字段 + 邻居 PG 回查 + 关联边触及本节点过滤。"""
    nid, nb1 = uuid.uuid4(), uuid.uuid4()
    node = SimpleNamespace(
        id=nid, type="Requirement", title="详情需求", status="approved",
        requirement_key="SYS-0002", system_id=None, project_id=None,
        content="完整正文", properties={"module": "auth"}, tags=["登录"],
        version=3, created_by="pm-key", created_at=datetime(2026, 10, 1, 12, 0, 0),
    )
    nb_row = SimpleNamespace(
        id=nb1, type="CodeSnippet", title="关联片段", status="approved",
        system_id=None, project_id=None,
    )

    async def fake_get_node(session, node_id):
        assert node_id == nid
        return node

    async def fake_has_embedding(session, node_id):
        return True

    async def fake_get_by_ids(session, *, node_ids, status=None, include_deleted=False):
        assert node_ids == [nb1]
        return [nb_row]

    class FakeStore:
        async def neighbors(self, session, node_id, depth=1):
            return [{"label": "CodeSnippet",
                     "properties": {"id": str(nb1), "title": "关联片段"}}]

        async def subgraph_edges(self, session, node_ids):
            return [
                {"source": str(nid), "target": str(nb1),
                 "edge_type": "relates_to", "properties": {}},
            ]

    monkeypatch.setattr(visual_api, "get_node", fake_get_node)
    monkeypatch.setattr(visual_api, "node_has_embedding", fake_has_embedding)
    monkeypatch.setattr(visual_api, "get_nodes_by_ids", fake_get_by_ids)
    monkeypatch.setattr(visual_api, "get_graph_store", lambda: FakeStore())

    async with make_client() as client:
        await _login(client)
        r = await client.get(f"/api/node/{nid}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == str(nid)
        assert data["content"] == "完整正文"
        assert data["vector_ready"] is True
        assert data["created_at"] == "2026-10-01T12:00:00"
        assert data["neighbors"] == [
            {"id": str(nb1), "type": "CodeSnippet", "title": "关联片段",
             "status": "approved", "system_id": None, "project_id": None},
        ]
        assert len(data["edges"]) == 1
        assert data["edges"][0]["source"] == str(nid)


async def test_systems_serialization_contract(monkeypatch):
    """/api/systems 精简清单：id/name/description；批次四在其上扩展。"""
    sid = uuid.uuid4()

    class FakeSystem:
        id = sid
        name = "测试系统域"
        description = "说明"

    async def fake_list(session):
        return [FakeSystem()]

    monkeypatch.setattr(visual_api, "list_systems", fake_list)
    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/systems")
        assert r.status_code == 200
        assert r.json() == {"systems": [
            {"id": str(sid), "name": "测试系统域", "description": "说明"}
        ]}
