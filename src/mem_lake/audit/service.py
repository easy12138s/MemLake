"""审计日志写入与查询服务。

append-only 语义：本模块仅提供 INSERT（write_audit_log）与 SELECT（query_audit_logs）路径，
禁止对 AuditLog 执行 update/delete。审计写入不 commit，由调用方控制事务，
保证审计记录与业务操作在同一事务内原子提交。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.audit.models import AuditLog


async def write_audit_log(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    operation_id: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    """写入一条审计日志。

    构造 AuditLog 对象，session.add() + flush()（返回对象含生成的 id 与 created_at）。
    不 commit，由调用方控制事务。
    """
    log = AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        project_id=project_id,
        operation_id=operation_id,
        detail=detail or {},
    )
    session.add(log)
    await session.flush()
    return log


async def query_audit_logs(
    session: AsyncSession,
    *,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """查询审计日志。

    动态构建 WHERE 条件（非 None 才过滤），按 created_at DESC 排序，limit/offset 分页。
    project_id 过滤实现按项目隔离审计（Admin 审计追溯）。
    """
    stmt = select(AuditLog)
    if actor is not None:
        stmt = stmt.where(AuditLog.actor == actor)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if project_id is not None:
        stmt = stmt.where(AuditLog.project_id == project_id)

    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())
