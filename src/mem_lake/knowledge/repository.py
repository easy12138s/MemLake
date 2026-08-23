"""知识图谱 Repository：节点 CRUD + 边 CRUD + 事务性共写 + 审计。

职责边界：
- 仅封装"PG 关系表（knowledge_node）+ AGE 图（节点/边）+ 审计日志"在同一 AsyncSession
  事务内的原子写入。session 不 commit，由调用方（approval 模块或 gateway）控制提交。
- 节点写入前调用 schema.validate_node 校验类型与必填字段，不合规抛 SchemaValidationError。
- 边写入前调用 schema.validate_edge_type 校验类型。
- 向量生成委托给 EmbeddingClient，向量延迟生成策略由调用方决定（直接 approved 场景同步生成；
  审批流场景审批通过时再调用 regenerate_vector）。
- 图操作委托给 GraphStore 抽象，AGEGraphStore 为 v1.0 实现。
- 审计写入委托给 audit.service.write_audit_log，与业务操作同事务。
- RLS 上下文（project_id/actor）由调用方在事务前注入（auth/rls.py）。

设计权衡：
- 不在 repository 内 commit，保证审批流可整体回滚。
- get_node 返回 ORM 对象（而非 dict），保留懒加载与类型提示。
- 软删除（is_deleted=True + status=archived）替代物理删除，保留审计可追溯。
- update_node 不修改 type 字段（节点类型不可变更，避免图谱与关系表不一致）。
"""

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.audit.service import write_audit_log
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.embed import build_embed_text
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.schema import (
    SchemaValidationError,
    validate_edge_type,
    validate_node,
)


class NodeNotFoundError(Exception):
    """节点不存在或已软删除时抛出。"""


async def create_node(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient | None,
    project_id: uuid.UUID | None,
    node_type: str,
    title: str,
    content: str,
    properties: dict[str, Any],
    tags: list[str] | None = None,
    source: dict[str, Any] | None = None,
    created_by: str,
    system_id: uuid.UUID | None = None,
    generate_vector: bool = True,
    content_vector: list[float] | None = None,
) -> KnowledgeNode:
    """创建知识节点（PG 表 + AGE 图节点 + 审计日志，事务性共写）。

    system 维度（PM 需求跨项目建模）：
    - Requirement：system_id 必填，project_id 可空（悬浮需求）
    - 其余资产类型：project_id 必填（不可悬浮）
    违规抛 SchemaValidationError。

    流程：
    1. schema.validate_node 校验类型与必填字段
    2. 按类型强约束 system/project 归属
    3. 若 generate_vector 且 embedding_client 提供：调用 EmbeddingClient 生成 1024 维向量
    4. INSERT knowledge_node（content_tsv 由触发器自动维护）
    5. 调用 graph_store.add_node 同步图节点（带 id/project_id/title 属性）
    6. write_audit_log 记录创建审计

    不 commit，由调用方控制事务。
    """
    validate_node(node_type, properties)

    # ---- system / project 归属强约束（system 维度）----
    if node_type == "Requirement" and system_id is None:
        raise SchemaValidationError(
            "Requirement 必须归属 system（system_id 必填）"
        )
    if node_type != "Requirement" and project_id is None:
        raise SchemaValidationError(
            f"节点类型 {node_type} 必须归属 project（project_id 必填）"
        )

    content_vector_value: list[float] | None = None
    if generate_vector:
        if content_vector is not None:
            # 复用调用方批量预计算的向量（审批批量 embed 场景）
            content_vector_value = content_vector
        elif embedding_client is None:
            raise ValueError(
                "generate_vector=True 且未提供 content_vector 时必须提供 embedding_client"
            )
        else:
            # 拼接标题、正文与关键属性作为向量化输入（属性富集提升语义召回）
            embed_input = build_embed_text(node_type, title, content, properties)
            content_vector_value = await embedding_client.embed_one(embed_input)

    node = KnowledgeNode(
        project_id=project_id,
        system_id=system_id,
        type=node_type,
        title=title,
        content=content,
        content_vector=content_vector_value,
        properties=properties,
        tags=tags or [],
        source=source or {},
        status="approved",
        version=1,
        created_by=created_by,
    )
    session.add(node)
    await session.flush()  # 触发 server_default 生成 id 与 created_at

    # AGE 图节点：携带 id/project_id/title 供图查询过滤（system_id 可选）
    graph_props: dict[str, Any] = {
        "id": str(node.id),
        "title": title,
    }
    if project_id is not None:
        graph_props["project_id"] = str(project_id)
    if system_id is not None:
        graph_props["system_id"] = str(system_id)
    await graph_store.add_node(
        session,
        node_id=node.id,
        label=node_type,
        properties=graph_props,
    )

    await write_audit_log(
        session,
        actor=created_by,
        action="write",
        target_type="node",
        target_id=node.id,
        project_id=project_id,
        detail={
            "node_type": node_type,
            "title": title,
            "version": 1,
            "vector_generated": content_vector_value is not None,
            "system_id": str(system_id) if system_id else None,
        },
    )

    return node


