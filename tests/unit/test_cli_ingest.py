"""ingest 单测：resolve_system 解析 + run_import_batch 批量驱动。

旧单条导入路径（run_import/ingest_requirement/find_requirement_by_source）
已随代码瘦身删除，相关用例一并移除。
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.cli.ingest import resolve_system
from mem_lake.knowledge.models import KnowledgeNode


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _parsed(title: str) -> ParsedRequirement:
    return ParsedRequirement(title=title, content=f"正文 {title}", rel_path=title)


@pytest.mark.asyncio
async def test_resolve_system_missing_raises(monkeypatch):
    """name/code 均未命中时抛 ValueError。"""
    src = iter([_FakeResult(None), _FakeResult(None)])

    async def fake_execute(stmt):
        return next(src)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    with pytest.raises(ValueError):
        await resolve_system(session, name="NOPE", code="NOPE")


@pytest.mark.asyncio
async def test_resolve_system_found(monkeypatch):
    """按 code 命中 System。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="HIS", code="HIS")
    src = iter([_FakeResult(sys_obj)])

    async def fake_execute(stmt):
        return next(src)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    got = await resolve_system(session, code="HIS")
    assert got is sys_obj


@pytest.mark.asyncio
async def test_resolve_system_by_name(monkeypatch):
    """未传 code、只按 name 也能解析（DB system code 为 NULL 的场景）。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="中方诊药云系统", code=None)

    async def fake_execute(stmt):
        return _FakeResult(sys_obj)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    got = await resolve_system(session, name="中方诊药云系统")
    assert got is sys_obj


@pytest.mark.asyncio
async def test_resolve_system_falls_back_to_code(monkeypatch):
    """name 未命中时回退 code。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="HIS", code="HIS")
    src = iter([_FakeResult(None), _FakeResult(sys_obj)])

    async def fake_execute(stmt):
        return next(src)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    got = await resolve_system(session, code="HIS", name="NOPE")
    assert got is sys_obj


