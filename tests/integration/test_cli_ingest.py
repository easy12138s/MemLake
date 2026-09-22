"""ingest 集成测试：run_import_batch 批量驱动（依赖真实 DB/AGE）。

FIX-06 单测分层：原依赖 db_session/graph_store 的 run_import_batch 批量用例
由 tests/unit 整体迁移至此；resolve_system 纯逻辑用例留守
tests/unit/test_cli_ingest.py。
"""

import uuid

import pytest

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.knowledge.models import KnowledgeNode


def _parsed(title: str) -> ParsedRequirement:
    return ParsedRequirement(title=title, content=f"正文 {title}", rel_path=title)


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
            # FIX-11：图节点删除方法已移除（软删除模型下图节点随 PG 软删过滤兜底），
            # 测试清理直接 DETACH DELETE 图节点。
            await graph_store._exec_cypher(
                s, "MATCH (n {id: $node_id}) DETACH DELETE n",
                {"node_id": str(n.id)},
            )
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


@pytest.mark.asyncio
async def test_failed_batch_rolls_back_and_later_batch_commits_clean(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """失败路径：第 1 批 embed 失败 rollback，第 2 批成功时不得带上第 1 批数据。

    回归用例：无 rollback 时同一会话内失败批次已 flush 的行会被后续成功批次的
    commit 一并落库（缺向量/图/审计的残缺节点）。
    """
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.knowledge.models import System

    system = System(name=f"FB-{uuid.uuid4().hex[:8]}", code=f"FB-{uuid.uuid4().hex[:8]}")
    db_session.add(system)
    await db_session.flush()

    parsed = [_parsed(f"F/{i}.html") for i in range(3)]  # 3 条，batch_size=2 → 2 批

    # embed 首次调用失败（第 1 批），其后成功
    call_count = {"n": 0}

    async def flaky_embed(texts, prompt=None, prompt_name=None, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("embedding 服务 500")
        return [[0.1] * 1024 for _ in texts]

    mock_embedding_client.embed = AsyncMock(side_effect=flaky_embed)

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
            batch_size=2,
        )
        assert summary.failed == ["F/0.html", "F/1.html"]
        assert summary.created == ["F/2.html"]

        rows = (
            (
                await db_session.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.type == "Requirement",
                        KnowledgeNode.system_id == system.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(r.properties or {}).get("source_doc") for r in rows] == ["F/2.html"]
    finally:
        await _cleanup_batch_scope(system_id=system.id)


@pytest.mark.asyncio
async def test_run_import_batch_prints_progress(
    db_session, graph_store, mock_embedding_client, tmp_path, capsys
):
    """每批 commit 后打印一行进度（实时进度给运维看）。"""
    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.knowledge.models import System

    system = System(name=f"PG-{uuid.uuid4().hex[:8]}", code=f"PG-{uuid.uuid4().hex[:8]}")
    db_session.add(system)
    await db_session.flush()

    parsed = [_parsed(f"P/{i}.html") for i in range(3)]

    try:
        await run_import_batch(
            session=db_session,
            project_id=None,
            system=system,
            directory="unused",
            parsed=parsed,
            embedding_client=mock_embedding_client,
            graph_store=graph_store,
            created_by="cli-import",
            batch_size=2,
        )
        out = capsys.readouterr().out
        # 2 批 → 2 行进度，含已提交节点累计数
        assert out.count("[进度]") == 2
        assert "2/2" in out
    finally:
        await _cleanup_batch_scope(system_id=system.id)


# ============================================================================
# notes 适配器：拆分条目库内建 relates_to 链边
# ============================================================================


def _write_notes_md(path, segments):
    """写 notes 格式 .md（需求点之间以两个空行分隔）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n\n".join(segments), encoding="utf-8")


async def _requirement_relates_edges(db_session, graph_store) -> set:
    """返回 (from_id, to_id) 的 relates_to 有向边集合（AGE，测试断言用）。"""
    from conftest import match_pattern

    rows = await match_pattern(
        graph_store,
        db_session,
        "MATCH (a:Requirement)-[r:relates_to]->(b:Requirement) "
        "RETURN {from_id: a.id, to_id: b.id}",
    )
    return {(r["from_id"], r["to_id"]) for r in rows}


@pytest.mark.asyncio
async def test_run_import_batch_notes_split_creates_chain_edges(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """notes 拆分入库：每段一条 Requirement（title={stem} #N，source_doc={file}#N），
    同文件条目按文档序建 relates_to 链边 n1→n2→n3。"""
    from sqlalchemy import select

    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.knowledge.models import System

    system = System(name=f"NOTES-{uuid.uuid4().hex[:8]}", code=f"NT{uuid.uuid4().hex[:6]}")
    db_session.add(system)
    await db_session.flush()

    _write_notes_md(
        tmp_path / "处方单.md",
        ["需求点一：" + "甲" * 300, "需求点二：" + "乙" * 200, "需求点三"],
    )

    try:
        summary = await run_import_batch(
            session=db_session,
            project_id=None,
            system=system,
            directory=str(tmp_path),
            adapter="notes",
            embedding_client=mock_embedding_client,
            graph_store=graph_store,
            created_by="cli-import",
        )
        assert summary.failed == []
        assert len(summary.created) == 3
        assert summary.edges_created == 2

        rows = (
            (
                await db_session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.type == "Requirement",
                        KnowledgeNode.system_id == system.id,
                    )
                    .order_by(KnowledgeNode.title)
                )
            )
            .scalars()
            .all()
        )
        assert [r.title for r in rows] == ["处方单 #1", "处方单 #2", "处方单 #3"]
        assert [(r.properties or {}).get("source_doc") for r in rows] == [
            "处方单.md#1",
            "处方单.md#2",
            "处方单.md#3",
        ]

        id_of = {r.title: str(r.id) for r in rows}
        want = {
            (id_of["处方单 #1"], id_of["处方单 #2"]),
            (id_of["处方单 #2"], id_of["处方单 #3"]),
        }
        assert want <= await _requirement_relates_edges(db_session, graph_store)
    finally:
        await _cleanup_batch_scope(system_id=system.id)


@pytest.mark.asyncio
async def test_notes_rerun_no_duplicate_edges(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """重跑幂等：节点全 skipped、edges_created=0，链边不重复不新增。"""
    from mem_lake.cli.ingest import run_import_batch
    from mem_lake.knowledge.models import System

    system = System(name=f"NOTES2-{uuid.uuid4().hex[:8]}", code=f"N2{uuid.uuid4().hex[:6]}")
    db_session.add(system)
    await db_session.flush()

    _write_notes_md(
        tmp_path / "登录.md",
        ["段一" + "甲" * 300, "段二" + "乙" * 300, "段三"],
    )
    kwargs = dict(
        session=db_session,
        project_id=None,
        system=system,
        directory=str(tmp_path),
        adapter="notes",
        embedding_client=mock_embedding_client,
        graph_store=graph_store,
        created_by="cli-import",
    )

    try:
        first = await run_import_batch(**kwargs)
        assert len(first.created) == 3
        assert first.edges_created == 2
        before = await _requirement_relates_edges(db_session, graph_store)
        assert len(before) == 2

        second = await run_import_batch(**kwargs)
        assert second.created == []
        assert len(second.skipped) == 3
        assert second.edges_created == 0
        assert await _requirement_relates_edges(db_session, graph_store) == before
    finally:
        await _cleanup_batch_scope(system_id=system.id)
