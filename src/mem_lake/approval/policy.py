"""冲突策略与自动审批（FIX-24 从 service.py 拆出的 policy 模块）。

职责：
- auto_process_batch：三层冲突检测 → 无冲突自动通过 / 有冲突升级人工（admin Agent 设计）。
- submit_batch_with_mode：按提交方审核模式创建批次，宽松模式下同事务自动处理。

依赖说明：检测/提交/审批执行等协作函数通过本包的对外门面
mem_lake.approval.service 在调用时解析（函数内 `import mem_lake.approval.service
as _svc`）。原因：FIX-24 拆包后，门面再次导出这些符号；运行时按门面命名空间
解析可保持既有行为（例如对被 patch 的 service 符号生效），同时避免模块加载期
service ↔ policy 的循环依赖。
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.approval.executor import _build_conflict_query_vectors, _to_uuid
from mem_lake.approval.models import ApprovalBatch
from mem_lake.approval.repository import (
    STATUS_PENDING_REVIEW,
    BatchStatusError,
)
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.observability.metrics import APPROVAL_BATCHES
from mem_lake.search.vector import VectorSearcher


async def auto_process_batch(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    reviewed_by: str,
    graph_store: GraphStore,
    embedding_client: EmbeddingClient,
    vector_searcher: VectorSearcher,
) -> dict[str, Any]:
    """自动处理审批批次：三层冲突检测 → 无冲突自动通过 / 有冲突升级人工。

    为 admin Agent 设计（PDD v1.x 自动审批能力）。流程：
    1. 查询批次详情，校验 status == pending_review
    2. 遍历 approval_items 中的 node+create 项，对每个节点调用 detect_conflicts
       （三层检测：硬门控→关键属性比对→内容语义相似度，与 review_approve 同一实现）
    3. 合并所有节点的冲突检测结果
    4. 若无冲突：调用 review_approve 完成原子写入，返回 decision="auto_approved"
    5. 若有冲突：不写入图谱，返回 decision="needs_human_review" + 冲突详情

    事务性：与 review_approve 一致，本层不 commit，由调用方控制事务边界。
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
    from mem_lake.approval import service as _svc  # 门面：运行时解析，避免加载期循环依赖

    batch = await _svc.get_batch_detail(session, batch_id)

    # 1. 状态校验
    if batch.status != STATUS_PENDING_REVIEW:
        raise BatchStatusError(
            f"批次状态不允许自动处理: 当前={batch.status}, 期望={STATUS_PENDING_REVIEW}"
        )

    # 2. 遍历 node+create 项执行三层冲突检测
    # 批量化（_build_conflict_query_vectors）：所有查询文本一次性 embed，
    # 再逐条用预计算向量比对。
    create_items = [
        it
        for it in batch.items
        if it.item_type == "node" and it.action == "create"
    ]
    conflict_query_vectors = await _build_conflict_query_vectors(
        embedding_client, create_items
    )

    all_conflicts: list[dict[str, Any]] = []
    candidates_total = 0
    checked_nodes = 0
    qv_index = 0

    for item in batch.items:
        if item.item_type != "node" or item.action != "create":
            continue

        checked_nodes += 1
        payload = item.payload or {}

        # 悬浮需求（project_id=None，依附 system）按 system_id 收口冲突候选域；
        # 归属需求仍按 project_id 检测。detect_conflicts 二者选一。
        _pid_raw = payload.get("project_id")
        _sid_raw = payload.get("system_id")
        _conflict_pid = _to_uuid(_pid_raw) if _pid_raw else None
        _conflict_sid = _to_uuid(_sid_raw) if _sid_raw else None

        conflict_result = await _svc.detect_conflicts(
            session,
            vector_searcher=vector_searcher,
            project_id=_conflict_pid,
            system_id=_conflict_sid,
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
        approved_batch = await _svc.review_approve(
            session,
            batch_id=batch_id,
            reviewed_by=reviewed_by,
            graph_store=graph_store,
            embedding_client=embedding_client,
            vector_searcher=vector_searcher,
            review_comment="auto_approved: no conflict detected",
            # 复用本函数 step2 已批量 embed 的冲突查询向量，消除 review_approve
            # 内部重复 embed（AUDIT §2.12）
            conflict_query_vectors=conflict_query_vectors,
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
    APPROVAL_BATCHES.labels(status="pending").inc()
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


async def submit_batch_with_mode(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    batch_type: str,
    submitted_by: str,
    submitter_role: str,
    items: list[dict[str, Any]],
    operation_id: str | None = None,
    lax_mode: bool,
    graph_store: GraphStore | None = None,
    embedding_client: EmbeddingClient | None = None,
    vector_searcher: VectorSearcher | None = None,
) -> tuple[ApprovalBatch, str | None]:
    """按提交方审核模式创建批次：宽松模式提交即自动处理（同一事务）。

    宽松模式（lax_mode=True 且全局 LAX_MODE_ENABLED=True）：
    1. 同一事务内先 submit_batch 建批（幂等 + submit 审计不变）
    2. 立即调用 auto_process_batch：无冲突 → approved 直接入库；有冲突 →
       needs_human_review 批次保持 pending
    返回 (batch, decision)；decision 为 "auto_approved"/"needs_human_review"
    或 None（严格模式/全局熔断，走正常审批）。

    全局开关在此单一判定：lax_mode 即便传 True，若 LAX_MODE_ENABLED=False 也强制走审批，
    防止绕过（与 config 注释一致）。

    graph_store/embedding_client/vector_searcher 仅宽松路径需要（为 auto_process_batch
    提供）；严格模式下传 None 即可。

    不 commit（提交与自动审批在同一事务，由调用方提交）。
    """
    from mem_lake.approval import service as _svc  # 门面：运行时解析，避免加载期循环依赖

    lax = lax_mode and _svc.get_settings().LAX_MODE_ENABLED

    batch = await _svc.submit_batch(
        session,
        project_id=project_id,
        batch_type=batch_type,
        submitted_by=submitted_by,
        submitter_role=submitter_role,
        items=items,
        operation_id=operation_id,
    )

    if not lax:
        return batch, None

    if graph_store is None or embedding_client is None or vector_searcher is None:
        raise RuntimeError(
            "宽松模式需要 graph_store/embedding_client/vector_searcher 依赖"
        )

    result = await _svc.auto_process_batch(
        session,
        batch_id=batch.id,
        reviewed_by=submitted_by,
        graph_store=graph_store,
        embedding_client=embedding_client,
        vector_searcher=vector_searcher,
    )
    return batch, result["decision"]
