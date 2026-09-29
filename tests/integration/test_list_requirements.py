"""list_requirements 集成测试：清单式枚举（反馈 ISSUE-01，批次三）。

repository.list_requirements 过滤矩阵：module 精确 / source_doc 前缀 / tags /
分页 total / system 维度。工具层 scope 兜底由 e2e 协作测试覆盖。
"""

import uuid

import pytest

from mem_lake.knowledge.models import KnowledgeNode, System
from mem_lake.knowledge.repository import create_node, list_requirements


async def _seed_req(session, graph_store, emb, helpers, *, pid, sid, title, module, source_doc):
    return await create_node(
        session,
        graph_store=graph_store,
        embedding_client=emb,
        project_id=pid,
        node_type="Requirement",
        title=title,
        content=f"{title} 正文",
        properties={"priority": "P2", "module": module, "source_doc": source_doc},
        tags=[module],
        created_by="ak_pm",
        system_id=sid,
    )


@pytest.fixture
async def seeded(db_session, graph_store, mock_embedding_client, knowledge_helpers):
    """种子：2 个 system；药库1条 + 收费2条；外加 1 个 CodeSnippet（不应出现在枚举结果）。"""
    pid = uuid.uuid4()
    sys_a = System(name=f"LA-{uuid.uuid4().hex[:6]}", code=f"LA{uuid.uuid4().hex[:4]}")
    sys_b = System(name=f"LB-{uuid.uuid4().hex[:6]}", code=f"LB{uuid.uuid4().hex[:4]}")
    db_session.add_all([sys_a, sys_b])
    await db_session.flush()

    n1 = await _seed_req(db_session, graph_store, mock_embedding_client, knowledge_helpers,
                         pid=pid, sid=sys_a.id, title="药库采购", module="药库", source_doc="a/purchase.md")
    n2 = await _seed_req(db_session, graph_store, mock_embedding_client, knowledge_helpers,
                         pid=pid, sid=sys_a.id, title="门诊收费", module="收费", source_doc="b/bill.md")
    n3 = await _seed_req(db_session, graph_store, mock_embedding_client, knowledge_helpers,
                         pid=pid, sid=sys_b.id, title="欠费账单", module="收费", source_doc="b/arrears.md")
    code = await create_node(
        db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
        project_id=pid, node_type="CodeSnippet", title="BillingService",
        content="计费服务", properties=knowledge_helpers["CodeSnippet"](),
        tags=["收费"], created_by="ak_dev",
    )
    await db_session.commit()
    yield {"pid": pid, "sys_a": sys_a, "sys_b": sys_b, "nodes": [n1, n2, n3], "code": code}
    from sqlalchemy import delete

    from mem_lake.audit.models import AuditLog
    from mem_lake.knowledge.models import NodeEmbedding

    ids = [n.id for n in [n1, n2, n3]] + [code.id]
    for nid in ids:
        await graph_store._exec_cypher(
            db_session, "MATCH (n {id: $nid}) DETACH DELETE n", {"nid": str(nid)})
    await db_session.execute(delete(NodeEmbedding).where(NodeEmbedding.node_id.in_(ids)))
    await db_session.execute(delete(AuditLog).where(AuditLog.target_id.in_(ids)))
    await db_session.execute(delete(KnowledgeNode).where(KnowledgeNode.id.in_(ids)))
    await db_session.delete(sys_a)
    await db_session.delete(sys_b)
    await db_session.commit()


async def test_list_all_with_total(db_session, seeded):
    """无过滤：total=3（仅 Requirement），items 不含 CodeSnippet。"""
    rows, total = await list_requirements(db_session, project_id=seeded["pid"])
    assert total == 3
    assert len(rows) == 3
    assert seeded["code"].id not in {r.id for r in rows}


async def test_pagination(db_session, seeded):
    """分页：limit/offset 生效，total 恒为命中总数。"""
    rows, total = await list_requirements(db_session, project_id=seeded["pid"], limit=2)
    assert total == 3 and len(rows) == 2
    rows2, total2 = await list_requirements(
        db_session, project_id=seeded["pid"], limit=2, offset=2)
    assert total2 == 3 and len(rows2) == 1


async def test_module_exact_filter(db_session, seeded):
    """module 精确过滤：收费 → 2 条（跨 system 均命中）。"""
    rows, total = await list_requirements(db_session, project_id=seeded["pid"], module="收费")
    assert total == 2
    assert {r.title for r in rows} == {"门诊收费", "欠费账单"}


async def test_source_doc_prefix_filter(db_session, seeded):
    """source_doc 前缀过滤：b/ → 2 条；a/ → 1 条。"""
    _, total_b = await list_requirements(db_session, project_id=seeded["pid"], source_doc_prefix="b/")
    assert total_b == 2
    _, total_a = await list_requirements(db_session, project_id=seeded["pid"], source_doc_prefix="a/")
    assert total_a == 1


async def test_system_dimension(db_session, seeded):
    """system 维度：sys_a → 2 条；sys_b → 1 条（悬浮跨 project 全系统枚举）。"""
    _, total_a = await list_requirements(db_session, system_id=seeded["sys_a"].id)
    assert total_a == 2
    _, total_b = await list_requirements(db_session, system_id=seeded["sys_b"].id)
    assert total_b == 1


async def test_tags_filter(db_session, seeded):
    """tags 过滤复用 FilterSpec 语义：tags=['收费'] → 2 条。"""
    rows, total = await list_requirements(
        db_session, project_id=seeded["pid"], tags=("收费",))
    assert total == 2
    assert {r.title for r in rows} == {"门诊收费", "欠费账单"}


async def test_combined_filters(db_session, seeded):
    """组合过滤：system_b + module=收费 → 1 条（欠费账单）。"""
    rows, total = await list_requirements(
        db_session, system_id=seeded["sys_b"].id, module="收费")
    assert total == 1 and rows[0].title == "欠费账单"