async def get_node(
    session: AsyncSession,
    node_id: uuid.UUID,
    include_deleted: bool = False,
) -> KnowledgeNode:
    """按 id 查询节点。

    默认排除软删除节点（is_deleted=False）。include_deleted=True 返回含已删除。
    不存在或已删除（且 include_deleted=False）抛 NodeNotFoundError。
    """
    stmt = select(KnowledgeNode).where(KnowledgeNode.id == node_id)
    if not include_deleted:
        stmt = stmt.where(KnowledgeNode.is_deleted == False)  # noqa: E712
    result = await session.execute(stmt)
    node = result.scalar_one_or_none()
    if node is None:
        raise NodeNotFoundError(f"节点不存在或已删除: {node_id}")
    return node


async def get_nodes_by_ids(
    session: AsyncSession,
    *,
    node_ids: list[uuid.UUID],
    status: str | None = "approved",
    include_deleted: bool = False,
) -> list[KnowledgeNode]:
    """按 id 列表批量查询节点（供审批异步嵌入 worker 加载指定节点）。

    不 commit。空列表直接返回空。
    """
    if not node_ids:
        return []
    stmt = select(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids))
    if status is not None:
        stmt = stmt.where(KnowledgeNode.status == status)
    if not include_deleted:
        stmt = stmt.where(KnowledgeNode.is_deleted == False)  # noqa: E712
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_node(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient | None,
    node_id: uuid.UUID,
    title: str | None = None,
    content: str | None = None,
    properties: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source: dict[str, Any] | None = None,
    actor: str,
    regenerate_vector: bool = True,
) -> KnowledgeNode:
    """更新节点字段并版本递增。

    规则：
    - 不允许修改 type 字段（节点类型不可变更），调用方需重新创建新节点
    - title/content/properties 任一更新且 regenerate_vector=True：重新生成向量
      （build_embed_text 的输入含属性段，属性变更同样影响向量）
    - properties 整体替换（不深度合并，调用方负责合并逻辑）
    - 版本号 +1
    - 审计日志记录变更前后关键字段

    不存在抛 NodeNotFoundError。不 commit。
    """
    node = await get_node(session, node_id)

    changes: dict[str, Any] = {}
    if title is not None and title != node.title:
        changes["title"] = {"from": node.title, "to": title}
        node.title = title
    if content is not None and content != node.content:
        changes["content"] = {"from": node.content[:200], "to": content[:200]}
        node.content = content
    if properties is not None:
        # 更新前重新校验必填字段（防止 properties 缺失关键字段）
        validate_node(node.type, properties)
        changes["properties"] = "updated"
        node.properties = properties
    if tags is not None:
        changes["tags"] = {"from": node.tags, "to": tags}
        node.tags = tags
    if source is not None:
        changes["source"] = "updated"
        node.source = source

    if not changes:
        # 无变更直接返回，避免无谓的版本递增
        return node

    node.version += 1

    # 标题/正文/属性任一变更时重生成向量（embed 输入含属性段，属性变更影响向量）
    if regenerate_vector and any(
        k in changes for k in ("title", "content", "properties")
    ):
        if embedding_client is None:
            raise ValueError(
                "regenerate_vector=True 且 title/content/properties 变更时必须提供 embedding_client"
            )
        embed_input = build_embed_text(node.type, node.title, node.content, node.properties)
        node.content_vector = await embedding_client.embed_one(embed_input)
        changes["vector_regenerated"] = True

    # 标题变更时同步图投影的 title，避免 impact_analysis 等返回旧标题（同事务，不 commit）。
    # 节点不存在时 sync_node_title 静默无操作（图投影非真相源，一致性以 PG 为准）。
    if "title" in changes:
        await graph_store.sync_node_title(session, node.id, node.title)

    await session.flush()

    await write_audit_log(
        session,
        actor=actor,
        action="update",
        target_type="node",
        target_id=node.id,
        project_id=node.project_id,
        detail={
            "node_type": node.type,
            "version": node.version,
            "changes": changes,
        },
    )

    return node


