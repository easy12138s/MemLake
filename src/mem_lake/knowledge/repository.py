"""知识图谱 Repository：节点 CRUD + 边 CRUD + 事务性共写 + 审计。

职责边界：
- 仅封装"PG 关系表（knowledge_node）+ AGE 图（节点/边）+ 审计日志"在同一 AsyncSession
  事务内的原子写入。session 不 commit，由调用方（approval 模块或 gateway）控制提交。
- 节点写入前调用 schema.validate_node 校验类型与必填字段，不合规抛 SchemaValidationError。
- 边写入前调用 schema.validate_edge_type 校验类型。
- 向量生成委托给 EmbeddingClient，向量延迟生成策略由调用方决定（直接 approved 场景同步生成；
  审批流场景 generate_vector=False 延迟生成，由 reindex worker 后续补写）。
- 图操作委托给 GraphStore 抽象，AGEGraphStore 为 v1.0 实现。
- 审计写入委托给 audit.service.write_audit_log，与业务操作同事务。
- 项目隔离由应用层 validate_project_access + 检索侧 FilterSpec（project_id 过滤）实现，
  不做数据库行级隔离策略（部署连接用户为表 owner 会天然绕过；见 db/init.py 设计说明）。

设计权衡：
- 不在 repository 内 commit，保证审批流可整体回滚。
- get_node 返回 ORM 对象（而非 dict），保留懒加载与类型提示。
- 软删除（is_deleted=True + status=archived）替代物理删除，保留审计可追溯。
- update_node 不修改 type 字段（节点类型不可变更，避免图谱与关系表不一致）。
"""

import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.audit.service import write_audit_log
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.embed import build_embed_facets
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.models import (
    KnowledgeNode,
    NodeEmbedding,
    RequirementCounter,
    System,
    SystemProject,
)
from mem_lake.knowledge.schema import (
    validate_attribution,
    validate_edge_type,
    validate_node,
)
from mem_lake.search.filters import node_active_approved


class NodeNotFoundError(Exception):
    """节点不存在或已软删除时抛出。"""


def _graph_props(node: KnowledgeNode) -> dict[str, Any]:
    """构造 AGE 图节点属性：id/title 必带，project_id/system_id 非空才带。

    create_node 与 batch_insert_requirements 共用（图节点仅存过滤/展示所需字段）。
    """
    props: dict[str, Any] = {
        "id": str(node.id),
        "title": node.title,
    }
    if node.project_id is not None:
        props["project_id"] = str(node.project_id)
    if node.system_id is not None:
        props["system_id"] = str(node.system_id)
    return props


async def _node_write_audit_detail(
    session: AsyncSession, node: KnowledgeNode
) -> dict[str, Any]:
    """构造节点创建审计 detail（create_node 与 batch_insert_requirements 共用）。

    FIX-08：vector_generated 判定基于 NodeEmbedding 记录存在性（content_vector
    列废弃，检索主路径走 node_embedding），而非 content_vector 判空。
    """
    return {
        "node_type": node.type,
        "title": node.title,
        "version": node.version,
        "vector_generated": await _node_has_embedding(session, node.id),
        "system_id": str(node.system_id) if node.system_id else None,
    }


