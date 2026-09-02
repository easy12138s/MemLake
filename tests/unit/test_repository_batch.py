"""batch_insert_requirements 单测：批量 embed + 主键分配 + 建图 + 审计。

依赖 db_session/graph_store（真实 AGE），embedding 用 _FakeEmbed 记录调用次数，
验证批量路径确实一次 embed 汇聚多段文本（而非逐节点 embed_one）。
"""

import uuid

import pytest
from sqlalchemy import select

from mem_lake.knowledge.models import KnowledgeNode, NodeEmbedding, RequirementCounter, System
from mem_lake.knowledge.repository import batch_insert_requirements


class _FakeEmbed:
    """记录每次 embed 的 texts 数量，并返回等长 1024 维向量。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def embed(self, texts, prompt=None, prompt_name=None):
        self.calls.append(len(texts))
        return [[0.1] * 1024 for _ in texts]


def _requirement(title: str, requirement_key: str | None = None) -> KnowledgeNode:
    return KnowledgeNode(
        type="Requirement",
        title=title,
        content=f"正文 {title}",
        properties={
            "priority": "P3",
            "module": "云HIS",
            "acceptance_criteria": [f"AC for {title}"],
            "source_doc": f"HIS/{title}.html",
        },
        tags=[],
        source={},
        requirement_key=requirement_key,
        system_id=uuid.uuid4(),
        status="approved",
        version=1,
        created_by="cli-import",
    )


@pytest.mark.asyncio
async def test_batch_insert_batches_embeds_and_creates(db_session, graph_store):
    code = f"HIS-{uuid.uuid4().hex[:8]}"
    system = System(name=code, code=code)
    db_session.add(system)
    await db_session.flush()

    nodes = [
        _requirement("loginar", "HIS-0001"),
        _requirement("registration", "HIS-0002"),
        _requirement("billing", "HIS-0003"),
    ]
    for n in nodes:
        n.system_id = system.id

    embed = _FakeEmbed()
    result = await batch_insert_requirements(
        db_session,
        graph_store=graph_store,
        embedding_client=embed,
        nodes=nodes,
        actor="cli-import",
        system_id=system.id,
    )

    assert result == {"created": 3}
    # 至少 2 次 embed 调用：一次主向量 + 一次 facet 向量
    assert len(embed.calls) >= 2
    # 批量：某次调用一次收到全部 3 段文本（主向量）
    assert max(embed.calls) >= 3

    reqs = (
        (await db_session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.type == "Requirement",
                KnowledgeNode.system_id == system.id,
            )
        ))
        .scalars()
        .all()
    )
    assert len(reqs) == 3
    keys = {r.requirement_key for r in reqs}
    assert keys == {"HIS-0001", "HIS-0002", "HIS-0003"}
    for r in reqs:
        assert r.content_vector is not None
        assert len(r.content_vector) == 1024

    facets = (
        await db_session.execute(select(NodeEmbedding).where(NodeEmbedding.node_id.in_([n.id for n in reqs])))
    ).scalars().all()
    # 每节点至少 content facet → >= 3 行
    assert len(facets) >= 3

    from mem_lake.audit.models import AuditLog

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.target_type == "node")
        )
    ).scalars().all()
    # 仅断言本批新增（>=3），不依赖全局计数
    assert len(audits) >= 3


@pytest.mark.asyncio
async def test_batch_insert_requirement_key_sequence(db_session, graph_store):
    code = f"HIS-{uuid.uuid4().hex[:8]}"
    system = System(name=code, code=code)
    db_session.add(system)
    await db_session.flush()

    nodes = [
        _requirement("loginar", requirement_key=None),
        _requirement("registration", requirement_key=None),
    ]
    for n in nodes:
        n.system_id = system.id

    embed = _FakeEmbed()
    await batch_insert_requirements(
        db_session,
        graph_store=graph_store,
        embedding_client=embed,
        nodes=nodes,
        actor="cli-import",
        allocate_requirement_key=True,
        system_id=system.id,
    )

    reqs = (
        (await db_session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.type == "Requirement",
                KnowledgeNode.system_id == system.id,
            )
        ))
        .scalars()
        .all()
    )
    keys = sorted(r.requirement_key for r in reqs)
    assert keys == [f"{code.upper()}-0001", f"{code.upper()}-0002"]

    counter = await db_session.get(RequirementCounter, system.id)
    assert counter is not None
    assert counter.last_value >= 2


@pytest.mark.asyncio
async def test_batch_insert_empty_nodes(db_session, graph_store):
    """空节点列表直接返回 0，不触发 embed/写库。"""
    embed = _FakeEmbed()
    result = await batch_insert_requirements(
        db_session,
        graph_store=graph_store,
        embedding_client=embed,
        nodes=[],
        actor="cli",
    )
    assert result == {"created": 0}
    assert embed.calls == []


@pytest.mark.asyncio
async def test_batch_insert_requires_system_id_for_key_alloc(db_session, graph_store):
    """allocate_requirement_key=True 但未给 system_id 时抛 ValueError。"""
    from mem_lake.knowledge.repository import batch_insert_requirements as fn

    embed = _FakeEmbed()
    with pytest.raises(ValueError):
        await fn(
            db_session,
            graph_store=graph_store,
            embedding_client=embed,
            nodes=[_requirement("x")],
            actor="cli",
            allocate_requirement_key=True,
            system_id=None,
        )
