"""审批批次 CRUD 与提交（FIX-24 从 service.py 拆出的 repository 模块）。

职责：状态机常量与异常定义、批次 CRUD（submit_batch/_create_batch、
list_pending_batches、get_batch_detail、_find_by_idempotency_key）。

submit_batch 内的幂等重放 + SAVEPOINT（FIX-13）并发处理逻辑随提交一并收拢于此。
不 commit，由调用方（gateway 工具）控制事务边界。
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mem_lake.approval.models import ApprovalBatch, ApprovalItem
from mem_lake.approval.validation import (
    PayloadValidationError,
    _validate_item_payload,
    _validate_item_structure,
)
from mem_lake.audit.service import write_audit_log
from mem_lake.config import get_settings

# 批次类型白名单（PDD 3.4）
BATCH_TYPES: frozenset[str] = frozenset({
    "publish_requirement",
    "submit_dev_artifacts",
    "update_requirement_relations",
    "update_node",
})

# 状态机常量
STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# 终态集合（不可再转换）
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_APPROVED, STATUS_REJECTED})


class BatchNotFoundError(Exception):
    """批次不存在时抛出。"""


class BatchStatusError(Exception):
    """批次状态不允许当前操作时抛出（如已审批的批次再次审批）。"""


class IdempotencyConflictError(Exception):
    """幂等键冲突时抛出（同 operation_id 已有不同内容的批次）。"""


async def submit_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    batch_type: str,
    submitted_by: str,
    submitter_role: str,
    items: list[dict[str, Any]],
    operation_id: str | None = None,
) -> ApprovalBatch:
    """提交审批批次。

    参数：
        session: 异步数据库会话
        project_id: 归属项目（悬浮 system 需求批次可为 None）
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

    # 2. 幂等校验（先查询，命中直接返回已有批次）
    if operation_id is not None:
        existing = await _find_by_idempotency_key(
            session, submitted_by=submitted_by, batch_type=batch_type, operation_id=operation_id
        )
        if existing is not None:
            # 幂等重放：返回已有批次（含 items）
            return await get_batch_detail(session, existing.id)

    try:
        # FIX-13：_create_batch 包 begin_nested()（SAVEPOINT），IntegrityError 竞态时
        # 自动回滚至 savepoint，外事务状态不受影响（SQLAlchemy 标准恢复模式），
        # 取代此前手动 session.rollback()（会废弃整个外事务）。
        async with session.begin_nested():
            return await _create_batch(
                session,
                project_id=project_id,
                batch_type=batch_type,
                submitted_by=submitted_by,
                submitter_role=submitter_role,
                items=items,
                operation_id=operation_id,
            )
    except IntegrityError:
        # 并发同 operation_id 提交：两个请求都通过幂等检查后各自插入，
        # 第二个撞唯一约束 uq_approval_batch_idempotency。SAVEPOINT 已回滚本次
        # 未成功的插入（外事务仍可用），回查已存在的批次做幂等重放（AUDIT §2.17）。
        if operation_id is None:
            raise
        existing = await _find_by_idempotency_key(
            session, submitted_by=submitted_by, batch_type=batch_type, operation_id=operation_id
        )
        if existing is not None:
            return await get_batch_detail(session, existing.id)
        raise


async def _create_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    batch_type: str,
    submitted_by: str,
    submitter_role: str,
    items: list[dict[str, Any]],
    operation_id: str | None,
) -> ApprovalBatch:
    """创建审批批次的核心逻辑（submit_batch 内部）。"""
    # 3. 校验 items 非空与 payload 合规性
    if not items:
        raise PayloadValidationError("items 不能为空")

    if len(items) > get_settings().MAX_ITEMS_PER_BATCH:
        raise PayloadValidationError(
            f"批次 items 数量 {len(items)} 超过上限 {get_settings().MAX_ITEMS_PER_BATCH}"
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
