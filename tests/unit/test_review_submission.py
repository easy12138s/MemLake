"""review_submission 批量处理核心测试（无 DB）。"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from mem_lake.gateway.tools import review_tools
from mem_lake.gateway.tools.review_tools import _review_submission_core


@pytest.fixture(autouse=True)
def _count_pending(monkeypatch):
    async def _count(session, **kwargs):
        return 0

    monkeypatch.setattr(review_tools, "count_pending_batches", _count)


def _batch(status: str = "pending_review") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=None,
        status=status,
        conflict_hint=None,
        items=[],
    )


@pytest.mark.asyncio
async def test_review_submission_auto_processes_each_batch(monkeypatch):
    batch = _batch()

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_list(session, **kwargs):
        return [batch]

    async def fake_auto(session, **kwargs):
        return {
            "decision": "auto_approved",
            "conflict_hint": {"has_conflict": False, "checked_nodes": 1},
            "batch": SimpleNamespace(
                id=batch.id, status="approved", project_id=None, conflict_hint=None
            ),
            "created_node_ids": [uuid.uuid4()],
        }

    async def fake_enqueue(project_id, node_ids):
        return None

    monkeypatch.setattr(review_tools, "readonly_session", fake_session)
    monkeypatch.setattr(review_tools, "transactional_session", fake_session)
    monkeypatch.setattr(review_tools, "list_pending_batches", fake_list)
    monkeypatch.setattr(review_tools, "auto_process_batch", fake_auto)
    monkeypatch.setattr(review_tools, "_safe_enqueue_embed", fake_enqueue)
    monkeypatch.setattr(
        review_tools, "build_created_nodes_payload", lambda b: [{"node_id": "n1"}]
    )
    monkeypatch.setattr(review_tools, "count_created_edges", lambda b: 0)

    out = await _review_submission_core(
        submission_id=uuid.uuid4(),
        action="auto",
        review_comment=None,
        batch_ids=None,
        reviewed_by="ak_admin",
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
    )

    assert out.processed == 1
    assert out.items[0].batch_id == batch.id
    assert out.items[0].decision == "auto_approved"
    assert out.items[0].status == "approved"
    assert out.items[0].created is not None


@pytest.mark.asyncio
async def test_review_submission_reject_requires_comment(monkeypatch):
    with pytest.raises(ValueError, match="review_comment"):
        await _review_submission_core(
            submission_id=uuid.uuid4(),
            action="reject",
            review_comment="  ",
            batch_ids=None,
            reviewed_by="ak_admin",
            graph_store=None,
            embedding_client=None,
            vector_searcher=None,
        )


@pytest.mark.asyncio
async def test_review_submission_filters_batch_ids(monkeypatch):
    b1, b2 = _batch(), _batch()

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_list(session, **kwargs):
        wanted = kwargs.get("batch_ids")
        return [b for b in (b1, b2) if not wanted or b.id in wanted]

    seen: list = []

    async def fake_auto(session, **kwargs):
        seen.append(kwargs["batch_id"])
        b = b1 if kwargs["batch_id"] == b1.id else b2
        return {
            "decision": "needs_human_review",
            "conflict_hint": {"has_conflict": True},
            "batch": b,
            "created_node_ids": [],
        }

    monkeypatch.setattr(review_tools, "readonly_session", fake_session)
    monkeypatch.setattr(review_tools, "transactional_session", fake_session)
    monkeypatch.setattr(review_tools, "list_pending_batches", fake_list)
    monkeypatch.setattr(review_tools, "auto_process_batch", fake_auto)

    out = await _review_submission_core(
        submission_id=uuid.uuid4(),
        action="auto",
        review_comment=None,
        batch_ids=[b2.id],
        reviewed_by="ak_admin",
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
    )

    assert seen == [b2.id]
    assert out.processed == 1


@pytest.mark.asyncio
async def test_review_submission_sanitizes_internal_errors(monkeypatch):
    batch = _batch()

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_list(session, **kwargs):
        return [batch]

    async def boom(session, **kwargs):
        raise RuntimeError("secret dsn")

    async def fake_enqueue(project_id, node_ids):
        return None

    monkeypatch.setattr(review_tools, "readonly_session", fake_session)
    monkeypatch.setattr(review_tools, "transactional_session", fake_session)
    monkeypatch.setattr(review_tools, "list_pending_batches", fake_list)
    monkeypatch.setattr(review_tools, "approval_review_approve", boom)
    monkeypatch.setattr(review_tools, "_safe_enqueue_embed", fake_enqueue)

    out = await _review_submission_core(
        submission_id=uuid.uuid4(),
        action="approve",
        review_comment=None,
        batch_ids=None,
        reviewed_by="ak_admin",
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
    )

    assert out.items[0].status == "error"
    assert out.items[0].error == "工具调用失败: RuntimeError"


@pytest.mark.asyncio
async def test_review_submission_filters_batch_ids_in_query_and_reports_more(monkeypatch):
    batches = [_batch(), _batch(), _batch()]
    seen: dict = {}

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_list(session, **kwargs):
        seen.update(kwargs)
        return batches

    async def fake_count(session, **kwargs):
        return 5

    async def fake_reject(session, **kwargs):
        return SimpleNamespace(
            id=kwargs["batch_id"], status="rejected", conflict_hint=None, items=[]
        )

    monkeypatch.setattr(review_tools, "readonly_session", fake_session)
    monkeypatch.setattr(review_tools, "transactional_session", fake_session)
    monkeypatch.setattr(review_tools, "list_pending_batches", fake_list)
    monkeypatch.setattr(review_tools, "count_pending_batches", fake_count)
    monkeypatch.setattr(review_tools, "approval_review_reject", fake_reject)

    wanted = [b.id for b in batches]
    out = await _review_submission_core(
        submission_id=uuid.uuid4(),
        action="reject",
        review_comment="dup",
        batch_ids=wanted,
        reviewed_by="ak_admin",
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
        limit=2,
    )

    assert seen["batch_ids"] == wanted
    assert seen["limit"] == 3
    assert seen["order_asc"] is True
    assert out.has_more is True
    assert out.remaining == 5
    assert out.processed == 2
