"""update_requirement_relations 的 scope 核心逻辑测试。

悬浮需求关系不需要 project_id 作为权限锚点：project_id 省略时应按两端
需求自身归属校验可见性，且不触发项目级鉴权。
"""

import uuid
from contextlib import asynccontextmanager

import pytest

from mem_lake.approval.service import PayloadValidationError
from mem_lake.gateway.tools import write_tools
from mem_lake.gateway.tools.write_tools import (
    RelationInput,
    _submit_requirement_relations,
)


@pytest.mark.asyncio
async def test_relations_without_project_skip_project_auth(monkeypatch):
    calls: dict = {}

    def fake_validate_project(project_id):
        calls["project"] = project_id

    async def fake_validate_refs(project_id, ref_ids, label):
        calls["refs"] = (project_id, ref_ids, label)

    async def fake_submit(**kwargs):
        calls["submit"] = kwargs
        return "ok"

    monkeypatch.setattr(write_tools, "validate_project_access", fake_validate_project)
    monkeypatch.setattr(write_tools, "_validate_requirement_refs", fake_validate_refs)
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    from_id, to_id = uuid.uuid4(), uuid.uuid4()
    result = await _submit_requirement_relations(
        project_id=None,
        relations=[
            RelationInput(from_id=from_id, to_id=to_id, relation_type="relates_to")
        ],
        operation_id=None,
    )

    assert result == "ok"
    assert "project" not in calls
    assert calls["refs"][0] is None
    assert calls["refs"][1] == [str(from_id), str(to_id)]
    assert calls["submit"]["project_id"] is None
    assert calls["submit"]["batch_type"] == "update_requirement_relations"
    item = calls["submit"]["items"][0]
    assert item["item_type"] == "edge"
    assert item["entity_type"] == "relates_to"
    assert item["payload"]["from_ref"] == str(from_id)
    assert item["payload"]["to_ref"] == str(to_id)


@pytest.mark.asyncio
async def test_relations_with_project_still_validates_project(monkeypatch):
    calls: dict = {}

    def fake_validate_project(project_id):
        calls["project"] = project_id

    async def fake_validate_refs(project_id, ref_ids, label):
        calls["refs"] = (project_id, ref_ids, label)

    async def fake_submit(**kwargs):
        return "ok"

    monkeypatch.setattr(write_tools, "validate_project_access", fake_validate_project)
    monkeypatch.setattr(write_tools, "_validate_requirement_refs", fake_validate_refs)
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    project_id = uuid.uuid4()
    await _submit_requirement_relations(
        project_id=project_id,
        relations=[
            RelationInput(
                from_id=uuid.uuid4(),
                to_id=uuid.uuid4(),
                relation_type="relates_to",
            )
        ],
        operation_id=None,
    )

    assert calls["project"] == project_id
    assert calls["refs"][0] == project_id


@pytest.mark.asyncio
async def test_relations_empty_rejected(monkeypatch):
    from mem_lake.approval.service import PayloadValidationError

    with pytest.raises(PayloadValidationError):
        await _submit_requirement_relations(
            project_id=None, relations=[], operation_id=None
        )


def test_relation_endpoint_requires_exactly_one_side():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RelationInput(relation_type="relates_to")
    with pytest.raises(ValidationError):
        RelationInput(
            from_id=uuid.uuid4(),
            from_ref="SYS-1",
            to_id=uuid.uuid4(),
            relation_type="relates_to",
        )


@pytest.mark.asyncio
async def test_relations_by_requirement_key_and_external_id(monkeypatch):
    calls: dict = {}
    resolved = {"SYS-0001": uuid.uuid4(), "ADO-2": uuid.uuid4()}

    async def fake_resolve(refs):
        return {ref: resolved[ref] for ref in refs}

    async def fake_validate_refs(project_id, ref_ids, label):
        calls["refs"] = ref_ids

    async def fake_submit(**kwargs):
        calls["submit"] = kwargs
        return "ok"

    monkeypatch.setattr(
        write_tools, "_resolve_requirement_refs", fake_resolve
    )
    monkeypatch.setattr(
        write_tools, "_validate_requirement_refs", fake_validate_refs
    )
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    result = await _submit_requirement_relations(
        project_id=None,
        relations=[
            RelationInput(
                from_ref="SYS-0001",
                to_ref="ADO-2",
                relation_type="relates_to",
            )
        ],
        operation_id=None,
    )

    assert result == "ok"
    item = calls["submit"]["items"][0]
    assert item["payload"]["from_ref"] == str(resolved["SYS-0001"])
    assert item["payload"]["to_ref"] == str(resolved["ADO-2"])
    assert set(calls["refs"]) == {str(v) for v in resolved.values()}


@pytest.mark.asyncio
async def test_resolve_requirement_ref_rejects_ambiguous(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_find(session, ref):
        return [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]

    monkeypatch.setattr(write_tools, "readonly_session", fake_session)
    monkeypatch.setattr(write_tools, "find_requirements_by_ref", fake_find)

    with pytest.raises(PayloadValidationError, match="不唯一"):
        await write_tools._resolve_requirement_ref("DUP-1")


@pytest.mark.asyncio
async def test_resolve_requirement_refs_uses_single_query(monkeypatch):
    from types import SimpleNamespace

    from mem_lake.gateway.tools import write_tools as wt

    node_a = SimpleNamespace(
        id=uuid.uuid4(), requirement_key="A-1", properties={"external_id": None}
    )
    node_b = SimpleNamespace(
        id=uuid.uuid4(), requirement_key="B-1", properties={"external_id": "EXT-9"}
    )
    seen: list[list[str]] = []

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_find(session, refs):
        seen.append(list(refs))
        return [node_a, node_b]

    monkeypatch.setattr(wt, "readonly_session", fake_session)
    monkeypatch.setattr(wt, "find_requirements_by_refs", fake_find)

    resolved = await wt._resolve_requirement_refs(["A-1", "EXT-9"])

    assert seen == [["A-1", "EXT-9"]]
    assert resolved == {"A-1": node_a.id, "EXT-9": node_b.id}