async def _node_has_embedding(session: AsyncSession, node_id: uuid.UUID) -> bool:
    """判断节点是否已有 facet 向量记录（FIX-08：替代 content_vector 判空）。"""
    result = await session.execute(
        select(NodeEmbedding.id).where(NodeEmbedding.node_id == node_id).limit(1)
    )
    return result.first() is not None


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
) -> KnowledgeNode:
    """创建知识节点（PG 表 + AGE 图节点 + 审计日志，事务性共写）。

    system 维度（PM 需求跨项目建模）：
    - Requirement：system_id 必填，project_id 可空（悬浮需求）
    - 其余资产类型：project_id 必填（不可悬浮）
    违规抛 SchemaValidationError。

    流程：
    1. schema.validate_node 校验类型与必填字段
    2. 按类型强约束 system/project 归属
    3. 若 generate_vector 且 embedding_client 提供：写入 facet 多向量（node_embedding）
    4. INSERT knowledge_node（content_tsv 由触发器自动维护）
    5. 调用 graph_store.add_node 同步图节点（带 id/project_id/title 属性）
    6. write_audit_log 记录创建审计

    不 commit，由调用方控制事务。

    注（FIX-08）：不再向 knowledge_node.content_vector 写主向量——检索主路径走
    node_embedding 多向量，content_vector 列废弃。此处仅写 facet 向量，避免为
    无用途列多算一次 content embed。
    """
    validate_node(node_type, properties)

    # ---- system / project 归属强约束（FIX-17：schema.validate_attribution 单一实现）----
    validate_attribution(node_type, system_id=system_id, project_id=project_id)

    requirement_key = None
    if node_type == "Requirement" and system_id is not None:
        # 需求主键由服务端按 system 域分配可读序号（如 HIS-0001），作为需求节点唯一键；
        # properties 契约（必填∪可选白名单）已在 schema.validate_node 收口
        prefix = await _resolve_requirement_prefix(session, system_id)
        seq = await _alloc_requirement_sequence(session, system_id)
        requirement_key = f"{prefix}-{seq:04d}"

    node = KnowledgeNode(
        project_id=project_id,
        system_id=system_id,
        requirement_key=requirement_key,
        type=node_type,
        title=title,
        content=content,
        properties=properties,
        tags=tags or [],
        source=source or {},
        status="approved",
        version=1,
        created_by=created_by,
    )
    session.add(node)
    await session.flush()  # 触发 server_default 生成 id 与 created_at

    # 多向量 facet 写入（32k 适配 D）：generate_vector 必须提供 embedding_client 才可写。
    # FIX-08：content_vector 停写，单主向量 embed 一并消除，仅写 facet 多向量；
    # generate_vector=True 但缺 client 仍抛错（facet 向量写入依赖 client）。
    if generate_vector:
        if embedding_client is None:
            raise ValueError(
                "generate_vector=True 时必须提供 embedding_client"
            )
        await _store_facet_vectors(
            session,
            node_id=node.id,
            node_type=node_type,
            title=title,
            content=content,
            properties=properties,
            embedding_client=embedding_client,
        )

    # AGE 图节点：携带 id/project_id/title 供图查询过滤（system_id 可选）
    await graph_store.add_node(
        session,
        node_id=node.id,
        label=node_type,
        properties=_graph_props(node),
    )

    await write_audit_log(
        session,
        actor=created_by,
        action="write",
        target_type="node",
        target_id=node.id,
        project_id=project_id,
        detail=await _node_write_audit_detail(session, node),
    )

    return node


async def _resolve_requirement_prefix(session: AsyncSession, system_id: uuid.UUID) -> str:
    """根据 system 域解析需求主键前缀。

    code 优先；否则由 name 派生（取 ASCII 字母数字，截断 6 位）；都没有则回退 SYS。
    """
    system = await session.get(System, system_id)
    if system is None:
        return "SYS"
    if system.code:
        code = str(system.code).strip().upper()
        return code[:32] or "SYS"
    if system.name:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", system.name)[:6].upper()
        if cleaned:
            return cleaned
    return "SYS"


async def _alloc_requirement_sequence(session: AsyncSession, system_id: uuid.UUID) -> int:
    """原子递增并返回某 system 下需求序号（1 起）。"""
    await session.execute(
        pg_insert(RequirementCounter)
        .values(system_id=system_id, last_value=0)
        .on_conflict_do_nothing()
    )
    result = await session.execute(
        sa_update(RequirementCounter)
        .where(RequirementCounter.system_id == system_id)
        .values(last_value=RequirementCounter.last_value + 1)
        .returning(RequirementCounter.last_value)
    )
    return int(result.scalar_one())


