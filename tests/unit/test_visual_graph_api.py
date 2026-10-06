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
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def _login(client):
    r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
    assert r.status_code == 200


async def test_graph_serialization_contract(monkeypatch):
    """载荷裁剪：正文前 100 字、vector_ready、None 置空、分页信息透传。"""
    nid, nid2 = uuid.uuid4(), uuid.uuid4()
    node = SimpleNamespace(
        id=nid,
        type="Requirement",
        title="登录需求",
        status="approved",
        system_id=None,
        project_id=None,
        requirement_key="SYS-0001",
        content="x" * 300,
    )

    async def fake_list(session, **kwargs):
        return [node], 57

    async def fake_embedded(session, *, node_ids):
        return {nid}

    class FakeStore:
        async def subgraph_edges(self, session, node_ids):
            assert node_ids == [nid]
            return [
                {
                    "source": str(nid),
                    "target": str(nid2),
                    "edge_type": "relates_to",
                    "properties": {"created_by": "dev"},
                },
            ]

    monkeypatch.setattr(visual_api, "list_graph_nodes", fake_list)
    monkeypatch.setattr(visual_api, "list_embedded_node_ids", fake_embedded)
    monkeypatch.setattr(visual_api, "get_graph_store", lambda: FakeStore())

    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/graph?offset=10&limit=50")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 57
        assert data["offset"] == 10
        assert data["limit"] == 50
        assert data["nodes"] == [
            {
                "id": str(nid),
                "type": "Requirement",
                "title": "登录需求",
                "status": "approved",
                "system_id": None,
                "project_id": None,
                "requirement_key": "SYS-0001",
                "content_preview": "x" * 100,
                "vector_ready": True,
            }
        ]
        assert data["edges"][0]["edge_type"] == "relates_to"
        assert data["edges"][0]["source"] == str(nid)


async def test_graph_param_validation(monkeypatch):
    """非法 system_id/limit/offset/status/types 一律 400。"""
    async with make_client() as client:
        await _login(client)
        for qs in (
            "system_id=zzz",
            "project_id=zzz",
            "limit=abc",
            "offset=abc",
            "offset=-1",
            "status=bogus",
            "types=NotAType",
        ):
            r = await client.get(f"/api/graph?{qs}")
            assert r.status_code == 400, qs


async def test_graph_tree_contract(monkeypatch):
    """树端点：需求为顶点 + 一跳资产 + ProjectProfile 项目桶 + 分页信息。"""
    rid, aid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    req = SimpleNamespace(
        id=rid,
        title="权限需求",
        status="approved",
        requirement_key="SYS-0003",
        system_id=None,
        project_id=pid,
        created_at=datetime(2026, 10, 1, 12, 0, 0),
    )
    asset = SimpleNamespace(
        id=aid,
        type="CodeSnippet",
        title="鉴权片段",
        status="approved",
        project_id=pid,
    )
    project = SimpleNamespace(
        id=pid,
        type="ProjectProfile",
        title="用户中心",
        status="approved",
        project_id=pid,
    )

    async def fake_list(session, **kwargs):
        # 树模式强制以 Requirement 为顶点分页
        assert kwargs["node_types"] == ("Requirement",)
        assert kwargs["limit"] <= 200
        return [req], 1

    class FakeStore:
        async def incident_edges(self, session, node_ids):
            return [
                # 需求→资产（保留）；资产→资产（两端均非需求，后端过滤不呈现）
                {"source": str(rid), "target": str(aid), "edge_type": "realized_by", "properties": {}},
                {"source": str(aid), "target": str(pid), "edge_type": "relates_to", "properties": {}},
                # 需求→项目档案（保留，ProjectProfile 进项目标题桶）
                {"source": str(rid), "target": str(pid), "edge_type": "belongs_to", "properties": {}},
                # 悬空边：对端在 PG 查不到（图投影漂移），须被过滤
                {"source": str(rid), "target": str(uuid.uuid4()), "edge_type": "references", "properties": {}},
            ]

    async def fake_get_by_ids(session, *, node_ids, status=None, include_deleted=False):
        # 图边对端回查：资产与 ProjectProfile 均可见（悬空 uuid 除外）
        by_id = {a.id: a for a in (asset, project)}
        return [by_id[i] for i in node_ids if i in by_id]

    async def fake_profiles(session, *, project_ids):
        # 项目桶：按业务 project_id 锚点（ProjectProfile.project_id）回查
        assert project_ids == [pid]
        return [project]

    monkeypatch.setattr(visual_api, "list_graph_nodes", fake_list)
    monkeypatch.setattr(visual_api, "get_graph_store", lambda: FakeStore())
    monkeypatch.setattr(visual_api, "get_nodes_by_ids", fake_get_by_ids)
    monkeypatch.setattr(visual_api, "get_project_profiles_by_ids", fake_profiles)

    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/graph/tree")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["requirements"][0]["title"] == "权限需求"
        assert data["requirements"][0]["project_id"] == str(pid)
        # 资产-资产边不进 links；需求-资产/需求-项目边保留（按 edge_type 排序稳定比较）
        assert sorted(data["links"], key=lambda x: x["edge_type"]) == [
            {"source": str(rid), "target": str(pid), "edge_type": "belongs_to"},
            {"source": str(rid), "target": str(aid), "edge_type": "realized_by"},
        ]
        assert data["assets"] == [
            {"id": str(aid), "type": "CodeSnippet", "title": "鉴权片段", "status": "approved", "project_id": str(pid)}
        ]
        assert data["projects"] == [{"id": str(pid), "title": "用户中心"}]


async def test_graph_tree_empty(monkeypatch):
    """无匹配需求：空结构不报错（前端降级空态）。"""

    async def fake_list(session, **kwargs):
        return [], 0

    monkeypatch.setattr(visual_api, "list_graph_nodes", fake_list)
    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/graph/tree?q=ZZQG")
        assert r.status_code == 200
        data = r.json()
        assert data["requirements"] == []
        assert data["total"] == 0


async def test_node_detail_serialization_contract(monkeypatch):
    """详情契约：完整字段 + 邻居 PG 回查 + 关联边触及本节点过滤。"""
    nid, nb1 = uuid.uuid4(), uuid.uuid4()
    node = SimpleNamespace(
        id=nid,
        type="Requirement",
        title="详情需求",
        status="approved",
        requirement_key="SYS-0002",
        system_id=None,
        project_id=None,
        content="完整正文",
        properties={"module": "auth"},
        tags=["登录"],
        version=3,
        created_by="pm-key",
        created_at=datetime(2026, 10, 1, 12, 0, 0),
    )
    nb_row = SimpleNamespace(
        id=nb1,
        type="CodeSnippet",
        title="关联片段",
        status="approved",
        system_id=None,
        project_id=None,
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
            return [{"label": "CodeSnippet", "properties": {"id": str(nb1), "title": "关联片段"}}]

        async def subgraph_edges(self, session, node_ids):
            return [
                {"source": str(nid), "target": str(nb1), "edge_type": "relates_to", "properties": {}},
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
            {
                "id": str(nb1),
                "type": "CodeSnippet",
                "title": "关联片段",
                "status": "approved",
                "system_id": None,
                "project_id": None,
            },
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
        assert r.json() == {"systems": [{"id": str(sid), "name": "测试系统域", "description": "说明"}]}
