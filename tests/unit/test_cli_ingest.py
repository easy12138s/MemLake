"""ingest 单测：source_doc 幂等 + properties/source 映射 + 异常隔离。

run_import 通过注入 fake ingest 验证驱动逻辑（不连 DB）；
ingest_requirement/find_requirement_by_source 的真实调用以集成测试
（db_session + graph_store + mock_embedding_client，见文件末尾 Task 5 用例）验证。
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.cli.ingest import resolve_system, run_import
from mem_lake.knowledge.models import KnowledgeNode


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _parsed(title: str) -> ParsedRequirement:
    return ParsedRequirement(title=title, content=f"正文 {title}", rel_path=title)


@pytest.mark.asyncio
async def test_run_import_aggregates_by_status():
    """run_import 依赖注入 fake ingest，按返回 status 聚合 created/skipped/failed。"""
    seen: list[str] = []

    async def fake_ingest(session, *, graph_store, embedding_client, project_id,
                          system_id, parsed, priority, module, force, actor, created_by):
        seen.append(parsed.rel_path)
        if parsed.rel_path == "a.html":
            raise RuntimeError("模拟解析/入库失败")
        if parsed.rel_path == "b.html":
            return {"status": "created", "title": parsed.title}
        return {"status": "skipped", "title": parsed.title}

    props = dict(
        graph_store=None,
        embedding_client=None,
        project_id=uuid.uuid4(),
        system_id=uuid.uuid4(),
        system_code=None,
        priority="P3",
        module="导入",
        force=False,
        actor="cli",
        created_by="cli",
        ingest=fake_ingest,
    )
    parsed_list = [_parsed("a.html"), _parsed("b.html"), _parsed("c.html")]

    summary = await run_import(None, parsed_list=parsed_list, dry_run=False, **props)

    assert summary.created == ["b.html"]
    assert summary.skipped == ["c.html"]
    assert summary.failed == ["a.html"]
    assert seen == ["a.html", "b.html", "c.html"]  # 失败不中断其余


@pytest.mark.asyncio
async def test_run_import_dry_run_does_not_ingest():
    """dry_run=True 时不调用 ingest，只产出待导入清单（parsed 明细）。"""
    calls: list[str] = []

    async def fake_ingest(*args, **kwargs):
        calls.append("ingest")
        return {"status": "created", "title": ""}

    summary = await run_import(
        None,
        parsed_list=[_parsed("a.html"), _parsed("b.html")],
        project_id=uuid.uuid4(),
        system_id=uuid.uuid4(),
        system_code=None,
        priority="P3",
        module="导入",
        force=False,
        dry_run=True,
        actor="cli",
        created_by="cli",
        ingest=fake_ingest,
    )
    assert calls == []
    assert {"a.html", "b.html"} == {p.title for p in summary.pending}


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


# ============================================================================
# 集成测试：真实 create_node/update_node + source_doc 幂等
# （依赖 db_session/graph_store/mock_embedding_client fixture，需本地 postgres 在线）
# ============================================================================


@pytest.mark.asyncio
async def test_ingest_requirement_create_then_skip_then_force_update(
    db_session, graph_store, mock_embedding_client
):
    """create → skip（幂等）→ force 更新，source_doc 不重复。"""
    from sqlalchemy import select

    from mem_lake.cli.ingest import ingest_requirement
    from mem_lake.knowledge.models import System

    system = System(name="HIS", code="HIS")
    db_session.add(system)
    await db_session.flush()

    project_id = uuid.uuid4()
    parsed = ParsedRequirement(
        title="HIS/auth/login.html", content="实现 JWT 登录", rel_path="HIS/auth/login.html"
    )
    kwargs = dict(
        graph_store=graph_store,
        embedding_client=mock_embedding_client,
        project_id=project_id,
        system_id=system.id,
        priority="P3",
        module="导入",
        actor="cli-import",
        created_by="cli-import",
    )

    # 1) create
    r1 = await ingest_requirement(db_session, parsed=parsed, force=False, **kwargs)
    assert r1["status"] == "created"

    # 2) 幂等：再跑一次 → skipped，不新增
    r2 = await ingest_requirement(db_session, parsed=parsed, force=False, **kwargs)
    assert r2["status"] == "skipped"

    # 3) force → updated，内容更新、source_doc 保留
    parsed_updated = ParsedRequirement(
        title="HIS/auth/login.html", content="实现 JWT 登录(改)", rel_path="HIS/auth/login.html"
    )
    r3 = await ingest_requirement(db_session, parsed=parsed_updated, force=True, **kwargs)
    assert r3["status"] == "updated"

    rows = (
        await db_session.execute(
            select(KnowledgeNode).where(KnowledgeNode.type == "Requirement")
        )
    ).scalars().all()
    reqs = [r for r in rows if (r.properties or {}).get("source_doc") == "HIS/auth/login.html"]
    assert len(reqs) == 1
    assert reqs[0].content == "实现 JWT 登录(改)"
    assert reqs[0].properties["priority"] == "P3"
    assert reqs[0].properties["module"] == "导入"


@pytest.mark.asyncio
async def test_find_requirement_by_source_filters_by_project_and_system(
    db_session, graph_store, mock_embedding_client
):
    """source_doc 匹配限定 project+system，跨 system 不算重复。"""
    from mem_lake.cli.ingest import find_requirement_by_source, ingest_requirement
    from mem_lake.knowledge.models import System

    sys_a = System(name="A", code="A")
    sys_b = System(name="B", code="B")
    db_session.add_all([sys_a, sys_b])
    await db_session.flush()

    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    parsed = ParsedRequirement(title="x.html", content="c", rel_path="x.html")

    for pid, sid in [(proj_a, sys_a.id), (proj_b, sys_b.id)]:
        await ingest_requirement(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=pid,
            system_id=sid,
            parsed=parsed,
            priority="P3",
            module="导入",
            force=False,
            actor="cli",
            created_by="cli",
        )
        await db_session.flush()

    found_a = await find_requirement_by_source(
        db_session, project_id=proj_a, system_id=sys_a.id, source_doc="x.html"
    )
    found_b = await find_requirement_by_source(
        db_session, project_id=proj_b, system_id=sys_b.id, source_doc="x.html"
    )
    assert found_a is not None and found_b is not None
    assert found_a.id != found_b.id  # 各自独立，不互相视为重复


@pytest.mark.asyncio
async def test_ingest_requirement_floating_project_none(
    db_session, graph_store, mock_embedding_client
):
    """悬浮需求（project_id=None）：创建 → 幂等 skip，source_doc 保留。"""
    from sqlalchemy import select

    from mem_lake.cli.ingest import ingest_requirement
    from mem_lake.knowledge.models import System

    system = (
        await db_session.execute(select(System).where(System.name == "中方诊药云系统"))
    ).scalar_one_or_none()
    if system is None:
        system = System(name="中方诊药云系统", code=None)
        db_session.add(system)
        await db_session.flush()

    rel_path = "云HIS-一期/登录页面.html"
    parsed = ParsedRequirement(
        title=rel_path, content="实现账号密码登录与验证码", rel_path=rel_path
    )
    kwargs = dict(
        graph_store=graph_store,
        embedding_client=mock_embedding_client,
        project_id=None,
        system_id=system.id,
        priority="P3",
        module="云HIS",
        actor="cli-import",
        created_by="cli-import",
    )

    r1 = await ingest_requirement(db_session, parsed=parsed, force=False, **kwargs)
    assert r1["status"] == "created"

    rows = (
        await db_session.execute(select(KnowledgeNode).where(KnowledgeNode.type == "Requirement"))
    ).scalars().all()
    reqs = [r for r in rows if (r.properties or {}).get("source_doc") == rel_path]
    assert len(reqs) == 1
    assert reqs[0].project_id is None
    assert reqs[0].system_id == system.id
    assert reqs[0].properties["source_doc"] == rel_path

    r2 = await ingest_requirement(db_session, parsed=parsed, force=False, **kwargs)
    assert r2["status"] == "skipped"

    rows2 = (
        await db_session.execute(select(KnowledgeNode).where(KnowledgeNode.type == "Requirement"))
    ).scalars().all()
    reqs2 = [r for r in rows2 if (r.properties or {}).get("source_doc") == rel_path]
    assert len(reqs2) == 1


_AXURE_LEAF = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>u1234 登录页面</title></head>
<body><!-- 页面描述 --><div class="ax_drop_target">登录表单</div>
<div style="left:20px;"><!-- Start Comments -->
<div class="note" id="u1240">UComment 登录功能需求
<b>用户名、密码、验证码</b></div>
<!-- End Comments --></div></body></html>
"""