_AXURE_LEAF = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>u1234 登录页面</title></head>
<body><!-- 页面描述 --><div class="ax_drop_target">登录表单</div>
<div style="left:20px;"><!-- Start Comments -->
<div class="note" id="u1240">UComment 登录功能需求
<b>用户名、密码、验证码</b></div>
<!-- End Comments --></div></body></html>
"""

def _write_axure(path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ============================================================================
# run_import_batch：批量入库驱动（真实 graph_store + mock_embedding_client）
# ============================================================================


async def _cleanup_batch_scope(*, system_id=None, title_prefix=None):
    """清理 run_import_batch 已 commit 的数据（node/audit/graph/counter/system）。

    run_import_batch 每批 commit，绕过 db_session 的回滚隔离；测试结束后须用
    全新 session 显式清理，避免污染共享测试库。
    """
    from sqlalchemy import delete, select

    from mem_lake.audit.models import AuditLog
    from mem_lake.db.session import AsyncSessionLocal
    from mem_lake.knowledge.age_store import get_graph_store
    from mem_lake.knowledge.models import (
        KnowledgeNode,
        NodeEmbedding,
        RequirementCounter,
        System,
    )

    async with AsyncSessionLocal() as s:
        sel = select(KnowledgeNode).where(KnowledgeNode.type == "Requirement")
        if system_id is not None:
            sel = sel.where(KnowledgeNode.system_id == system_id)
        if title_prefix is not None:
            sel = sel.where(KnowledgeNode.title.like(f"{title_prefix}%"))
        nodes = (await s.execute(sel)).scalars().all()

        graph_store = get_graph_store()
        for n in nodes:
            await graph_store.delete_node(s, n.id)
            await s.execute(delete(AuditLog).where(AuditLog.target_id == n.id))
            await s.execute(delete(NodeEmbedding).where(NodeEmbedding.node_id == n.id))
            await s.execute(delete(KnowledgeNode).where(KnowledgeNode.id == n.id))
        if system_id is not None:
            await s.execute(
                delete(RequirementCounter).where(
                    RequirementCounter.system_id == system_id
                )
            )
            sys_obj = await s.get(System, system_id)
            if sys_obj is not None:
                await s.delete(sys_obj)
        await s.commit()


@pytest.mark.asyncio
async def test_run_import_batch_idempotent_and_skips_existing(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """批量导入：首跑 created=N，二次跑全 skipped（跨批次 source_doc 幂等），key 不重复。"""
    from sqlalchemy import select

    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.knowledge.models import System

    system = System(name=f"HIS-{uuid.uuid4().hex[:8]}", code=f"HIS-{uuid.uuid4().hex[:8]}")
    db_session.add(system)
    await db_session.flush()

    _write_axure(tmp_path / "HIS" / "login.html", _AXURE_LEAF)
    _write_axure(tmp_path / "HIS" / "reg.html", _AXURE_LEAF)
    _write_axure(tmp_path / "HIS" / "pharmacy.html", _AXURE_LEAF)

    kwargs = dict(
        session=db_session,
        project_id=None,
        system=system,
        directory=str(tmp_path),
        adapter="axure",
        embedding_client=mock_embedding_client,
        graph_store=graph_store,
        created_by="cli-import",
        batch_size=50,
    )

    try:
        summary = await run_import_batch(**kwargs)
        assert summary.failed == []
        assert len(summary.created) == 3

        rows = (
            await db_session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.type == "Requirement",
                    KnowledgeNode.system_id == system.id,
                )
            )
        ).scalars().all()
        assert len(rows) == 3
        keys = [r.requirement_key for r in rows]
        assert len(keys) == len(set(keys)), "需求主键不得重复"
        for r in rows:
            assert (r.properties or {}).get("source_doc") == r.title

        # 幂等：二次跑 → created 不增，全 skipped
        summary2 = await run_import_batch(**kwargs)
        assert summary2.created == []
        assert len(summary2.skipped) == 3

        rows2 = (
            await db_session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.type == "Requirement",
                    KnowledgeNode.system_id == system.id,
                )
            )
        ).scalars().all()
        assert len(rows2) == 3  # 未新增
    finally:
        await _cleanup_batch_scope(system_id=system.id)


@pytest.mark.asyncio
async def test_run_import_batch_batches_commits(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """每批提交：完整跑完后，用全新 session 能查到已 commit 的 Requirement 行。"""
    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.db.session import AsyncSessionLocal
    from mem_lake.knowledge.models import System

    system = System(name=f"BATCH-{uuid.uuid4().hex[:8]}", code=f"BX-{uuid.uuid4().hex[:8]}")
    db_session.add(system)
    await db_session.flush()

    _write_axure(tmp_path / "B" / "a.html", _AXURE_LEAF)
    _write_axure(tmp_path / "B" / "b.html", _AXURE_LEAF)
    _write_axure(tmp_path / "B" / "c.html", _AXURE_LEAF)

    try:
        summary = await run_import_batch(
            session=db_session,
            project_id=None,
            system=system,
            directory=str(tmp_path),
            adapter="axure",
            embedding_client=mock_embedding_client,
            graph_store=graph_store,
            created_by="cli-import",
            batch_size=2,
        )
        assert len(summary.created) == 3

        # 全新 session 校验提交已持久化
        async with AsyncSessionLocal() as fresh:
            from sqlalchemy import select

            from mem_lake.knowledge.models import KnowledgeNode

            rows = (
                await fresh.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.type == "Requirement",
                        KnowledgeNode.system_id == system.id,
                    )
                )
            ).scalars().all()
            assert len(rows) == 3
    finally:
        await _cleanup_batch_scope(system_id=system.id)


@pytest.mark.asyncio
async def test_run_import_batch_floating_no_system(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """悬浮 + 无 system：节点 project_id=None && system_id=None，key 不分配。"""
    from sqlalchemy import select

    from mem_lake.cli.ingest import run_import_batch

    _write_axure(tmp_path / "F" / "x.html", _AXURE_LEAF)

    try:
        summary = await run_import_batch(
            session=db_session,
            project_id=None,
            system=None,
            directory=str(tmp_path),
            adapter="axure",
            embedding_client=mock_embedding_client,
            graph_store=graph_store,
            created_by="cli-import",
        )
        assert len(summary.created) == 1

        rows = (
            await db_session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.type == "Requirement",
                    KnowledgeNode.title == "F/x.html",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].project_id is None
        assert rows[0].system_id is None
        assert rows[0].requirement_key is None
    finally:
        await _cleanup_batch_scope(title_prefix="F/")


@pytest.mark.asyncio
async def test_run_import_batch_dedups_same_source_doc_within_run(
    db_session, graph_store, mock_embedding_client
):
    """同一次运行内两个文件映射到同一 source_doc → 第二个 skipped。"""
    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.knowledge.models import System

    system = System(name=f"DEDUP-{uuid.uuid4().hex[:8]}", code=f"DD-{uuid.uuid4().hex[:8]}")
    db_session.add(system)
    await db_session.flush()

    parsed = [
        _parsed("D/x.html"),
        _parsed("D/x.html"),  # 同一 source_doc
        _parsed("D/y.html"),
    ]

    try:
        summary = await run_import_batch(
            session=db_session,
            project_id=None,
            system=system,
            directory="unused",
            parsed=parsed,
            embedding_client=mock_embedding_client,
            graph_store=graph_store,
            created_by="cli-import",
        )
        assert summary.created == ["D/x.html", "D/y.html"]
        assert summary.skipped == ["D/x.html"]
    finally:
        await _cleanup_batch_scope(system_id=system.id)
