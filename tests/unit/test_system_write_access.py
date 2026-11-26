"""system 写鉴权与 project∈system 一致性测试（无 DB）。"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from mem_lake.gateway import dependencies
from mem_lake.gateway.tools import write_tools
from mem_lake.gateway.tools.write_tools import (
    BatchRequirementInput,
    RequirementInput,
    _publish_requirements_batch,
    _validate_requirements_core,
)
from mem_lake.knowledge import repository as knowledge_repository


def _token(claims: dict):
    return SimpleNamespace(claims=claims, scopes=[claims.get("role", "")])


class _ExistingSession:
    async def get(self, model, pk):
        return object()


@asynccontextmanager
async def _fake_session():
    yield _ExistingSession()


async def _fake_get_system_ids_by_projects(session, *, project_ids):
    return {uuid.UUID("11111111-1111-1111-1111-111111111111")}


async def _fake_get_system_project_ids(session, *, system_id):
    return [uuid.UUID("22222222-2222-2222-2222-222222222222")]


@pytest.mark.asyncio
async def test_admin_has_no_system_restriction(monkeypatch):
    monkeypatch.setattr(dependencies, "get_access_token", lambda: _token({"role": "admin"}))

    assert await dependencies.get_allowed_write_system_ids() is None


@pytest.mark.asyncio
async def test_pm_allowed_systems_include_project_systems(monkeypatch):
    system_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    project_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    monkeypatch.setattr(
        dependencies,
        "get_access_token",
        lambda: _token(
            {"role": "pm", "system_scope": [str(system_id)], "project_scope": [str(project_id)]}
        ),
    )
    monkeypatch.setattr(dependencies, "readonly_session", _fake_session)
    monkeypatch.setattr(
        knowledge_repository, "get_system_ids_by_projects", _fake_get_system_ids_by_projects
    )

    allowed = await dependencies.get_allowed_write_system_ids()

    assert allowed == {
        system_id,
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
    }


@pytest.mark.asyncio
async def test_validate_system_write_access_rejects_out_of_scope(monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "get_access_token",
        lambda: _token({"role": "pm", "system_scope": []}),
    )
    monkeypatch.setattr(dependencies, "readonly_session", _fake_session)
    monkeypatch.setattr(knowledge_repository, "get_system_ids_by_projects", _fake_get_system_ids_by_projects)

    with pytest.raises(ToolError, match="不在当前 Access Key 的 system 范围内"):
        await dependencies.validate_system_write_access(uuid.uuid4())


@pytest.mark.asyncio
async def test_validate_system_write_access_rejects_missing_system(monkeypatch):
    class _MissingSession:
        async def get(self, model, pk):
            return None

    @asynccontextmanager
    async def _missing_session():
        yield _MissingSession()

    monkeypatch.setattr(
        dependencies,
        "get_access_token",
        lambda: _token({"role": "pm", "system_scope": []}),
    )
    monkeypatch.setattr(dependencies, "readonly_session", _missing_session)

    with pytest.raises(ToolError, match="system 不存在"):
        await dependencies.validate_system_write_access(uuid.uuid4())


@pytest.mark.asyncio
async def test_validate_project_in_system(monkeypatch):
    monkeypatch.setattr(dependencies, "readonly_session", _fake_session)
    monkeypatch.setattr(knowledge_repository, "get_system_project_ids", _fake_get_system_project_ids)

    system_id = uuid.uuid4()
    member = uuid.UUID("22222222-2222-2222-2222-222222222222")
    await dependencies.validate_project_in_system(system_id, member)

    with pytest.raises(ToolError, match="未挂载到 system"):
        await dependencies.validate_project_in_system(system_id, uuid.uuid4())


def _item(ref: str) -> BatchRequirementInput:
    return BatchRequirementInput(
        client_ref=ref,
        requirement=RequirementInput(
            title=f"需求{ref}",
            content="内容",
            properties={"priority": "P1", "module": "医保"},
        ),
    )


def _patch_common(monkeypatch):
    monkeypatch.setattr(write_tools, "validate_project_access", lambda pid: None)
    monkeypatch.setattr(
        write_tools,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "MAX_ITEMS_PER_BATCH": 50,
                "MAX_CONTENT_LENGTH": 10000,
                "CONFLICT_SIMILARITY_THRESHOLD": 0.85,
                "CONFLICT_SUGGEST_PENDING": 0.95,
            },
        )(),
    )


@pytest.mark.asyncio
async def test_validate_requirements_collects_system_scope_error(monkeypatch):
    _patch_common(monkeypatch)
    called = {"hybrid": False}

    async def _deny(system_id):
        raise ToolError("权限拒绝：system 不在当前 Access Key 的 system 范围内")

    async def fake_hybrid(**kwargs):  # pragma: no cover - 不应被调用
        called["hybrid"] = True
        return {"fused": []}

    monkeypatch.setattr(write_tools, "validate_system_write_access", _deny)
    monkeypatch.setattr(write_tools, "hybrid_search", fake_hybrid)

    out = await _validate_requirements_core(
        system_id=uuid.uuid4(),
        requirements=[_item("x")],
        threshold=None,
        include_conflicts=True,
        embedding_client=object(),
        graph_store=object(),
    )

    assert out.items[0].ok is False
    assert any("system 范围内" in e for e in out.items[0].errors)
    assert called["hybrid"] is False


@pytest.mark.asyncio
async def test_publish_requirements_rejects_out_of_scope_system(monkeypatch):
    _patch_common(monkeypatch)

    async def _deny(system_id):
        raise ToolError("权限拒绝：system 不在当前 Access Key 的 system 范围内")

    monkeypatch.setattr(write_tools, "validate_system_write_access", _deny)
    monkeypatch.setattr(write_tools, "get_current_key_id", lambda: "ak_pm")

    with pytest.raises(ToolError, match="system 范围内"):
        await _publish_requirements_batch(
            system_id=uuid.uuid4(),
            requirements=[_item("x")],
            submission_id=None,
            operation_id=None,
        )
