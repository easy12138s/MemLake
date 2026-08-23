"""宽松模式（免审批直接入库）单元测试：分支决策 + 出参 + 依赖读取。

聚焦纯逻辑（无真实 DB）：
- approval.service.submit_batch_with_mode 的分支（宽松/严格/全局熔断）用 mock 依赖验证
- gateway.dependencies.get_current_lax_mode 从 token claims 读取
- _shared.WriteToolOutput 携带 decision

真实 end-to-end（set_mode/list、宽松下发/冲突回退）用 TRAE MCP 与集成测试覆盖。
"""

import pytest
from pydantic import BaseModel


class _FakeToken:
    def __init__(self, claims) -> None:
        self.claims = claims
        self.scopes = []
        self.client_id = "client-id"


class _FakeBatch(BaseModel):
    """足够驱动 from_batch 的最小批对象。"""

    id: str = "9d9f2e3e-0000-4000-8000-000000000001"
    status: str = "approved"
    submitted_at: str = "2026-08-23T00:00:00Z"
    items: list = []


class _FakeDecisionResult(dict):
    pass


@pytest.mark.asyncio
async def test_submit_lax_no_conflict_auto_approved(monkeypatch):
    """宽松 + 无冲突 → decision=auto_approved（调用 auto_process_batch）。"""
    import mem_lake.approval.service as svc

    calls = {}

    async def fake_submit_batch(session, **kw):
        calls["submit"] = kw
        return _FakeBatch()

    async def fake_auto_process(session, **kw):
        calls["auto"] = kw
        return {"decision": "auto_approved"}

    monkeypatch.setattr(svc, "submit_batch", fake_submit_batch)
    monkeypatch.setattr(svc, "auto_process_batch", fake_auto_process)
    monkeypatch.setattr(
        svc, "get_settings", lambda: type("S", (), {"LAX_MODE_ENABLED": True})()
    )

    batch, decision = await svc.submit_batch_with_mode(
        session=None,
        project_id="p1",
        batch_type="publish_requirement",
        submitted_by="ak",
        submitter_role="pm",
        items=[],
        lax_mode=True,
        graph_store=object(),
        embedding_client=object(),
        vector_searcher=object(),
    )
    assert batch.status == "approved"
    assert decision == "auto_approved"
    assert "auto" in calls  # 宽松路径确实调用了 auto_process_batch


@pytest.mark.asyncio
async def test_submit_lax_conflict_needs_human_review(monkeypatch):
    """宽松 + 有冲突 → decision=needs_human_review（批次保持 pending）。"""
    import mem_lake.approval.service as svc

    async def fake_submit_batch(session, **kw):
        return _FakeBatch(status="pending_review")

    async def fake_auto_process(session, **kw):
        return {"decision": "needs_human_review"}

    monkeypatch.setattr(svc, "submit_batch", fake_submit_batch)
    monkeypatch.setattr(svc, "auto_process_batch", fake_auto_process)
    monkeypatch.setattr(
        svc, "get_settings", lambda: type("S", (), {"LAX_MODE_ENABLED": True})()
    )

    batch, decision = await svc.submit_batch_with_mode(
        session=None, project_id="p1", batch_type="publish_requirement",
        submitted_by="ak", submitter_role="pm", items=[], lax_mode=True,
        graph_store=object(), embedding_client=object(), vector_searcher=object(),
    )
    assert decision == "needs_human_review"


@pytest.mark.asyncio
async def test_submit_strict_returns_none_decision(monkeypatch):
    """严格模式（lax_mode=False）→ decision=None，且不调用 auto_process_batch。"""
    import mem_lake.approval.service as svc

    called = {"auto": False}

    async def fake_submit_batch(session, **kw):
        return _FakeBatch(status="pending_review")

    async def fake_auto_process(*a, **kw):
        called["auto"] = True
        return {"decision": "auto_approved"}

    monkeypatch.setattr(svc, "submit_batch", fake_submit_batch)
    monkeypatch.setattr(svc, "auto_process_batch", fake_auto_process)
    monkeypatch.setattr(
        svc, "get_settings", lambda: type("S", (), {"LAX_MODE_ENABLED": True})()
    )

    batch, decision = await svc.submit_batch_with_mode(
        session=None, project_id="p1", batch_type="publish_requirement",
        submitted_by="ak", submitter_role="pm", items=[], lax_mode=False,
    )
    assert decision is None
    assert called["auto"] is False  # 严格模式走审批，不自动处理


@pytest.mark.asyncio
async def test_submit_global_off_forces_strict(monkeypatch):
    """全局 LAX_MODE_ENABLED=false 时即使 lax_mode=True 也回退审批。"""
    import mem_lake.approval.service as svc

    called = {"auto": False}

    async def fake_submit_batch(session, **kw):
        return _FakeBatch(status="pending_review")

    async def fake_auto_process(*a, **kw):
        called["auto"] = True
        return {"decision": "auto_approved"}

    monkeypatch.setattr(svc, "submit_batch", fake_submit_batch)
    monkeypatch.setattr(svc, "auto_process_batch", fake_auto_process)
    monkeypatch.setattr(
        svc, "get_settings", lambda: type("S", (), {"LAX_MODE_ENABLED": False})()
    )

    batch, decision = await svc.submit_batch_with_mode(
        session=None, project_id="p1", batch_type="submit_dev_artifacts",
        submitted_by="ak", submitter_role="dev", items=[], lax_mode=True,
    )
    assert decision is None
    assert called["auto"] is False  # 熔断生效，强制走审批


@pytest.mark.asyncio
async def test_submit_lax_missing_deps_raises(monkeypatch):
    """宽松路径缺依赖（graph_store/embedding/vector 为 None）时应报错。"""
    import mem_lake.approval.service as svc

    async def fake_submit_batch(session, **kw):
        return _FakeBatch()

    monkeypatch.setattr(svc, "submit_batch", fake_submit_batch)
    monkeypatch.setattr(
        svc, "get_settings", lambda: type("S", (), {"LAX_MODE_ENABLED": True})()
    )

    with pytest.raises(RuntimeError):
        await svc.submit_batch_with_mode(
            session=None, project_id="p1", batch_type="publish_requirement",
            submitted_by="ak", submitter_role="pm", items=[], lax_mode=True,
        )


def test_get_current_lax_mode_from_claims(monkeypatch):
    """get_current_lax_mode 应从 token.claims 读取 lax_mode，缺省 False。"""
    import mem_lake.gateway.dependencies as deps

    monkeypatch.setattr(deps, "get_access_token", lambda: _FakeToken({"lax_mode": True}))
    assert deps.get_current_lax_mode() is True

    monkeypatch.setattr(deps, "get_access_token", lambda: _FakeToken({"role": "dev"}))
    assert deps.get_current_lax_mode() is False  # 无 lax_mode → 缺省 False


def test_write_tool_output_carries_decision():
    """WriteToolOutput.from_batch 应透传 decision；strict 无决策。"""
    from mem_lake.gateway.tools._shared import WriteToolOutput

    out = WriteToolOutput.from_batch(_FakeBatch(), decision="auto_approved")
    assert out.status == "approved"
    assert out.decision == "auto_approved"

    out2 = WriteToolOutput.from_batch(_FakeBatch(status="pending_review"))
    assert out2.decision is None