_AXURE_INDEX = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>云HIS 导航</title></head>
<body><div class="ax_drop_target">导航壳</div></body></html>
"""


def _write_axure(path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_run_import_yun_his_floating_from_real_dir(
    db_session, graph_store, mock_embedding_client, tmp_path
):
    """CLI E2E：AxureCleanedAdapter 清洗 → extract_directory 版本目录遍历 → run_import 悬浮入库。"""
    from sqlalchemy import select

    from mem_lake.cli.adapters import AxureCleanedAdapter
    from mem_lake.cli.extractor import extract_directory
    from mem_lake.cli.ingest import run_import
    from mem_lake.knowledge.models import System

    _write_axure(tmp_path / "云HIS-一期" / "login.html", _AXURE_LEAF)
    _write_axure(tmp_path / "云HIS-一期" / "medical.html", _AXURE_LEAF)
    _write_axure(tmp_path / "云HIS-一期" / "index.html", _AXURE_INDEX)
    _write_axure(tmp_path / "云HIS-二期" / "pharmacy.html", _AXURE_LEAF)
    _write_axure(tmp_path / "云HIS-二期" / "billing.html", _AXURE_LEAF)
    _write_axure(tmp_path / "云HIS-二期" / "index.html", _AXURE_INDEX)
    parsed_list = extract_directory(tmp_path, adapter=AxureCleanedAdapter())
    rel_paths = {p.rel_path for p in parsed_list}
    # index 导航壳被 accepts 排除，仅 4 个叶子页
    assert rel_paths == {
        "云HIS-一期/login.html",
        "云HIS-一期/medical.html",
        "云HIS-二期/pharmacy.html",
        "云HIS-二期/billing.html",
    }

    system = (
        await db_session.execute(select(System).where(System.name == "中方诊药云系统"))
    ).scalar_one_or_none()
    if system is None:
        system = System(name="中方诊药云系统", code=None)
        db_session.add(system)
        await db_session.flush()

    kwargs = dict(
        session=db_session,
        graph_store=graph_store,
        embedding_client=mock_embedding_client,
        project_id=None,
        system_id=system.id,
        system_code=None,
        parsed_list=parsed_list,
        priority="P3",
        module="云HIS",
        force=False,
        dry_run=False,
        actor="cli-import",
        created_by="cli-import",
    )

    summary = await run_import(**kwargs)
    assert summary.failed == []
    assert len(summary.created) == 4
    assert set(summary.created) == rel_paths

    rows = (
        await db_session.execute(select(KnowledgeNode).where(KnowledgeNode.type == "Requirement"))
    ).scalars().all()
    reqs = [r for r in rows if (r.properties or {}).get("source_doc") in rel_paths]
    assert len(reqs) == 4
    for r in reqs:
        assert r.project_id is None
        assert r.system_id == system.id
        assert r.properties["module"] == "云HIS"
        assert r.properties["priority"] == "P3"
        assert r.properties["source_doc"] == r.title
        assert "/" in r.properties["source_doc"]  # 版本前缀相对 posix 路径

    # 幂等：再次 run_import → 全部 skipped，created 为空
    summary2 = await run_import(**kwargs)
    assert summary2.created == []
    assert len(summary2.skipped) == 4
    assert set(summary2.skipped) == rel_paths


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