async def _write_facets_batch(
    session: AsyncSession,
    *,
    node_meta: list[tuple[uuid.UUID, str, str, str, dict[str, Any]]],
    embedding_client: EmbeddingClient,
) -> int:
    """批量写入多向量 facet（FIX-25 单一实现，三调用点复用）。

    node_meta 每项为 (node_id, node_type, title, content, properties)。所有节点的
    所有 facet 文本一次批量 embed（减少 HTTP 往返），写回 node_embedding 表，
    按 node_id 幂等（先删旧行再写）。返回写入的 facet 行数（全部空节点返回 0）。
    不 commit，由调用方事务控制。
    """
    all_texts: list[str] = []
    meta: list[tuple[uuid.UUID, str]] = []  # (node_id, facet_name)
    for node_id, node_type, title, content, props in node_meta:
        facets = build_embed_facets(node_type, title, content, props)
        for fname, ftext in facets.items():
            all_texts.append(ftext)
            meta.append((node_id, fname))
    if not all_texts:
        return 0
    vectors = await embedding_client.embed(all_texts)
    # 幂等：先按节点批量删旧行，再插入新行
    node_ids = [m[0] for m in meta]
    await session.execute(
        delete(NodeEmbedding).where(NodeEmbedding.node_id.in_(node_ids))
    )
    for (node_id, fname), vec in zip(meta, vectors):
        session.add(
            NodeEmbedding(
                node_id=node_id,
                facet=fname,
                content_vector=vec,
            )
        )
    return len(meta)


async def _store_facet_vectors(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    node_type: str,
    title: str,
    content: str,
    properties: dict[str, Any],
    embedding_client: EmbeddingClient,
) -> int:
    """写入单个节点的多向量 facet（32k 适配 D）。

    FIX-25：收敛为 _write_facets_batch 的单元素调用（create_node/update_node 复用
    同一实现），保持幂等（先清后写）。返回写入的 facet 行数（0 表示空节点不写）。
    """
    return await _write_facets_batch(
        session,
        node_meta=[(node_id, node_type, title, content, properties)],
        embedding_client=embedding_client,
    )


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
    - title/content/properties 任一更新且 regenerate_vector=True：重新生成 facet
      向量（facet 文本含属性段，属性变更同样影响向量）
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

    # 标题/正文/属性任一变更时重生成 facet 向量（embed 输入含属性段，属性变更影响向量）。
    # FIX-08：content_vector 停写，单主向量 embed 一并消除，仅重算 facet 多向量。
    if regenerate_vector and any(
        k in changes for k in ("title", "content", "properties")
    ):
        if embedding_client is None:
            raise ValueError(
                "regenerate_vector=True 且 title/content/properties 变更时必须提供 embedding_client"
            )
        changes["vector_regenerated"] = True
        # 多向量 facet（32k 适配 D）
        await _store_facet_vectors(
            session,
            node_id=node.id,
            node_type=node.type,
            title=node.title,
            content=node.content,
            properties=node.properties,
            embedding_client=embedding_client,
        )

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
    """批量重新生成节点 facet 向量（不 commit，由调用方事务控制）。

    FIX-08：content_vector 停写，仅重算多向量 facet（node_embedding）。汇集所有
    节点所有 facet 文本，一次批量 embed（相比逐节点 embed_one 大幅减少 HTTP 往返），
    供 reindex 后台任务使用。
    """
    if not nodes:
        return 0
    # FIX-25：收敛到 _write_facets_batch 单一实现（汇集所有节点所有 facet 文本一次
    # 批量 embed，幂等写回），与 create_node/update_node/batch_insert_requirements 复用。
    await _write_facets_batch(
        session,
        node_meta=[
            (n.id, n.type, n.title, n.content, n.properties) for n in nodes
        ],
        embedding_client=embedding_client,
    )

    for node in nodes:
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


