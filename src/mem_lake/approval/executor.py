"""审批执行写入（FIX-24 从 service.py 拆出的 executor 模块）。

职责：审批通过（review_approve）与拒绝（review_reject）的状态机推进，及
node/edge 的执行写入（_execute_node_create / _execute_node_update /
_execute_edge_create）、临时引用解析（_resolve_ref）、冲突提示批量向量化
（_build_conflict_query_vectors）与合并（_merge_conflict_hints）、uuid 工具
（_to_uuid）。

事务性：本层不 commit，由调用方（gateway 工具或 policy.auto_process_batch）
控制事务边界。review_approve 内的所有写操作（create_node / add_edge /
audit_log / 状态更新 / target_id 回填）在同一 AsyncSession 内，任一步骤失败
整体回滚（PDD 3.4 硬约束）。
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.approval.conflict import detect_conflicts
from mem_lake.approval.models import ApprovalBatch, ApprovalItem
from mem_lake.approval.repository import (
    STATUS_APPROVED,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    BatchStatusError,
    get_batch_detail,
)
from mem_lake.approval.validation import PayloadValidationError
from mem_lake.audit.service import write_audit_log
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.embed import build_embed_text
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.repository import add_edge, create_node, get_node, update_node
from mem_lake.observability.metrics import APPROVAL_BATCHES
from mem_lake.search.vector import VectorSearcher


async def _build_conflict_query_vectors(
    embedding_client: EmbeddingClient,
    create_items: list[ApprovalItem],
) -> list[list[float]]:
    """批量构造冲突检测查询向量（review_approve / auto_process_batch 共用）。

    所有 node+create 项的查询文本一次性 embed（prompt_name="query"，与
    VectorSearcher.search 语义一致），避免每项各 embed 一次。查询文本用
    build_embed_text，与 conflict.detect_conflicts 内部构造一致。
    返回顺序与 create_items 一致；create_items 为空时返回 []。
    """
    if not create_items:
        return []
    return await embedding_client.embed(
        [
            build_embed_text(
                it.entity_type,
                it.payload["title"],
                it.payload["content"],
                it.payload.get("properties", {}),
            )
            for it in create_items
        ],
        prompt_name="query",
    )


async def review_approve(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    reviewed_by: str,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    vector_searcher: VectorSearcher,
    review_comment: str | None = None,
    conflict_query_vectors: list[list[float]] | None = None,
) -> ApprovalBatch:
    """审批通过：原子写入 knowledge_node 表与 AGE 图，生成 conflict_hint。

    流程：
    1. 查询批次（含 items），校验 status == pending_review（否则抛 BatchStatusError）
    2. 遍历 approval_items，按 (item_type, action) 分发：
       - node + create：调用 create_node(status="approved", generate_vector=True)
         冲突检测：detect_conflicts(...)（统一三层实现，写入后检测并排除自身），
         累加 conflict_hint；回填 approval_item.target_id = node.id
       - node + update：调用 update_node
       - edge + create：解析 from_ref/to_ref 并校验端点存在，调用 add_edge
    3. 合并所有 node 的 conflict_hint，写入 approval_batch.conflict_hint
    4. 更新 approval_batch.status = approved, reviewed_by, reviewed_at, review_comment
    5. 写审计日志

    事务性：所有写操作在同一 AsyncSession 内，任一步骤失败整体回滚。
    不 commit。

    conflict_query_vectors：调用方（auto_process_batch）已预计算的冲突查询向量
    （与内部构造顺序一致），非 None 时跳过内部重新 embed，消除重复计算
    （AUDIT §2.12）。
    """
    batch = await get_batch_detail(session, batch_id)

    # 1. 状态校验
    if batch.status != STATUS_PENDING_REVIEW:
        raise BatchStatusError(
            f"批次状态不允许审批通过: 当前={batch.status}, 期望={STATUS_PENDING_REVIEW}"
        )

    # 2. 遍历 items 执行写入
    all_conflict_hints: list[dict[str, Any]] = []

    # 冲突检测批量向量化：所有新建节点的查询文本一次性 embed（prompt_name="query"，
    # 与 VectorSearcher.search 语义一致），避免每节点各 embed 一次（2N → 2）。
    # 查询文本用 build_embed_text，与 conflict.detect_conflicts 内部构造一致。
    # auto_process_batch 已预计算时复用，此处跳过内部重新 embed。
    create_items = [
        it
        for it in batch.items
        if it.item_type == "node" and it.action == "create"
    ]
    if conflict_query_vectors is None:
        # FIX-12：复用 _build_conflict_query_vectors（与 auto_process_batch 同一实现），
        # 消除瘦身轮遗留的内联重复。
        conflict_query_vectors = await _build_conflict_query_vectors(
            embedding_client, create_items
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
            # 悬浮节点（project_id=None）按 system_id 收口冲突候选域。
            conflict_hint = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=node.project_id,
                system_id=node.system_id,
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

    # 2.1 新建节点向量化延迟到后台异步执行：facet 向量（node_embedding）暂缺
    # （FIX-08：content_vector 列已废弃，检索走 node_embedding，缺向量节点自动
    # 排除），审批提交后由调用方经 start_embed_nodes_task 入队，复用 reindex
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

    APPROVAL_BATCHES.labels(status="approved").inc()

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

    APPROVAL_BATCHES.labels(status="rejected").inc()

    return batch


async def _execute_node_create(
    session: AsyncSession,
    *,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    item: ApprovalItem,
) -> Any:
    """执行 node + create：调用 create_node 写入 PG 表与 AGE 图。"""
    payload = item.payload
    _pid = payload.get("project_id")
    _sid = payload.get("system_id")
    return await create_node(
        session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        project_id=_to_uuid(_pid) if _pid else None,
        node_type=item.entity_type,
        title=payload["title"],
        content=payload["content"],
        properties=payload["properties"],
        tags=payload.get("tags", []),
        source=payload.get("source", {}),
        created_by=payload["created_by"],
        system_id=_to_uuid(_sid) if _sid else None,
        generate_vector=False,  # 延迟生成：审批通过后由 review_tools 调 start_embed_nodes_task 入队后台 worker 补向量
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
    node_id = _to_uuid(payload["node_id"])
    # node+update 的 node_id 为必填项（提交时已校验），解析必成功
    assert node_id is not None
    return await update_node(
        session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        node_id=node_id,
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
    3. 业务可读键（如 requirement_key）：当前不支持，需 Agent 先查到节点 UUID

    PDD 3.4 硬约束：解析失败抛 PayloadValidationError 触发事务回滚（保证原子性）。
    解析成功后，校验节点在 knowledge_node 表存在（避免 AGE 静默丢失边创建）。

    注：AGE CREATE edge 在 MATCH 失败时静默跳过不抛错（Cypher 标准行为），
    因此必须在应用层显式校验节点存在性。
    """
    payload = item.payload
    from_ref = payload.get("from_ref")
    to_ref = payload.get("to_ref")

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


