"""validate_requirements dry-run 核心测试（无 DB）。"""

import uuid
from contextlib import asynccontextmanager

import pytest

from mem_lake.gateway.tools import write_tools
from mem_lake.gateway.tools.write_tools import (
    BatchRequirementInput,
    RequirementInput,
    _validate_requirements_core,
)
from mem_lake.search.fusion import SearchResult


@asynccontextmanager
async def _fake_session():
    yield None


def _item(ref: str, *, props=None) -> BatchRequirementInput:
    return BatchRequirementInput(
        client_ref=ref,
        requirement=RequirementInput(
            title=f"需求{ref}",
            content="内容",
            properties=props or {"priority": "P1", "module": "医保"},
        ),
    )


@pytest.mark.asyncio
async def test_validate_requirements_reports_errors_and_conflicts(monkeypatch):
    existing_id = uuid.uuid4()
    calls = {"hybrid": 0}

    async def fake_hybrid(**kwargs):
        calls["hybrid"] += 1
        return {
            "fused": [
                SearchResult(
                    node_id=existing_id,
                    title="旧版本需求",
                    content="内容",
                    node_type="Requirement",
                    score=0.91,
                    source="fused",
                    properties={},
                    tags=[],
                )
            ]
        }

    async def fake_ready(session, node_ids):
        return {existing_id}

    async def fake_validate_refs(project_id, ref_ids, label):
        return None

    monkeypatch.setattr(write_tools, "readonly_session", _fake_session)
    monkeypatch.setattr(write_tools, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(write_tools, "get_node_ids_with_vectors", fake_ready)
    monkeypatch.setattr(write_tools, "_validate_requirement_refs", fake_validate_refs)
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

    out = await _validate_requirements_core(
        system_id=uuid.uuid4(),
        requirements=[
            _item("good"),
            _item("bad", props={"priority": "P1"}),  # 缺 module
        ],
        threshold=None,
        include_conflicts=True,
        embedding_client=object(),
        graph_store=object(),
    )

    good, bad = out.items
    assert good.ok is True
    assert good.errors == []
    assert len(good.conflicts) == 1
    assert good.conflicts[0]["node_id"] == str(existing_id)
    assert good.conflicts[0]["vector_ready"] is True
    assert good.suggestion == "review"
    assert any("acceptance_criteria" in w for w in good.warnings)
    assert bad.ok is False
    assert bad.errors
    assert bad.conflicts == []
    # 有错误的条目不执行冲突检索
    assert calls["hybrid"] == 1


@pytest.mark.asyncio
async def test_validate_requirements_can_skip_conflicts(monkeypatch):
    called = {"hybrid": False}

    async def fake_hybrid(**kwargs):
        called["hybrid"] = True
        return {"fused": []}

    monkeypatch.setattr(write_tools, "hybrid_search", fake_hybrid)
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

    out = await _validate_requirements_core(
        system_id=uuid.uuid4(),
        requirements=[_item("x")],
        threshold=None,
        include_conflicts=False,
        embedding_client=None,
        graph_store=None,
    )
    assert out.items[0].ok is True
    assert called["hybrid"] is False


def test_requirement_lint_warnings_detect_impl_details():
    from mem_lake.gateway.tools.write_tools import _requirement_lint_warnings

    req = RequirementInput(
        title="需求",
        content="新增数据库表并创建索引，使用幂等键防重",
        properties={"priority": "P1", "module": "m"},
        tags=["待确认"],
    )
    warnings = _requirement_lint_warnings(req)
    assert any("acceptance_criteria" in w for w in warnings)
    assert any("待确认" in w for w in warnings)
    assert any("实现细节" in w for w in warnings)
