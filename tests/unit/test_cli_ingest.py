"""ingest 单测：source_doc 幂等 + properties/source 映射 + 异常隔离。

run_import 通过注入 fake ingest 验证驱动逻辑（不连 DB）；
ingest_requirement/find_requirement_by_source 的真实调用以集成测试
（db_session + graph_store + mock_embedding_client，见文件末尾 Task 5 用例）验证。
"""

import uuid

import pytest

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.cli.ingest import run_import
from mem_lake.knowledge.models import KnowledgeNode


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
