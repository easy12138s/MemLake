"""update_requirements 批量标签/属性更新核心测试（无 DB）。"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from mem_lake.approval.service import PayloadValidationError
from mem_lake.gateway.tools import write_tools
from mem_lake.gateway.tools.write_tools import _update_requirements_batch

PROJECT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
SYSTEM_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


def _node(*, tags=None, properties=None, project_id=PROJECT_ID, system_id=SYSTEM_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        type="Requirement",
        version=1,
        project_id=project_id,
        system_id=system_id,
        tags=tags or [],
        properties=properties or {"priority": "P2", "module": "药库"},
    )


@asynccontextmanager
async def _fake_session():
    yield None


@pytest.mark.asyncio
async def test_update_requirements_merges_tags_and_properties(monkeypatch):
    n1 = _node(tags=["待确认"], properties={"priority": "P2", "module": "药库"})
    n2 = _node(tags=["待确认", "old"], properties={"priority": "P3", "module": "收费"})
    calls: dict = {}

    async def fake_validate_refs(project_id, ref_ids, label):
        return None

    async def fake_get_nodes(session, **kwargs):
        return [n1, n2]

    async def fake_submit(**kwargs):
        calls["submit"] = kwargs
        return "ok"

    monkeypatch.setattr(write_tools, "readonly_session", _fake_session)
    monkeypatch.setattr(write_tools, "_validate_requirement_refs", fake_validate_refs)
    monkeypatch.setattr(write_tools, "get_nodes_by_ids", fake_get_nodes)
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)
    monkeypatch.setattr(write_tools, "get_current_role", lambda: "pm")
    monkeypatch.setattr(
        write_tools,
        "get_settings",
        lambda: type(
            "S", (), {"MAX_ITEMS_PER_BATCH": 50, "MAX_CONTENT_LENGTH": 10000}
        )(),
    )

    result = await _update_requirements_batch(
        node_ids=[n1.id, n2.id],
        tags_add=["已确认"],
        tags_remove=["待确认"],
        properties_patch={"priority": "P1"},
        operation_id=None,
    )

    assert result == "ok"
    items = calls["submit"]["items"]
    assert len(items) == 2
    assert items[0]["payload"]["tags_add"] == ["已确认"]
    assert items[0]["payload"]["tags_remove"] == ["待确认"]
    assert items[0]["payload"]["properties_patch"] == {"priority": "P1"}
    assert items[0]["payload"]["base_version"] == 1
    assert items[1]["payload"]["tags_add"] == ["已确认"]
    assert items[1]["payload"]["tags_remove"] == ["待确认"]
    assert calls["submit"]["batch_type"] == "update_node"
    assert calls["submit"]["submitter_role"] == "pm"


@pytest.mark.asyncio
async def test_update_requirements_requires_a_change(monkeypatch):
    with pytest.raises(PayloadValidationError, match="至少提供"):
        await _update_requirements_batch(
            node_ids=[uuid.uuid4()],
            tags_add=None,
            tags_remove=None,
            properties_patch=None,
            operation_id=None,
        )


@pytest.mark.asyncio
async def test_update_requirements_skips_noop_nodes(monkeypatch):
    node = _node(tags=["a"], properties={"priority": "P2", "module": "m"})

    async def fake_validate_refs(project_id, ref_ids, label):
        return None

    async def fake_get_nodes(session, **kwargs):
        return [node]

    monkeypatch.setattr(write_tools, "readonly_session", _fake_session)
    monkeypatch.setattr(write_tools, "_validate_requirement_refs", fake_validate_refs)
    monkeypatch.setattr(write_tools, "get_nodes_by_ids", fake_get_nodes)
    monkeypatch.setattr(
        write_tools, "get_settings", lambda: type("S", (), {"MAX_ITEMS_PER_BATCH": 50})()
    )

    with pytest.raises(PayloadValidationError, match="没有实际"):
        await _update_requirements_batch(
            node_ids=[node.id],
            tags_add=["a"],
            tags_remove=["missing"],
            properties_patch=None,
            operation_id=None,
        )


async def _run_submit(monkeypatch, nodes):
    calls: dict = {}

    async def fake_validate_refs(project_id, ref_ids, label):
        return None

    async def fake_get_nodes(session, **kwargs):
        return nodes

    async def fake_submit(**kwargs):
        calls["submit"] = kwargs
        return "ok"

    monkeypatch.setattr(write_tools, "readonly_session", _fake_session)
    monkeypatch.setattr(write_tools, "_validate_requirement_refs", fake_validate_refs)
    monkeypatch.setattr(write_tools, "get_nodes_by_ids", fake_get_nodes)
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)
    monkeypatch.setattr(write_tools, "get_current_role", lambda: "pm")
    monkeypatch.setattr(
        write_tools,
        "get_settings",
        lambda: type("S", (), {"MAX_ITEMS_PER_BATCH": 50})(),
    )

    await _update_requirements_batch(
        node_ids=[n.id for n in nodes],
        tags_add=["新标签"],
        tags_remove=None,
        properties_patch=None,
        operation_id=None,
    )
    return calls["submit"]


@pytest.mark.asyncio
async def test_update_requirements_uses_single_project_id(monkeypatch):
    nodes = [_node(), _node()]

    submit = await _run_submit(monkeypatch, nodes)

    assert submit["project_id"] == PROJECT_ID


@pytest.mark.asyncio
async def test_update_requirements_rejects_cross_project_batch(monkeypatch):
    other = uuid.uuid4()
    nodes = [_node(), _node(project_id=other)]

    with pytest.raises(PayloadValidationError, match="按项目拆分"):
        await _run_submit(monkeypatch, nodes)


@pytest.mark.asyncio
async def test_update_requirements_rejects_cross_system_batch(monkeypatch):
    nodes = [_node(), _node(system_id=uuid.uuid4())]

    with pytest.raises(PayloadValidationError, match="同一 system"):
        await _run_submit(monkeypatch, nodes)
