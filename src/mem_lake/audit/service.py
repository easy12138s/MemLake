"""审计日志写入与查询服务。

append-only 语义：本模块仅提供 INSERT（write_audit_log）与 SELECT（query_audit_logs）路径，
禁止对 AuditLog 执行 update/delete。审计写入不 commit，由调用方控制事务，
保证审计记录与业务操作在同一事务内原子提交。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
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
    detail: dict[str, Any] | None = None,
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
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """查询审计日志。

    动态构建 WHERE 条件（非 None 才过滤），按 created_at DESC 排序，limit/offset 分页。
    project_id 过滤实现按项目隔离审计（Admin 审计追溯）。
    start_time/end_time 在 SQL 层过滤（created_at 范围），与分页组合正确
    （应用层过滤会导致跨页漏数据）。
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
    if start_time is not None:
        stmt = stmt.where(AuditLog.created_at >= start_time)
    if end_time is not None:
        stmt = stmt.where(AuditLog.created_at <= end_time)

    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@dataclass
class ActorUsageStats:
    """单个 actor（Access Key）的工具调用使用统计。"""

    total_calls: int
    error_calls: int
    last_used_at: datetime | None


async def get_actor_usage_stats(session: AsyncSession) -> dict[str, ActorUsageStats]:
    """按 actor(=key_id 字符串) 全局聚合 tool_call 使用统计。

    无调用记录的 actor 不出现在结果中。错误数按 detail.result_status='error'
    统计（JSONB 表达式，无索引）；last_used_at 为最近一次 tool_call 时间。

    不 commit。
    """
    result = await session.execute(
        text(
            """
            SELECT
                actor,
                count(*) AS total_calls,
                count(*) FILTER (WHERE detail->>'result_status' = 'error') AS error_calls,
                max(created_at) AS last_used_at
            FROM audit_log
            WHERE action = 'tool_call'
            GROUP BY actor
            """
        )
    )
    stats: dict[str, ActorUsageStats] = {}
    for actor, total, errors, last_used in result.all():
        stats[actor] = ActorUsageStats(
            total_calls=int(total),
            error_calls=int(errors),
            last_used_at=last_used,
        )
    return stats
