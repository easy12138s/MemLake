"""publish_requirements 批量发布核心测试（无 DB）。"""

import uuid
from types import SimpleNamespace

import pytest

from mem_lake.approval.service import PayloadValidationError
from mem_lake.gateway.tools import write_tools
from mem_lake.gateway.tools.write_tools import (
    BatchRequirementInput,
    RequirementInput,
    _publish_requirements_batch,
)


@pytest.fixture(autouse=True)
def _allow_system_write_scope(monkeypatch):
    """鉴权细节由 test_system_write_access.py 覆盖；本文件只测批量语义。"""

    async def _ok(*args, **kwargs):
        return None

    monkeypatch.setattr(write_tools, "validate_system_write_access", _ok)
    monkeypatch.setattr(write_tools, "validate_project_in_system", _ok)



def _item(ref: str, title: str) -> BatchRequirementInput:
    return BatchRequirementInput(
        client_ref=ref,
        requirement=RequirementInput(
            title=title,
            content=f"{title} 内容",
            properties={"priority": "P1", "module": "医保"},
        ),
    )


@pytest.mark.asyncio
async def test_batch_publish_creates_one_batch_per_requirement(monkeypatch):
    calls: list[dict] = []

    async def fake_submit(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            batch_id=uuid.uuid4(),
            status="pending_review",
            mode="strict",
            decision=None,
            conflict_hint=None,
            created=None,
            edges_created=None,
            submission_id=kwargs["submission_id"],
        )

    monkeypatch.setattr(write_tools, "get_current_key_id", lambda: "ak_pm")
    monkeypatch.setattr(write_tools, "validate_project_access", lambda pid: None)
    monkeypatch.setattr(
        write_tools,
        "get_settings",
        lambda: type(
            "S", (), {"MAX_ITEMS_PER_BATCH": 50, "MAX_CONTENT_LENGTH": 10000}
        )(),
    )
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    submission_id = uuid.uuid4()
    out = await _publish_requirements_batch(
        system_id=uuid.uuid4(),
        requirements=[_item("A-1", "需求A"), _item("A-2", "需求B")],
        submission_id=submission_id,
        operation_id="op",
    )

    assert out.submission_id == submission_id
    assert [i.client_ref for i in out.items] == ["A-1", "A-2"]
    assert all(i.error is None for i in out.items)
    assert len(calls) == 2
    assert all(c["submission_id"] == submission_id for c in calls)
    assert [c["operation_id"] for c in calls] == ["op:A-1", "op:A-2"]
    assert all(c["batch_type"] == "publish_requirement" for c in calls)


@pytest.mark.asyncio
async def test_batch_publish_rejects_duplicate_client_ref(monkeypatch):
    async def fake_submit(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("预检失败时不应提交")

    monkeypatch.setattr(write_tools, "get_current_key_id", lambda: "ak_pm")
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    with pytest.raises(PayloadValidationError, match="client_ref"):
        await _publish_requirements_batch(
            system_id=uuid.uuid4(),
            requirements=[_item("dup", "A"), _item("dup", "B")],
            submission_id=None,
            operation_id=None,
        )


@pytest.mark.asyncio
async def test_batch_publish_prevalidates_all_before_submit(monkeypatch):
    submitted = []

    async def fake_submit(**kwargs):  # pragma: no cover - 不应被调用
        submitted.append(kwargs)
        raise AssertionError("预检失败时不应提交")

    monkeypatch.setattr(write_tools, "get_current_key_id", lambda: "ak_pm")
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    bad = BatchRequirementInput(
        client_ref="bad",
        requirement=RequirementInput(
            title="缺 module",
            content="内容",
            properties={"priority": "P1"},
        ),
    )
    with pytest.raises(Exception):
        await _publish_requirements_batch(
            system_id=uuid.uuid4(),
            requirements=[_item("ok", "正常需求"), bad],
            submission_id=None,
            operation_id=None,
        )
    assert submitted == []


@pytest.mark.asyncio
async def test_batch_publish_rejects_intra_batch_duplicates(monkeypatch):
    async def fake_submit(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("重复预检失败时不应提交")

    monkeypatch.setattr(write_tools, "get_current_key_id", lambda: "ak_pm")
    monkeypatch.setattr(write_tools, "submit_write_batch", fake_submit)

    with pytest.raises(PayloadValidationError, match="批内存在重复"):
        await _publish_requirements_batch(
            system_id=uuid.uuid4(),
            requirements=[_item("A", "同一需求"), _item("B", "同一需求")],
            submission_id=None,
            operation_id=None,
        )


@pytest.mark.asyncio
async def test_batch_publish_sanitizes_item_errors(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("secret dsn")

    monkeypatch.setattr(write_tools, "get_current_key_id", lambda: "ak_pm")
    monkeypatch.setattr(write_tools, "submit_write_batch", boom)

    out = await _publish_requirements_batch(
        system_id=uuid.uuid4(),
        requirements=[_item("A", "需求A")],
        submission_id=None,
        operation_id=None,
    )

    assert out.items[0].error == "工具调用失败: RuntimeError"
