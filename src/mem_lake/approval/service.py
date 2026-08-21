"""审批服务：批次提交、审批通过（原子写入图谱）、退回。

对齐 PDD 3.4 审批工作流。状态机自建（pending_review → approved/rejected，无外部框架）。
审批通过时复用 knowledge.repository 的写接口（create_node/add_edge）原子写入
knowledge_node 表与 AGE 图，并基于 VectorSearcher 检测冲突生成 conflict_hint。

事务性：service 层不 commit，由调用方（M6 gateway 工具）控制事务边界。
review_approve 内的所有写操作（create_node/add_edge/audit_log/状态更新/target_id 回填）
在同一 AsyncSession 内，任一步骤失败整体回滚（PDD 3.4 硬约束）。
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mem_lake.approval.conflict import detect_conflicts
from mem_lake.approval.models import ApprovalBatch, ApprovalItem
from mem_lake.audit.service import write_audit_log
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.repository import add_edge, create_node, get_node, update_node
from mem_lake.knowledge.schema import SchemaValidationError, validate_edge_type, validate_node
from mem_lake.search.vector import VectorSearcher

# 批次类型白名单（PDD 3.4）
BATCH_TYPES: frozenset[str] = frozenset({
    "publish_requirement",
    "submit_dev_artifacts",
    "update_requirement_relations",
})

# 状态机常量
STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# 终态集合（不可再转换）
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_APPROVED, STATUS_REJECTED})

# 单批次 items 上限（节点+边总数），防止一次灌入大量节点/边造成滥用
MAX_ITEMS_PER_BATCH: int = 50


class BatchNotFoundError(Exception):
    """批次不存在时抛出。"""


class BatchStatusError(Exception):
    """批次状态不允许当前操作时抛出（如已审批的批次再次审批）。"""


class IdempotencyConflictError(Exception):
    """幂等键冲突时抛出（同 operation_id 已有不同内容的批次）。"""


class PayloadValidationError(Exception):
    """payload 结构不合规时抛出。"""


async def submit_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    batch_type: str,
    submitted_by: str,
    submitter_role: str,
    items: list[dict],
    operation_id: str | None = None,
) -> ApprovalBatch:
    """提交审批批次。

    参数：
        session: 异步数据库会话
        project_id: 归属项目
        batch_type: 批次类型（必须在 BATCH_TYPES 白名单内）
        submitted_by: 提交者 Access Key ID
        submitter_role: 提交者角色（pm/dev）
        items: 审批项列表，每项格式：
            {"item_type": "node"/"edge", "action": "create"/"update"/"delete",
             "entity_type": "Requirement"/"implements"/..., "payload": {...}}
        operation_id: 幂等操作标识，None 时不参与幂等校验

    返回：创建的 ApprovalBatch（含 items）。

    流程：
    1. 校验 batch_type 在白名单内
    2. 幂等校验：若 operation_id 提供，查询 (submitted_by, batch_type, operation_id)
       - 已存在：返回已有批次（幂等重放）
       - 不存在：继续创建
    3. 校验每个 item 的 payload 合规性（node + create 校验必填字段，edge + create 校验类型）
    4. 生成 summary（N 个节点 + M 个关系）
    5. 创建 ApprovalBatch（status=pending_review）+ ApprovalItem 列表
    6. 写审计日志

    不 commit。
    """
    # 1. batch_type 白名单校验
    if batch_type not in BATCH_TYPES:
        raise PayloadValidationError(
            f"非法批次类型: {batch_type}，合法类型: {sorted(BATCH_TYPES)}"
        )

    # 2. 幂等校验
    if operation_id is not None:
        existing = await _find_by_idempotency_key(
            session, submitted_by=submitted_by, batch_type=batch_type, operation_id=operation_id
        )
        if existing is not None:
            # 幂等重放：返回已有批次（含 items）
            return await get_batch_detail(session, existing.id)

    # 3. 校验 items 非空与 payload 合规性
    if not items:
        raise PayloadValidationError("items 不能为空")

    if len(items) > MAX_ITEMS_PER_BATCH:
        raise PayloadValidationError(
            f"批次 items 数量 {len(items)} 超过上限 {MAX_ITEMS_PER_BATCH}"
            f"（节点+边总数），请分批提交"
        )

    for idx, item in enumerate(items):
        _validate_item_structure(item, idx)
        _validate_item_payload(item, idx)

    # 4. 生成 summary
    node_count = sum(1 for i in items if i["item_type"] == "node")
    edge_count = sum(1 for i in items if i["item_type"] == "edge")
    summary = f"{node_count} 个节点 + {edge_count} 个关系"

    # 5. 创建 ApprovalBatch + ApprovalItem
    batch = ApprovalBatch(
        project_id=project_id,
        batch_type=batch_type,
        submitted_by=submitted_by,
        submitter_role=submitter_role,
        summary=summary,
        status=STATUS_PENDING_REVIEW,
        operation_id=operation_id,
    )
    session.add(batch)
    await session.flush()  # 生成 batch.id

    for seq, item in enumerate(items, start=1):
        approval_item = ApprovalItem(
            batch_id=batch.id,
            seq=seq,
            item_type=item["item_type"],
            action=item["action"],
            entity_type=item["entity_type"],
            payload=item["payload"],
        )
        session.add(approval_item)

    await session.flush()

    # 显式预加载 items：调用方可能在 session 关闭后访问 batch.items
    # （如 WriteToolOutput.from_batch），此时懒加载会因 detached 报错。
    await session.refresh(batch, attribute_names=["items"])

    # 6. 写审计日志
    await write_audit_log(
        session,
        actor=submitted_by,
        action="submit",
        target_type="batch",
        target_id=batch.id,
        project_id=project_id,
        operation_id=operation_id,
        detail={
            "batch_type": batch_type,
            "summary": summary,
            "item_count": len(items),
        },
    )

    return batch


async def review_approve(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    reviewed_by: str,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    vector_searcher: VectorSearcher,
    review_comment: str | None = None,
) -> ApprovalBatch:
    """审批通过：原子写入 knowledge_node 表与 AGE 图，生成 conflict_hint。

    流程：
    1. 查询批次（含 items），校验 status == pending_review（否则抛 BatchStatusError）
    2. 遍历 approval_items，按 (item_type, action) 分发：
       - node + create：调用 create_node(status="approved", generate_vector=True)
         冲突检测：detect_conflicts(...)（统一三层实现，写入后检测并排除自身），
         累加 conflict_hint；回填 approval_item.target_id = node.id
       - node + update：调用 update_node
       - edge + create：校验 from_id/to_id 存在，调用 add_edge
    3. 合并所有 node 的 conflict_hint，写入 approval_batch.conflict_hint
    4. 更新 approval_batch.status = approved, reviewed_by, reviewed_at, review_comment
    5. 写审计日志

    事务性：所有写操作在同一 AsyncSession 内，任一步骤失败整体回滚。
    不 commit。
    """
    batch = await get_batch_detail(session, batch_id)

    # 1. 状态校验
    if batch.status != STATUS_PENDING_REVIEW:
        raise BatchStatusError(
            f"批次状态不允许审批通过: 当前={batch.status}, 期望={STATUS_PENDING_REVIEW}"
        )

    # 2. 遍历 items 执行写入
    all_conflict_hints: list[dict] = []

    # 冲突检测批量向量化：所有新建节点的查询文本一次性 embed（prompt_name="query"，
    # 与 VectorSearcher.search 语义一致），避免每节点各 embed 一次（2N → 2）。
    create_items = [
        it
        for it in batch.items
        if it.item_type == "node" and it.action == "create"
    ]
    conflict_query_vectors: list[list[float]] = []
    if create_items:
        conflict_query_vectors = await embedding_client.embed(
            [f"{it.payload['title']}\n{it.payload['content']}" for it in create_items],
            prompt_name="query",
        )

    qv_index = 0
    for item in batch.items:
        if item.item_type == "node" and item.action == "create":
            node = await _execute_node_create(
                session,
                graph_store=graph_store,
                embedding_client=embedding_client,
                item=item,
            )
            item.target_id = node.id

            # 冲突检测（节点已写入，排除自身；可捕获同批次内先写入的重复节点）。
            # 使用预计算查询向量（conflict_query_vectors[qv_index]），跳过内部 embed。
            conflict_hint = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=node.project_id,
                node_type=node.type,
                title=node.title,
                content=node.content,
                properties=node.properties or {},
                tags=node.tags or [],
                exclude_node_id=node.id,
                query_vector=conflict_query_vectors[qv_index],
            )
            qv_index += 1
            all_conflict_hints.append(
                {
                    "node_id": str(node.id),
                    "title": node.title,
                    "conflict": conflict_hint,
                }
            )

        elif item.item_type == "node" and item.action == "update":
            node = await _execute_node_update(
                session,
                graph_store=graph_store,
                embedding_client=embedding_client,
                item=item,
                actor=reviewed_by,
            )
            item.target_id = node.id

        elif item.item_type == "edge" and item.action == "create":
            await _execute_edge_create(
                session,
                graph_store=graph_store,
                item=item,
                actor=reviewed_by,
                batch=batch,
            )
            # 边无 target_id，留空

    # 2.1 新建节点向量化延迟到后台异步执行：content_vector 暂为 NULL（搜索已能安全
    # 跳过 NULL），审批提交后由调用方经 start_embed_nodes_task 入队，复用 reindex
    # worker 补向量。此处不再同步 embed，避免大批次审批阻塞 MCP 调用超时。
    # 调用方从 batch.items（node+create 项的 target_id）即可取得新建节点 id。

    # 3. 合并 conflict_hint
    merged_conflict_hint = _merge_conflict_hints(all_conflict_hints)
    batch.conflict_hint = merged_conflict_hint

    # 4. 更新批次状态
    batch.status = STATUS_APPROVED
    batch.reviewed_by = reviewed_by
    batch.reviewed_at = datetime.now(timezone.utc)
    if review_comment is not None:
        batch.review_comment = review_comment

    await session.flush()

    # 5. 写审计日志
    await write_audit_log(
        session,
        actor=reviewed_by,
        action="approve",
        target_type="batch",
        target_id=batch.id,
        project_id=batch.project_id,
        detail={
            "batch_type": batch.batch_type,
            "item_count": len(batch.items),
            "conflict_detected": merged_conflict_hint.get("has_conflict", False),
        },
    )

    return batch


async def review_reject(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    reviewed_by: str,
    review_comment: str,
) -> ApprovalBatch:
    """审批拒绝：不写入正式图谱，更新批次状态。

    流程：
    1. 查询批次，校验 status == pending_review（否则抛 BatchStatusError）
    2. 更新 approval_batch.status = rejected, reviewed_by, reviewed_at, review_comment
    3. 写审计日志
    4. approval_items 保留用于追溯（PDD 3.4）

    不 commit。
    """
    batch = await get_batch_detail(session, batch_id)

    # 1. 状态校验
    if batch.status != STATUS_PENDING_REVIEW:
        raise BatchStatusError(
            f"批次状态不允许审批拒绝: 当前={batch.status}, 期望={STATUS_PENDING_REVIEW}"
        )

    # 2. 更新批次状态
    batch.status = STATUS_REJECTED
    batch.reviewed_by = reviewed_by
    batch.reviewed_at = datetime.now(timezone.utc)
    batch.review_comment = review_comment

    await session.flush()

    # 3. 写审计日志
    await write_audit_log(
        session,
        actor=reviewed_by,
        action="reject",
        target_type="batch",
        target_id=batch.id,
        project_id=batch.project_id,
        detail={
            "batch_type": batch.batch_type,
            "review_comment": review_comment,
        },
    )

    return batch


async def auto_process_batch(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    reviewed_by: str,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    vector_searcher: VectorSearcher,
) -> dict:
    """自动处理审批批次：三层冲突检测 → 无冲突自动通过 / 有冲突升级人工。

    为 admin Agent 设计（PDD v1.x 自动审批能力）。流程：
    1. 查询批次详情，校验 status == pending_review
    2. 遍历 approval_items 中的 node+create 项，对每个节点调用 detect_conflicts
       （三层检测：硬门控→关键属性比对→内容语义相似度，与 review_approve 同一实现）
    3. 合并所有节点的冲突检测结果
    4. 若无冲突：调用 review_approve 完成原子写入，返回 decision="auto_approved"
    5. 若有冲突：不写入图谱，返回 decision="needs_human_review" + 冲突详情

    事务性：与 review_approve 一致，service 层不 commit，由调用方控制事务边界。
    无冲突时 review_approve 的所有写操作（节点/边/审计日志/状态更新）在同一
    AsyncSession 内，任一步骤失败整体回滚。有冲突时仅执行只读检索，无写操作。

    参数：
        session: 异步数据库会话
        batch_id: 审批批次 ID
        reviewed_by: 审批者 Access Key ID（自动审批时仍记录）
        graph_store: 图存储实例（传入 review_approve）
        embedding_client: Embedding 客户端（传入 review_approve）
        vector_searcher: 向量检索器（用于 detect_conflicts + 传入 review_approve）

    返回：
        {
            "decision": "auto_approved" | "needs_human_review",
            "conflict_hint": {
                "has_conflict": bool,
                "checked_nodes": int,        # 检测的 node+create 项数
                "candidates_examined": int,  # 向量检索召回的候选总数
                "conflicting_nodes": list,   # has_conflict=True 时的冲突详情
                "suggestion": "review" | None,
            },
            "batch": ApprovalBatch,  # auto_approved 时为 approved 状态，needs_human_review 时为 pending_review
        }
    """
    batch = await get_batch_detail(session, batch_id)

    # 1. 状态校验
    if batch.status != STATUS_PENDING_REVIEW:
        raise BatchStatusError(
            f"批次状态不允许自动处理: 当前={batch.status}, 期望={STATUS_PENDING_REVIEW}"
        )

    # 2. 遍历 node+create 项执行三层冲突检测
    # 批量化：所有查询文本一次性 embed（prompt_name="query"），再逐条用预计算向量比对。
    create_items = [
        it
        for it in batch.items
        if it.item_type == "node" and it.action == "create"
    ]
    conflict_query_vectors: list[list[float]] = []
    if create_items:
        conflict_query_vectors = await embedding_client.embed(
            [f"{it.payload['title']}\n{it.payload['content']}" for it in create_items],
            prompt_name="query",
        )

    all_conflicts: list[dict] = []
    candidates_total = 0
    checked_nodes = 0
    qv_index = 0

    for item in batch.items:
        if item.item_type != "node" or item.action != "create":
            continue

        checked_nodes += 1
        payload = item.payload or {}

        conflict_result = await detect_conflicts(
            session,
            vector_searcher=vector_searcher,
            project_id=_to_uuid(payload["project_id"]),
            node_type=item.entity_type,
            title=payload["title"],
            content=payload["content"],
            properties=payload.get("properties", {}),
            tags=payload.get("tags", []),
            query_vector=conflict_query_vectors[qv_index],
        )
        qv_index += 1

        candidates_total += conflict_result.get("candidates_examined", 0)
        if conflict_result.get("has_conflict"):
            all_conflicts.extend(conflict_result.get("conflicting_nodes", []))

    has_conflict = bool(all_conflicts)

    # 3. 无冲突 → 自动审批通过
    if not has_conflict:
        approved_batch = await review_approve(
            session,
            batch_id=batch_id,
            reviewed_by=reviewed_by,
            graph_store=graph_store,
            embedding_client=embedding_client,
            vector_searcher=vector_searcher,
            review_comment="auto_approved: no conflict detected",
        )
        # 新建节点 id 取自审批后 batch.items 中 node+create 项的 target_id
        created_node_ids = [
            it.target_id
            for it in approved_batch.items
            if it.item_type == "node"
            and it.action == "create"
            and it.target_id is not None
        ]
        return {
            "decision": "auto_approved",
            "conflict_hint": {
                "has_conflict": False,
                "checked_nodes": checked_nodes,
                "candidates_examined": candidates_total,
            },
            "batch": approved_batch,
            "created_node_ids": created_node_ids,
        }

    # 4. 有冲突 → 升级人工审查（不写入图谱）
    return {
        "decision": "needs_human_review",
        "conflict_hint": {
            "has_conflict": True,
            "checked_nodes": checked_nodes,
            "candidates_examined": candidates_total,
            "conflicting_nodes": all_conflicts,
            "suggestion": "review",
        },
        "batch": batch,
    }


async def list_pending_batches(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ApprovalBatch]:
    """查询待审批批次列表。

    可选 project_id 过滤。返回 status=pending_review 的批次，按 submitted_at 降序。
    不预加载 items（列表场景只需批次元数据）。
    """
    stmt = (
        select(ApprovalBatch)
        .where(ApprovalBatch.status == STATUS_PENDING_REVIEW)
        .order_by(ApprovalBatch.submitted_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if project_id is not None:
        stmt = stmt.where(ApprovalBatch.project_id == project_id)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_batch_detail(
    session: AsyncSession,
    batch_id: uuid.UUID,
) -> ApprovalBatch:
    """查询批次详情（含 items，预加载避免 N+1 查询）。

    不存在抛 BatchNotFoundError。
    """
    stmt = (
        select(ApprovalBatch)
        .options(selectinload(ApprovalBatch.items))
        .where(ApprovalBatch.id == batch_id)
    )
    result = await session.execute(stmt)
    batch = result.scalar_one_or_none()
    if batch is None:
        raise BatchNotFoundError(f"批次不存在: {batch_id}")
    return batch


# ============ 内部辅助函数 ============


async def _find_by_idempotency_key(
    session: AsyncSession,
    *,
    submitted_by: str,
    batch_type: str,
    operation_id: str,
) -> ApprovalBatch | None:
    """按幂等键查询已存在的批次。"""
    stmt = (
        select(ApprovalBatch)
        .where(ApprovalBatch.submitted_by == submitted_by)
        .where(ApprovalBatch.batch_type == batch_type)
        .where(ApprovalBatch.operation_id == operation_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _validate_item_structure(item: dict, idx: int) -> None:
    """校验 item 结构含必要字段。"""
    required_keys = {"item_type", "action", "entity_type", "payload"}
    missing = required_keys - set(item.keys())
    if missing:
        raise PayloadValidationError(
            f"item[{idx}] 缺失字段: {sorted(missing)}，必要字段: {sorted(required_keys)}"
        )

    if item["item_type"] not in ("node", "edge"):
        raise PayloadValidationError(
            f"item[{idx}] 非法 item_type: {item['item_type']}，合法值: node/edge"
        )

    if item["action"] not in ("create", "update", "delete"):
        raise PayloadValidationError(
            f"item[{idx}] 非法 action: {item['action']}，合法值: create/update/delete"
        )


def _validate_item_payload(item: dict, idx: int) -> None:
    """校验 payload 合规性（提交时校验，避免审批通过时才发现错误）。"""
    item_type = item["item_type"]
    action = item["action"]
    entity_type = item["entity_type"]
    payload = item["payload"]

    if not isinstance(payload, dict):
        raise PayloadValidationError(f"item[{idx}] payload 必须为 dict")

    if item_type == "node" and action == "create":
        # node + create：校验节点类型与必填字段
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise PayloadValidationError(
                f"item[{idx}] node+create payload 缺 properties 或非 dict"
            )
        try:
            validate_node(entity_type, properties)
        except SchemaValidationError as e:
            raise PayloadValidationError(f"item[{idx}] node+create 校验失败: {e}") from e

    elif item_type == "edge" and action == "create":
        # edge + create：校验边类型
        try:
            validate_edge_type(entity_type)
        except SchemaValidationError as e:
            raise PayloadValidationError(f"item[{idx}] edge+create 校验失败: {e}") from e

        # 校验 from_ref/to_ref 或 from_id/to_id 存在
        # M6 支持 from_ref/to_ref 临时引用（PDD 5.3），兼容旧 from_id/to_id
        has_from = "from_ref" in payload or "from_id" in payload
        has_to = "to_ref" in payload or "to_id" in payload
        if not has_from or not has_to:
            raise PayloadValidationError(
                f"item[{idx}] edge+create payload 缺 from_ref/to_ref（或 from_id/to_id）"
            )

    # node + update / edge + update / delete：提交时不强校验，留待审批通过时校验


async def _execute_node_create(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    item: ApprovalItem,
) -> Any:
    """执行 node + create：调用 create_node 写入 PG 表与 AGE 图。"""
    payload = item.payload
    return await create_node(
        session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        project_id=_to_uuid(payload["project_id"]),
        node_type=item.entity_type,
        title=payload["title"],
        content=payload["content"],
        properties=payload["properties"],
        tags=payload.get("tags", []),
        source=payload.get("source", {}),
        created_by=payload["created_by"],
        generate_vector=False,  # 延迟生成：在 review_approve 内统一批量 embed
    )


async def _execute_node_update(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    item: ApprovalItem,
    actor: str,
) -> Any:
    """执行 node + update：调用 update_node 更新字段并版本递增。"""
    payload = item.payload
    return await update_node(
        session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        node_id=_to_uuid(payload["node_id"]),
        title=payload.get("title"),
        content=payload.get("content"),
        properties=payload.get("properties"),
        tags=payload.get("tags"),
        source=payload.get("source"),
        actor=actor,
        regenerate_vector=True,
    )


async def _execute_edge_create(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    item: ApprovalItem,
    actor: str,
    batch: ApprovalBatch,
) -> None:
    """执行 edge + create：解析 from_ref/to_ref → 校验节点存在 → 调用 add_edge。

    PDD 3.4 + 5.3：edge item 的 from_ref/to_ref 支持三种引用形式：
    1. 临时引用名（如 "requirement" / "LoginService"）：匹配同批次已 create 节点的
       payload.ref，反查 approval_item.target_id
    2. UUID 字符串（已有节点）：直接解析
    3. 业务 ID（如 requirement_id）：当前不支持，需 Agent 先查到节点 UUID

    PDD 3.4 硬约束：解析失败抛 PayloadValidationError 触发事务回滚（保证原子性）。
    解析成功后，校验节点在 knowledge_node 表存在（避免 AGE 静默丢失边创建）。

    注：AGE CREATE edge 在 MATCH 失败时静默跳过不抛错（Cypher 标准行为），
    因此必须在应用层显式校验节点存在性。
    """
    payload = item.payload
    from_ref = payload.get("from_ref") or payload.get("from_id")
    to_ref = payload.get("to_ref") or payload.get("to_id")

    if not from_ref or not to_ref:
        raise PayloadValidationError(
            f"edge item 缺少 from_ref/to_ref: from_ref={from_ref}, to_ref={to_ref}"
        )

    # 解析临时引用为 UUID
    from_id = await _resolve_ref(session, from_ref, batch)
    to_id = await _resolve_ref(session, to_ref, batch)

    # 前置校验：from_id/to_id 节点必须存在（PG 表）
    # 不存在抛 NodeNotFoundError，触发事务回滚
    await get_node(session, from_id)
    await get_node(session, to_id)

    await add_edge(
        session,
        graph_store=graph_store,
        from_id=from_id,
        to_id=to_id,
        edge_type=item.entity_type,
        properties=payload.get("properties", {}),
        actor=actor,
    )


async def _resolve_ref(
    session: AsyncSession, ref: str, batch: ApprovalBatch
) -> uuid.UUID:
    """解析临时引用为节点 UUID。

    解析顺序（PDD 5.3）：
    1. 尝试 UUID 解析（已有节点的 UUID 字符串）
    2. 匹配同批次 items 的 ref（同批次已 create 的节点，通过 target_id 反查）

    参数：
        session: DB 会话
        ref: 引用字符串（UUID 或 ref 名）
        batch: 当前审批批次（含 items）

    返回：节点 UUID

    抛出 PayloadValidationError：无法解析引用
    """
    # 1. 尝试 UUID 解析
    try:
        return uuid.UUID(str(ref))
    except (ValueError, TypeError, AttributeError):
        pass

    # 2. 匹配同批次 items 的 ref
    # 遍历 batch.items，查找 item_type=node + action=create 且 payload.ref 匹配的项
    # 该项的 target_id 已在 _execute_node_create 后回填（审批通过时按顺序处理）
    for batch_item in batch.items:
        if batch_item.item_type != "node" or batch_item.action != "create":
            continue
        item_payload = batch_item.payload or {}
        item_ref = item_payload.get("ref")
        if item_ref == ref and batch_item.target_id is not None:
            return batch_item.target_id

    raise PayloadValidationError(
        f"无法解析临时引用: {ref}（既不是 UUID，也未在同批次找到匹配的 ref）"
    )


def _merge_conflict_hints(hints: list[dict]) -> dict:
    """合并多个节点的 conflict_hint 为单个 JSONB。

    返回结构：
    {
        "has_conflict": bool,
        "nodes_with_conflict": int,
        "details": [...],  # 含冲突的节点列表
        "suggestion": "review"/"manual_merge"/None
    }
    """
    if not hints:
        return {"has_conflict": False, "nodes_with_conflict": 0, "details": [], "suggestion": None}

    details_with_conflict = [h for h in hints if h["conflict"].get("has_conflict")]
    has_conflict = bool(details_with_conflict)

    # 聚合建议：任一节点 suggestion 为 review 则整体 review
    suggestions = {h["conflict"].get("suggestion") for h in details_with_conflict}
    if "review" in suggestions:
        suggestion = "review"
    elif "manual_merge" in suggestions:
        suggestion = "manual_merge"
    else:
        suggestion = None

    return {
        "has_conflict": has_conflict,
        "nodes_with_conflict": len(details_with_conflict),
        "details": details_with_conflict,
        "suggestion": suggestion,
    }


def _to_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """将字符串或 UUID 转换为 UUID。"""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