async def batch_insert_requirements(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient | None,
    nodes: list[KnowledgeNode],
    actor: str,
    allocate_requirement_key: bool = False,
    system_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """批量创建 Requirement 节点（创建路径，不 commit，由调用方事务控制）。

    镜像 batch_regenerate_vectors 的批量 embed 模式，但面向创建：
    - 可选为每节点原子分配可读需求主键（HIS-0001）
    - 全部节点的所有 facet 文本一次批量 embed，写 NodeEmbedding facet 行
    - session.add_all + flush 落 PG，同步 AGE 图节点与审计日志
    - 返回 {"created": len(nodes)}

    FIX-08：content_vector 停写，单主向量 embed 一并消除，仅写 facet 多向量。
    """
    if not nodes:
        return {"created": 0}

    if allocate_requirement_key:
        if system_id is None:
            raise ValueError("allocate_requirement_key=True 时 system_id 必填")
        prefix = await _resolve_requirement_prefix(session, system_id)
        for node in nodes:
            seq = await _alloc_requirement_sequence(session, system_id)
            node.requirement_key = f"{prefix}-{seq:04d}"

    session.add_all(nodes)
    await session.flush()  # 触发 server_default 生成 id 与 created_at

    # 写 facet 行：节点 id 需在 flush 后解析，故在 flush 之后汇总 node_meta 并收敛到
    # _write_facets_batch（FIX-25：与 create_node/update_node/batch_regenerate_vectors 复用）。
    if embedding_client:
        await _write_facets_batch(
            session,
            node_meta=[
                (n.id, n.type, n.title, n.content, n.properties) for n in nodes
            ],
            embedding_client=embedding_client,
        )

    # AGE 图节点 + 审计日志
    for node in nodes:
        await graph_store.add_node(
            session,
            node_id=node.id,
            label=node.type,
            properties=_graph_props(node),
        )
        await write_audit_log(
            session,
            actor=actor,
            action="write",
            target_type="node",
            target_id=node.id,
            project_id=node.project_id,
            detail=await _node_write_audit_detail(session, node),
        )

    await session.flush()

    return {"created": len(nodes)}


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
    params: dict[str, Any] = {"pid": project_id}
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
        .where(*node_active_approved())
    )
    if project_ids is not None:
        stmt = stmt.where(KnowledgeNode.project_id.in_(project_ids))
    stmt = stmt.order_by(KnowledgeNode.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ============================================================================
# System / SystemProject 域 repository（FIX-26：OR 操作收口于本层）
# ============================================================================


async def create_system(
    session: AsyncSession,
    *,
    name: str,
    description: str = "",
) -> System:
    """创建 System 域（不 commit，由调用方事务控制）。"""
    sys_obj = System(name=name, description=description)
    session.add(sys_obj)
    await session.flush()
    return sys_obj


async def get_system(
    session: AsyncSession, system_id: uuid.UUID
) -> System | None:
    """按 id 查询 System，不存在返回 None。"""
    return await session.get(System, system_id)


async def list_systems(session: AsyncSession) -> list[System]:
    """枚举全部 System（按 name 升序）。"""
    result = await session.execute(select(System).order_by(System.name))
    return list(result.scalars().all())


async def get_system_by_name(session: AsyncSession, name: str) -> System | None:
    """按 name 精确查询 System，不存在返回 None。"""
    result = await session.execute(select(System).where(System.name == name))
    return result.scalar_one_or_none()


async def get_system_by_code(session: AsyncSession, code: str) -> System | None:
    """按 code 精确查询 System（code 可能为 NULL），不存在返回 None。"""
    result = await session.execute(select(System).where(System.code == code))
    return result.scalar_one_or_none()


async def count_system_projects(
    session: AsyncSession, *, system_id: uuid.UUID
) -> int:
    """统计某 System 下绑定的项目数。"""
    result = await session.execute(
        select(func.count())
        .select_from(SystemProject)
        .where(SystemProject.system_id == system_id)
    )
    return int(result.scalar() or 0)


async def get_system_project_ids(
    session: AsyncSession, *, system_id: uuid.UUID
) -> set[uuid.UUID]:
    """返回某 System 下绑定的项目 ID 集合（供可见性判定）。"""
    result = await session.execute(
        select(SystemProject.project_id).where(SystemProject.system_id == system_id)
    )
    return {row[0] for row in result}


async def set_system_projects(
    session: AsyncSession,
    *,
    system_id: uuid.UUID,
    project_ids: list[uuid.UUID],
) -> int:
    """重置某 System 的项目绑定（幂等：先清空再批量插入，不 commit）。"""
    await session.execute(
        delete(SystemProject).where(SystemProject.system_id == system_id)
    )
    for pid in project_ids:
        session.add(SystemProject(system_id=system_id, project_id=pid))
    await session.flush()
    return len(project_ids)