async def archive_node(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    node_id: uuid.UUID,
    actor: str,
    delete_from_graph: bool = False,
) -> KnowledgeNode:
    """归档节点（软删除：is_deleted=True + status=archived）。

    - 默认不删除 AGE 图节点（保留图遍历历史，archived 状态由查询过滤）
    - delete_from_graph=True 时调用 graph_store.delete_node 同步删除图节点与关联边
    - 已归档节点幂等（重复归档不报错）

    不存在抛 NodeNotFoundError。不 commit。
    """
    node = await get_node(session, node_id, include_deleted=True)

    if node.is_deleted and node.status == "archived":
        # 幂等：已归档直接返回
        return node

    node.is_deleted = True
    node.status = "archived"
    await session.flush()

    if delete_from_graph:
        await graph_store.delete_node(session, node_id)

    await write_audit_log(
        session,
        actor=actor,
        action="archive",
        target_type="node",
        target_id=node.id,
        project_id=node.project_id,
        detail={
            "node_type": node.type,
            "title": node.title,
            "delete_from_graph": delete_from_graph,
        },
    )

    return node


async def add_edge(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    edge_type: str,
    properties: dict[str, Any] | None = None,
    actor: str,
) -> None:
    """创建图边（关系），仅写 AGE 图与审计日志。

    前置条件：from_id 与 to_id 对应的节点已存在（PG 表与 AGE 图均存在）。
    本方法不二次校验节点存在性（避免重复查询），由调用方保证。
    edge_type 经 schema.validate_edge_type 校验。

    不 commit。
    """
    validate_edge_type(edge_type)
    edge_props = properties or {}
    # 注入审计元数据（边属性），与 PDD 4.3 边属性示例对齐
    edge_props.setdefault("created_by", actor)

    await graph_store.add_edge(
        session,
        from_id=from_id,
        to_id=to_id,
        edge_type=edge_type,
        properties=edge_props,
    )

    await write_audit_log(
        session,
        actor=actor,
        action="write",
        target_type="edge",
        detail={
            "edge_type": edge_type,
            "from_id": str(from_id),
            "to_id": str(to_id),
        },
    )


async def regenerate_vector(
    session: AsyncSession,
    *,
    embedding_client: EmbeddingClient,
    node_id: uuid.UUID,
    actor: str,
) -> KnowledgeNode:
    """重新生成节点向量（独立调用入口，供审批通过场景使用）。

    不存在抛 NodeNotFoundError。不 commit。
    """
    node = await get_node(session, node_id)
    embed_input = build_embed_text(node.type, node.title, node.content, node.properties)
    node.content_vector = await embedding_client.embed_one(embed_input)
    await session.flush()

    await write_audit_log(
        session,
        actor=actor,
        action="update",
        target_type="node",
        target_id=node.id,
        project_id=node.project_id,
        detail={"vector_regenerated": True, "trigger": "manual"},
    )

    return node


