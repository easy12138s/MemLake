"""可视化控制台集成测试：数据层只读聚合 helper 与 overview 端点（真实 DB）。

helper 断言基于 db_session（事务回滚隔离）内的相对计数，不依赖共享库现状；
端点级断言为形状 + 非负性（生产库数据不可控）。
"""

import uuid

import httpx
from starlette.applications import Starlette

from mem_lake.approval.models import ApprovalBatch
from mem_lake.approval.repository import count_pending_batches
from mem_lake.auth.models import AccessKey
from mem_lake.auth.service import get_access_key_stats
from mem_lake.config import Settings
from mem_lake.gateway.background_tasks import get_task_status_counts
from mem_lake.gateway.models import ReindexTask
from mem_lake.knowledge.models import KnowledgeNode, System, SystemProject
from mem_lake.knowledge.repository import (
    count_nodes_by_status,
    count_system_mounts,
)
from mem_lake.visual import create_visual_app


async def test_count_pending_batches(db_session):
    base = await count_pending_batches(db_session)
    db_session.add(
        ApprovalBatch(
            batch_type="publish_requirement",
            submitted_by="visual-test",
            submitter_role="pm",
            summary="",
        )
    )
    await db_session.flush()
    assert await count_pending_batches(db_session) == base + 1


async def test_get_access_key_stats(db_session):
    base = await get_access_key_stats(db_session)
    db_session.add(AccessKey(key_hash="visual-test-hash", role="dev", status="active"))
    await db_session.flush()
    stats = await get_access_key_stats(db_session)
    assert (
        stats["dev"]["active"]
        == base.get("dev", {}).get("active", 0) + 1
    )


async def test_get_task_status_counts(db_session):
    base = await get_task_status_counts(db_session)
    db_session.add(ReindexTask(created_by="visual-test", status="pending"))
    await db_session.flush()
    counts = await get_task_status_counts(db_session)
    assert counts["pending"] == base.get("pending", 0) + 1


async def test_count_nodes_by_status(db_session):
    base = await count_nodes_by_status(db_session)
    db_session.add(
        KnowledgeNode(
            type="Requirement",
            title="visual-test",
            content="visual-test",
            properties={"priority": "P2", "module": "visual"},
            status="approved",
            created_by="visual-test",
            system_id=uuid.uuid4(),
        )
    )
    db_session.add(
        KnowledgeNode(
            type="Requirement",
            title="visual-test-deleted",
            content="visual-test-deleted",
            properties={"priority": "P2", "module": "visual"},
            status="approved",
            created_by="visual-test",
            system_id=uuid.uuid4(),
            is_deleted=True,
        )
    )
    await db_session.flush()
    counts = await count_nodes_by_status(db_session)
    assert counts.get("approved", 0) == base.get("approved", 0) + 1


async def test_count_system_mounts(db_session):
    base = await count_system_mounts(db_session)
    system = System(name=f"visual-test-{uuid.uuid4().hex[:8]}", description="")
    db_session.add(system)
    await db_session.flush()
    db_session.add(SystemProject(system_id=system.id, project_id=uuid.uuid4()))
    await db_session.flush()
    assert await count_system_mounts(db_session) == base + 1


def make_client(**overrides) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="it-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_overview_endpoint_requires_session():
    async with make_client() as client:
        r = await client.get("/api/overview")
        assert r.status_code == 401


async def test_overview_endpoint_shape():
    """真实 DB + AGE 图上的形状与类型断言（生产数据不可控，不做精确计数）。"""
    async with make_client() as client:
        r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
        assert r.status_code == 200
        r = await client.get("/api/overview")
        assert r.status_code == 200
        data = r.json()
        for key in (
            "graph", "quality", "nodes_by_status", "batches_pending",
            "keys", "systems", "reindex_tasks", "health",
        ):
            assert key in data
        assert isinstance(data["graph"]["nodes_by_type"], dict)
        assert isinstance(data["graph"]["edges_by_type"], dict)
        assert isinstance(data["quality"]["orphan_nodes"], int)
        assert isinstance(data["batches_pending"], int) and data["batches_pending"] >= 0
        assert isinstance(data["systems"]["systems"], int)
        assert isinstance(data["reindex_tasks"], dict)
        assert data["health"]["database"] is True
        assert isinstance(data["health"]["embedding"], bool)
