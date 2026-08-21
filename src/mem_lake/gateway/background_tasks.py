"""向量重嵌后台任务管理（异步任务化 reindex）。

设计背景：
- 部署为 uvicorn --workers 4，多 worker 进程；任务状态必须落库（reindex_task 表）
  才能跨 worker、跨重启一致，且天然可审计。
- reindex_project_vectors 工具改为「提交即返回 task_id」，真正重嵌由本模块的
  _reindex_worker 在后台协程执行，彻底解耦客户端 MCP 调用超时。
- worker 内部采用批量 embed + offset 分页遍历全部节点 + 每批独立事务，
  既大幅提速（减少 HTTP 往返），又根治「>500 节点遗漏」与「56s 长事务」问题。
- 防重入：同一项目已有 pending/running 任务时，新提交直接返回已有 task_id，
  避免客户端超时重试又触发全量重嵌。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from mem_lake.embedding.client import get_embedding_client
from mem_lake.gateway.dependencies import (
    get_readonly_session,
    transactional_session,
)
from mem_lake.gateway.models import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    ReindexTask,
)
from mem_lake.knowledge.repository import (
    batch_regenerate_vectors,
    count_nodes_by_project,
    list_nodes_by_project,
)

logger = logging.getLogger("mem_lake.gateway.background_tasks")

# 持有进行中 asyncio.Task 引用，防止被 GC（任务在接收请求的 worker 事件循环上运行）。
# 任务完成后由 done_callback 自动移除。
ACTIVE_TASKS: set[asyncio.Task] = set()

DEFAULT_BATCH_SIZE = 50


# ============================================================================
# 任务 CRUD（落库）
# ============================================================================


async def create_task_record(project_id: uuid.UUID, actor: str) -> uuid.UUID:
    """插入一条 pending 任务记录，返回 task_id。"""
    task_id = uuid.uuid4()
    async with transactional_session() as session:
        session.add(
            ReindexTask(id=task_id, project_id=project_id, status=TASK_STATUS_PENDING, created_by=actor)
        )
    return task_id


async def get_task_record(task_id: uuid.UUID) -> ReindexTask | None:
    """按 task_id 查询任务记录（只读）。"""
    session = await get_readonly_session()
    try:
        result = await session.execute(
            select(ReindexTask).where(ReindexTask.id == task_id)
        )
        return result.scalar_one_or_none()
    finally:
        await session.close()


async def find_running_task(project_id: uuid.UUID) -> ReindexTask | None:
    """查项目是否有 pending/running 任务（防重入用）。返回最新一条或 None。"""
    session = await get_readonly_session()
    try:
        result = await session.execute(
            select(ReindexTask)
            .where(
                ReindexTask.project_id == project_id,
                ReindexTask.status.in_([TASK_STATUS_PENDING, TASK_STATUS_RUNNING]),
            )
            .order_by(ReindexTask.created_at.desc())
        )
        return result.scalars().first()
    finally:
        await session.close()


async def _patch_task(task_id: uuid.UUID, **fields) -> None:
    """补丁式更新任务字段（统一带 updated_at）。独立事务。"""
    fields["updated_at"] = datetime.now(timezone.utc)
    async with transactional_session() as session:
        await session.execute(
            update(ReindexTask).where(ReindexTask.id == task_id).values(**fields)
        )


# ============================================================================
# 后台 worker
# ============================================================================


async def _reindex_worker(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    actor: str,
    batch_size: int,
    embedding_client,  # EmbeddingClient
) -> None:
    """后台重嵌协程：分页遍历全部 approved 节点，批量向量化并写回。

    状态机：pending → running → done / failed。进度（processed/total）实时落库，
    供 get_reindex_status 轮询。异常时整体标记 failed 并记录 error。
    """
    try:
        await _patch_task(
            task_id, status=TASK_STATUS_RUNNING, started_at=datetime.now(timezone.utc)
        )

        async with transactional_session() as session:
            total = await count_nodes_by_project(
                session, project_id=project_id, status="approved"
            )
        await _patch_task(task_id, total=total)

        processed = 0
        offset = 0
        while True:
            async with transactional_session() as session:
                nodes = await list_nodes_by_project(
                    session,
                    project_id=project_id,
                    status="approved",
                    limit=batch_size,
                    offset=offset,
                    order_by="id",
                )
                if not nodes:
                    break
                await batch_regenerate_vectors(
                    session,
                    embedding_client=embedding_client,
                    nodes=nodes,
                    actor=actor,
                )
            processed += len(nodes)
            await _patch_task(task_id, processed=processed, reindexed=processed)
            offset += batch_size

        await _patch_task(
            task_id,
            status=TASK_STATUS_DONE,
            reindexed=processed,
            finished_at=datetime.now(timezone.utc),
        )
        logger.info(
            "reindex task %s done: project=%s reindexed=%d",
            task_id,
            project_id,
            processed,
        )
    except Exception as exc:  # noqa: BLE001 - 需捕获所有异常以落库失败原因
        logger.exception("reindex task %s failed: %s", task_id, exc)
        await _patch_task(
            task_id,
            status=TASK_STATUS_FAILED,
            error=str(exc)[:2000],
            finished_at=datetime.now(timezone.utc),
        )


async def start_reindex_task(
    project_id: uuid.UUID, actor: str, batch_size: int | None = None
) -> uuid.UUID:
    """异步提交重嵌任务：建记录 + 启动后台 worker，返回 task_id。"""
    size = batch_size or DEFAULT_BATCH_SIZE
    task_id = await create_task_record(project_id, actor)
    embedding_client = get_embedding_client()
    task = asyncio.create_task(
        _reindex_worker(project_id, task_id, actor, size, embedding_client)
    )
    ACTIVE_TASKS.add(task)
    task.add_done_callback(ACTIVE_TASKS.discard)
    logger.info(
        "reindex task %s started: project=%s batch_size=%d", task_id, project_id, size
    )
    return task_id


# ============================================================================
# 启动对账
# ============================================================================


async def reconcile_orphan_tasks() -> int:
    """启动期把残留的 pending/running 任务标记为 failed。

    进程重启后这些任务的 worker 已不复存在，置为失败以便 admin 重新发起，
    避免任务永远停留在「进行中」误导轮询。
    """
    async with transactional_session() as session:
        result = await session.execute(
            select(ReindexTask).where(
                ReindexTask.status.in_([TASK_STATUS_PENDING, TASK_STATUS_RUNNING])
            )
        )
        tasks = result.scalars().all()
        for t in tasks:
            t.status = TASK_STATUS_FAILED
            t.error = "服务器重启前 worker 未结束，已置为失败，请重新发起"
            t.finished_at = datetime.now(timezone.utc)
    logger.info("reconciled %d orphan reindex tasks", len(tasks))
    return len(tasks)
