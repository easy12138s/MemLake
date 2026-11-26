"""review_approve 批量向量重算测试（无 DB）。"""

import uuid
from types import SimpleNamespace

import pytest

from mem_lake.approval import executor


def _node(title="旧标题", content="内容", properties=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        type="Requirement",
        title=title,
        content=content,
        properties=properties or {"priority": "P1", "module": "auth"},
        tags=[],
        project_id=uuid.uuid4(),
        system_id=uuid.uuid4(),
        version=1,
    )


def _batch(items):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="pending_review",
        project_id=None,
        batch_type="update_node",
        items=items,
        conflict_hint=None,
        reviewed_by=None,
        reviewed_at=None,
        review_comment=None,
        summary="1 个节点 + 0 个关系",
    )


def _update_item(node_id, payload):
    return SimpleNamespace(
        item_type="node",
        action="update",
        entity_type="Requirement",
        payload={"node_id": str(node_id), **payload},
        target_id=None,
    )


class _Session:
    async def flush(self):
        return None


async def _run_review(monkeypatch, item, before, after):
    batch = _batch([item])
    calls: dict = {}

    async def fake_pending(session, batch_id, *, verb):
        return batch

    async def fake_get_node(session, node_id):
        return before

    async def fake_nodes(session, **kwargs):
        return [before]

    async def fake_update_node(session, **kwargs):
        calls["update"] = kwargs
        return after

    async def fake_regen(session, *, nodes, embedding_client):
        calls["regen"] = nodes
        return len(nodes)

    async def fake_audit(session, **kwargs):
        return None

    monkeypatch.setattr(executor, "get_pending_batch", fake_pending)
    monkeypatch.setattr(executor, "get_node", fake_get_node)
    monkeypatch.setattr(executor, "get_nodes_by_ids", fake_nodes)
    monkeypatch.setattr(executor, "update_node", fake_update_node)
    monkeypatch.setattr(executor, "regenerate_node_vectors_batch", fake_regen)
    monkeypatch.setattr(executor, "write_audit_log", fake_audit)

    await executor.review_approve(
        _Session(),
        batch_id=batch.id,
        reviewed_by="ak_admin",
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
    )
    calls["batch"] = batch
    return calls


@pytest.mark.asyncio
async def test_content_update_regenerates_vectors_once(monkeypatch):
    before = _node()
    after = _node(title="新标题")
    item = _update_item(before.id, {"title": "新标题", "base_version": 1})

    calls = await _run_review(monkeypatch, item, before, after)

    assert calls["update"]["regenerate_vector"] is False
    assert calls["update"]["expected_version"] == 1
    assert calls["regen"] == [after]
    assert calls["batch"].conflict_hint["conflict_checked"] is False


@pytest.mark.asyncio
async def test_tags_only_update_skips_vector_regeneration(monkeypatch):
    before = _node()
    after = _node()
    after.tags = ["已确认"]
    item = _update_item(before.id, {"tags_add": ["已确认"]})

    calls = await _run_review(monkeypatch, item, before, after)

    assert "regen" not in calls
    assert calls["batch"].conflict_hint["conflict_checked"] is True


@pytest.mark.asyncio
async def test_missing_update_target_fails_before_any_write(monkeypatch):
    from mem_lake.approval.validation import PayloadValidationError

    before = _node()
    item = _update_item(before.id, {"title": "新标题"})
    batch = _batch([item])
    calls = {"update": 0}

    async def fake_pending(session, batch_id, *, verb):
        return batch

    async def fake_nodes(session, **kwargs):
        return []

    async def fake_update(session, **kwargs):  # pragma: no cover - 不应被调用
        calls["update"] += 1
        return before

    monkeypatch.setattr(executor, "get_pending_batch", fake_pending)
    monkeypatch.setattr(executor, "get_nodes_by_ids", fake_nodes)
    monkeypatch.setattr(executor, "update_node", fake_update)

    with pytest.raises(PayloadValidationError, match="不存在或已删除"):
        await executor.review_approve(
            _Session(),
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=object(),
            embedding_client=object(),
            vector_searcher=object(),
        )
    assert calls["update"] == 0


@pytest.mark.asyncio
async def test_same_orm_instance_still_marks_vector_dirty(monkeypatch):
    """回归：get_node 与 update_node 命中 identity map 返回同一实例时，
    必须用标量快照比较，否则向量永远不会重算。"""
    node = _node()
    item = _update_item(node.id, {"title": "新标题"})
    batch = _batch([item])
    calls: dict = {}

    async def fake_pending(session, batch_id, *, verb):
        return batch

    async def fake_nodes(session, **kwargs):
        return [node]

    async def fake_get_node(session, node_id):
        return node

    async def fake_update(session, **kwargs):
        node.title = kwargs["title"]
        return node

    async def fake_regen(session, *, nodes, embedding_client):
        calls["regen"] = nodes
        return len(nodes)

    async def fake_audit(session, **kwargs):
        return None

    monkeypatch.setattr(executor, "get_pending_batch", fake_pending)
    monkeypatch.setattr(executor, "get_nodes_by_ids", fake_nodes)
    monkeypatch.setattr(executor, "get_node", fake_get_node)
    monkeypatch.setattr(executor, "update_node", fake_update)
    monkeypatch.setattr(executor, "regenerate_node_vectors_batch", fake_regen)
    monkeypatch.setattr(executor, "write_audit_log", fake_audit)

    await executor.review_approve(
        _Session(),
        batch_id=batch.id,
        reviewed_by="ak_admin",
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
    )

    assert calls["regen"] == [node]
