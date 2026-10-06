"""可视化控制台批次二集成测试：图页数据层直测 + 三端点（真实 DB）。

端点级断言对共享库只做形状（生产数据不可控）；数据层（list_graph_nodes /
list_embedded_node_ids / node_has_embedding）用 db_session 事务回滚隔离，
过滤全部用新鲜 system_id 收敛，断言不依赖共享库现状、不留种子。
"""

import uuid

import httpx
import pytest
from starlette.applications import Starlette

from mem_lake.config import Settings
from mem_lake.knowledge.models import KnowledgeNode, NodeEmbedding
from mem_lake.knowledge.repository import (
    list_embedded_node_ids,
    list_graph_nodes,
    node_has_embedding,
)
from mem_lake.visual import create_visual_app


def make_client(**overrides) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="it-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _node(
    title: str,
    *,
    system_id: uuid.UUID | None = None,
    status: str = "approved",
    deleted: bool = False,
    requirement_key: str | None = None,
) -> KnowledgeNode:
    """构造图页直测用 KnowledgeNode（db_session 回滚隔离，不留种子）。"""
    return KnowledgeNode(
        type="Requirement",
        title=title,
        content="图页直测正文",
        properties={"module": "graph-test"},
        status=status,
        created_by="visual-graph-test",
        system_id=system_id,
        requirement_key=requirement_key,
        is_deleted=deleted,
    )


async def test_list_graph_nodes_default_and_status(db_session):
    """默认仅 approved（不含软删除）；archived 单查；None 全量。"""
    sid = uuid.uuid4()
    db_session.add(_node("特测-生效", system_id=sid))
    db_session.add(_node("特测-归档", system_id=sid, status="archived", deleted=True))
    db_session.add(_node("特测-其他域", system_id=uuid.uuid4()))
    await db_session.flush()

    rows, truncated = await list_graph_nodes(db_session, system_id=sid, limit=10)
    assert truncated is False
    assert [r.title for r in rows] == ["特测-生效"]

    rows, _ = await list_graph_nodes(db_session, system_id=sid, status="archived", limit=10)
    assert [r.title for r in rows] == ["特测-归档"]

    rows, _ = await list_graph_nodes(db_session, system_id=sid, status=None, limit=10)
    assert {r.title for r in rows} == {"特测-生效", "特测-归档"}


async def test_list_graph_nodes_type_and_q(db_session):
    """node_types 白名单过滤 + q 命中 title / requirement_key。"""
    sid = uuid.uuid4()
    db_session.add(_node("登录需求特测", system_id=sid))
    db_session.add(_node("另一标题", system_id=sid, requirement_key="ZZQG-0001"))
    db_session.add(
        KnowledgeNode(
            type="CodeSnippet",
            title="登录需求特测",
            content="c",
            properties={},
            status="approved",
            created_by="visual-graph-test",
            system_id=sid,
        )
    )
    await db_session.flush()

    rows, _ = await list_graph_nodes(
        db_session, system_id=sid, node_types=("CodeSnippet",), limit=10
    )
    assert {r.type for r in rows} == {"CodeSnippet"}

    rows, _ = await list_graph_nodes(db_session, system_id=sid, q="登录需求特测", limit=10)
    assert len(rows) == 2  # Requirement 与 CodeSnippet 同名各一

    rows, _ = await list_graph_nodes(db_session, system_id=sid, q="ZZQG-0001", limit=10)
    assert [r.requirement_key for r in rows] == ["ZZQG-0001"]


async def test_list_graph_nodes_truncated(db_session):
    """limit+1 探测截断：2 条 limit=1 → truncated=True、返回 1 条。"""
    sid = uuid.uuid4()
    db_session.add(_node("截断A", system_id=sid))
    db_session.add(_node("截断B", system_id=sid))
    await db_session.flush()
    rows, truncated = await list_graph_nodes(db_session, system_id=sid, limit=1)
    assert truncated is True
    assert len(rows) == 1


async def test_list_graph_nodes_invalid_params(db_session):
    """非法 status 抛 ValueError；非法 node_types 经 FilterSpec 白名单抛 ValueError。"""
    with pytest.raises(ValueError, match="非法 status"):
        await list_graph_nodes(db_session, status="bogus", limit=10)
    with pytest.raises(ValueError, match="非法节点类型"):
        await list_graph_nodes(db_session, node_types=("NotAType",), limit=10)


async def test_node_has_embedding_public(db_session):
    """node_has_embedding 公共化：以 node_embedding 记录存在性判定。"""
    node = _node("向量就绪特测")
    db_session.add(node)
    await db_session.flush()
    assert await node_has_embedding(db_session, node.id) is False
    db_session.add(NodeEmbedding(node_id=node.id, facet="content"))
    await db_session.flush()
    assert await node_has_embedding(db_session, node.id) is True


async def test_list_embedded_node_ids(db_session):
    """批量向量就绪查询：命中集合 + 空列表短路。"""
    a, b = _node("批量向量A"), _node("批量向量B")
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add(NodeEmbedding(node_id=a.id, facet="content"))
    await db_session.flush()
    assert await list_embedded_node_ids(db_session, node_ids=[a.id, b.id]) == {a.id}
    assert await list_embedded_node_ids(db_session, node_ids=[]) == set()
