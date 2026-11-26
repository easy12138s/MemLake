"""update_node 增量合并 / 乐观版本校验 / no-op 测试（无 DB）。"""

import uuid
from types import SimpleNamespace

import pytest

from mem_lake.knowledge import repository
from mem_lake.knowledge.repository import VersionConflictError, merge_update_values


def _node(**overrides) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "type": "Requirement",
        "title": "标题",
        "content": "正文",
        "version": 3,
        "properties": {"priority": "P1", "module": "医保"},
        "tags": ["a"],
        "source": None,
        "project_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Session:
    def __init__(self):
        self.flushed = 0

    async def flush(self):
        self.flushed += 1


def test_merge_update_values_patch_preserves_untouched_keys():
    node = _node()

    properties, tags = merge_update_values(
        node, properties_patch={"priority": "P0"}, tags_add=["b"], tags_remove=["a"]
    )

    assert properties == {"priority": "P0", "module": "医保"}
    assert tags == ["b"]


def test_merge_update_values_returns_none_when_not_requested():
    node = _node()

    assert merge_update_values(node) == (None, None)


def test_merge_update_values_rejects_properties_and_patch_together():
    with pytest.raises(ValueError, match="互斥"):
        merge_update_values(
            _node(), properties={"priority": "P0", "module": "m"}, properties_patch={}
        )


@pytest.mark.asyncio
async def test_update_node_rejects_stale_expected_version(monkeypatch):
    node = _node()

    async def fake_get_node(session, node_id):
        return node

    monkeypatch.setattr(repository, "get_node", fake_get_node)

    with pytest.raises(VersionConflictError):
        await repository.update_node(
            _Session(),
            graph_store=None,
            embedding_client=None,
            node_id=node.id,
            title="新标题",
            expected_version=2,
            actor="ak_pm",
        )


@pytest.mark.asyncio
async def test_update_node_noop_patch_does_not_bump_version(monkeypatch):
    node = _node()

    async def fake_get_node(session, node_id):
        return node

    monkeypatch.setattr(repository, "get_node", fake_get_node)

    result = await repository.update_node(
        _Session(),
        graph_store=None,
        embedding_client=None,
        node_id=node.id,
        properties_patch={"priority": "P1"},
        regenerate_vector=False,
        actor="ak_pm",
    )

    assert result.version == 3
    assert result.properties == {"priority": "P1", "module": "医保"}


@pytest.mark.asyncio
async def test_update_node_patch_bumps_version_and_merges(monkeypatch):
    node = _node()
    audits: list[dict] = []

    async def fake_get_node(session, node_id):
        return node

    async def fake_audit(session, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(repository, "get_node", fake_get_node)
    monkeypatch.setattr(repository, "write_audit_log", fake_audit)

    result = await repository.update_node(
        _Session(),
        graph_store=None,
        embedding_client=None,
        node_id=node.id,
        properties_patch={"priority": "P0"},
        regenerate_vector=False,
        actor="ak_pm",
    )

    assert result.version == 4
    assert result.properties == {"priority": "P0", "module": "医保"}
    assert audits and audits[0]["detail"]["changes"]["properties"] == "updated"


def _update_item(payload: dict) -> dict:
    return {
        "item_type": "node",
        "action": "update",
        "entity_type": "Requirement",
        "payload": payload,
    }


def test_validate_item_payload_accepts_bind_payload():
    from mem_lake.approval.validation import _validate_item_payload

    _validate_item_payload(
        _update_item({"node_id": str(uuid.uuid4()), "project_id": None}), 0
    )


def test_validate_item_payload_requires_a_change_field():
    from mem_lake.approval.validation import PayloadValidationError, _validate_item_payload

    with pytest.raises(PayloadValidationError, match="至少提供一个变更字段"):
        _validate_item_payload(_update_item({"node_id": str(uuid.uuid4())}), 0)


def test_validate_item_payload_rejects_properties_and_patch():
    from mem_lake.approval.validation import PayloadValidationError, _validate_item_payload

    with pytest.raises(PayloadValidationError, match="互斥"):
        _validate_item_payload(
            _update_item(
                {
                    "node_id": str(uuid.uuid4()),
                    "properties": {"priority": "P1"},
                    "properties_patch": {"priority": "P0"},
                }
            ),
            0,
        )


def test_validate_item_payload_rejects_bad_base_version():
    from mem_lake.approval.validation import PayloadValidationError, _validate_item_payload

    with pytest.raises(PayloadValidationError, match="base_version"):
        _validate_item_payload(
            _update_item({"node_id": str(uuid.uuid4()), "title": "x", "base_version": 0}),
            0,
        )