async def list_nodes_by_project(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_type: str | None = None,
    status: str | None = "approved",
    limit: int = 100,
    offset: int = 0,
    order_by: str | None = None,
) -> list[KnowledgeNode]:
    """按项目列出节点（支持类型与状态过滤，分页）。

    过滤规则：
    - status="approved"（默认）：仅返回 approved 且未软删除的节点
    - status="archived"：仅返回 archived 节点（is_deleted 隐含 True）
    - status=None：返回所有节点（含 archived），不过滤状态与软删除
    """
    stmt = select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
    if status is not None:
        stmt = stmt.where(KnowledgeNode.status == status)
        if status == "approved":
            stmt = stmt.where(KnowledgeNode.is_deleted == False)  # noqa: E712
    if node_type is not None:
        stmt = stmt.where(KnowledgeNode.type == node_type)

    if order_by == "id":
        # 按主键排序（唯一且稳定），用于 offset 分页遍历全量，避免非唯一排序键导致漏页/重页
        stmt = stmt.order_by(KnowledgeNode.id.asc())
    else:
        stmt = stmt.order_by(KnowledgeNode.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_nodes_by_project(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    status: str | None = "approved",
) -> int:
    """统计项目内节点数（与 list_nodes_by_project 同过滤规则）。

    供 reindex 后台任务预估总量、驱动进度展示。
    """
    stmt = select(func.count()).select_from(KnowledgeNode).where(
        KnowledgeNode.project_id == project_id
    )
    if status is not None:
        stmt = stmt.where(KnowledgeNode.status == status)
        if status == "approved":
            stmt = stmt.where(KnowledgeNode.is_deleted == False)  # noqa: E712
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def batch_regenerate_vectors(
    session: AsyncSession,
    *,
    embedding_client: EmbeddingClient,
    nodes: list[KnowledgeNode],
    actor: str,
) -> int:
    """批量重新生成节点向量（不 commit，由调用方事务控制）。

    收集节点 embed 文本 → 一次批量 embed 调用 → 逐节点写回 content_vector +
    审计日志。相比逐节点 embed_one 大幅减少 HTTP 往返，供 reindex 后台任务使用。
    """
    if not nodes:
        return 0
    texts = [
        build_embed_text(n.type, n.title, n.content, n.properties) for n in nodes
    ]
    embeddings = await embedding_client.embed(texts)
    for node, vec in zip(nodes, embeddings):
        node.content_vector = vec
        await write_audit_log(
            session,
            actor=actor,
            action="update",
            target_type="node",
            target_id=node.id,
            project_id=node.project_id,
            detail={"vector_regenerated": True, "trigger": "reindex"},
        )
    await session.flush()
    return len(nodes)


async def get_distinct_tags(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_type: str | None = None,
) -> list[str]:
    """返回项目内所有节点的去重标签集合（用于标签语义扩展的词表）。

    仅统计未软删除节点（is_deleted=false）；tags 为 JSONB 数组，
    用 jsonb_array_elements_text 展开后 DISTINCT。node_type 非空时按类型过滤。
    """
    base = (
        "SELECT DISTINCT jsonb_array_elements_text(tags) AS tag "
        "FROM knowledge_node "
        "WHERE project_id = :pid AND is_deleted = false AND tags IS NOT NULL"
    )
    if node_type is not None:
        base += " AND type = :nt"
    stmt = text(base)
    params = {"pid": project_id}
    if node_type is not None:
        params["nt"] = node_type
    result = await session.execute(stmt, params)
    return [row[0] for row in result if row[0]]


async def list_project_profiles(
    session: AsyncSession,
    *,
    project_ids: list[uuid.UUID] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[KnowledgeNode]:
    """列出 ProjectProfile 节点（用于 get_project_info 枚举可见项目）。

    仅返回未软删除且 approved 的画像节点。project_ids 非空时按项目 ID 过滤
    （用于 pm/dev 仅查 scope 内项目、或 get 单项目）。按 created_at 倒序，
    便于调用方按 project_id 去重时取最新。
    """
    stmt = (
        select(KnowledgeNode)
        .where(KnowledgeNode.type == "ProjectProfile")
        .where(KnowledgeNode.is_deleted == False)  # noqa: E712
        .where(KnowledgeNode.status == "approved")
    )
    if project_ids is not None:
        stmt = stmt.where(KnowledgeNode.project_id.in_(project_ids))
    stmt = stmt.order_by(KnowledgeNode.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())