def _merge_conflict_hints(hints: list[dict[str, Any]]) -> dict[str, Any]:
    """合并多个节点的 conflict_hint 为单个 JSONB。

    返回结构：
    {
        "has_conflict": bool,
        "nodes_with_conflict": int,
        "details": [...],  # 含冲突的节点列表
        "suggestion": "review"/None
    }

    detect_conflicts 的 suggestion 只为 "review" 或 None（conflict.py 由
    has_conflict 决定），故合并仅区分 review / None，无 manual_merge 分支。
    """
    if not hints:
        return {"has_conflict": False, "nodes_with_conflict": 0, "details": [], "suggestion": None}

    details_with_conflict = [h for h in hints if h["conflict"].get("has_conflict")]
    has_conflict = bool(details_with_conflict)

    # 聚合建议：任一节点冲突则整体 review（detect_conflicts 只产 review/None）
    suggestions = {h["conflict"].get("suggestion") for h in details_with_conflict}
    suggestion = "review" if "review" in suggestions else None

    return {
        "has_conflict": has_conflict,
        "nodes_with_conflict": len(details_with_conflict),
        "details": details_with_conflict,
        "suggestion": suggestion,
    }


def _to_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """将字符串或 UUID 转换为 UUID。None/空串返回 None（悬浮需求无 project_id 场景）。"""
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
