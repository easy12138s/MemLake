"""background_tasks（异步 reindex）单元测试。

覆盖：
- _reindex_worker：分页遍历全部 approved 节点、批量 embed 写回、进度/状态机、空项目
- batch_regenerate_vectors / count_nodes_by_project：批量向量化与计数
- find_running_task：仅返回 pending/running（防重入判定基础），done/failed 不返回
- reconcile_orphan_tasks：启动对账把残留 pending/running 置为 failed
- start_reindex_task：提交即返回 task_id 并调度后台 worker（任务落库、跨 worker 可用）

不依赖真实 embedding 服务：mock embedding 客户端替代。
"""

import datetime
import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select

from mem_lake.db.session import AsyncSessionLocal
from mem_lake.gateway import background_tasks
from mem_lake.gateway.background_tasks import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    _reindex_worker,
    create_task_record,
    find_running_task,
    get_task_record,
    reconcile_orphan_tasks,
    start_embed_nodes_task,
    start_reindex_task,
)
from mem_lake.gateway.models import ReindexTask
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import (
    batch_regenerate_vectors,
    count_nodes_by_project,
)


async def _seed_nodes(project_id: uuid.UUID, n: int, with_vector: bool = False) -> None:
    """向 knowledge_node 直接插入 n 个 approved 节点（绕过 AGE 图写入）。

    reindex/worker 仅读 PG 表并写回 content_vector，故直接插 ORM 即可满足测试。
    with_vector=True 时预置向量（用于 batch_regenerate_vectors 覆盖写回验证）。
    """
    async with AsyncSessionLocal() as session:
        for i in range(n):
            session.add(
                KnowledgeNode(
                    project_id=project_id,
                    type="CodeSnippet",
                    title=f"node-{i}",
                    content=f"content {i}",
                    content_vector=([0.0] * 1024) if with_vector else None,
                    status="approved",
                    is_deleted=False,
                    created_by="ak_seed",
                    properties={"name": f"n{i}"},
                    tags=[],
                )
            )
        await session.commit()


def _mock_embed(batch_client) -> None:
    """让 mock embedding 客户端按输入文本数返回等长向量列表。"""
    batch_client.embed = AsyncMock(
        side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
    )


async def test_reindex_worker_processes_all_with_pagination(init_tables, mock_embedding_client):
    """batch_size 小于节点总数时，worker 分页遍历全部节点并批量向量化。

    验证：status=done、processed==total==N、所有节点 content_vector 非空。
    """
    _mock_embed(mock_embedding_client)
    project_id = uuid.uuid4()
    await _seed_nodes(project_id, 7)

    task_id = await create_task_record(project_id, "ak_admin")
    # batch_size=3 → 7 个节点分 3 批（3/3/1）
    await _reindex_worker(project_id, task_id, "ak_admin", 3, mock_embedding_client)

    record = await get_task_record(task_id)
    assert record.status == TASK_STATUS_DONE
    assert record.total == 7
    assert record.processed == 7
    assert record.reindexed == 7
    assert record.error is None

    async with AsyncSessionLocal() as s:
        nodes = (await s.execute(
            select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        )).scalars().all()
    assert len(nodes) == 7
    assert all(n.content_vector is not None for n in nodes)


async def test_reindex_worker_empty_project(init_tables, mock_embedding_client):
    """空项目 reindex：total=0、processed=0、status=done（不报错）。"""
    _mock_embed(mock_embedding_client)
    project_id = uuid.uuid4()

    task_id = await create_task_record(project_id, "ak_admin")
    await _reindex_worker(project_id, task_id, "ak_admin", 50, mock_embedding_client)

    record = await get_task_record(task_id)
    assert record.status == TASK_STATUS_DONE
    assert record.total == 0
    assert record.processed == 0


async def test_reindex_worker_marks_failed_on_error(init_tables, mock_embedding_client):
    """embed 抛错时 worker 将任务置为 failed 并记录 error（不崩溃）。"""
    mock_embedding_client.embed = AsyncMock(side_effect=RuntimeError("embedding down"))
    project_id = uuid.uuid4()
    await _seed_nodes(project_id, 3)

    task_id = await create_task_record(project_id, "ak_admin")
    await _reindex_worker(project_id, task_id, "ak_admin", 50, mock_embedding_client)

    record = await get_task_record(task_id)
    assert record.status == TASK_STATUS_FAILED
    assert record.error is not None and "embedding down" in record.error


async def test_reindex_worker_node_scope_fills_target_nodes(
    init_tables, mock_embedding_client
):
    """节点级嵌入：仅嵌入 target_node_ids 指定的节点（审批异步嵌入路径）。

    验证：worker 跳过整库扫描，仅对指定节点批量向量化；任务 total=1、status=done，
    指定节点 content_vector 被填充，其余节点保持 NULL。
    """
    _mock_embed(mock_embedding_client)
    project_id = uuid.uuid4()
    await _seed_nodes(project_id, 3)  # 3 个节点，仅嵌入其中 1 个

    async with AsyncSessionLocal() as s:
        seeded = (
            await s.execute(
                select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
            )
        ).scalars().all()
    target = seeded[0]
    other_ids = [n.id for n in seeded[1:]]

    task_id = await create_task_record(
        project_id, "ak_admin", target_node_ids=[target.id]
    )
    await _reindex_worker(project_id, task_id, "ak_admin", 50, mock_embedding_client)

    record = await get_task_record(task_id)
    assert record.status == TASK_STATUS_DONE
    assert record.total == 1
    assert record.reindexed == 1

    async with AsyncSessionLocal() as s:
        refreshed = (
            await s.execute(
                select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
            )
        ).scalars().all()
    by_id = {n.id: n for n in refreshed}
    assert by_id[target.id].content_vector is not None
    # 其余节点未被嵌入（仍为 NULL）
    for oid in other_ids:
        assert by_id[oid].content_vector is None


async def test_start_embed_nodes_task_schedules_node_scope_worker(
    init_tables, mock_embedding_client, monkeypatch
):
    """start_embed_nodes_task 建节点级任务记录并调度后台 worker。

    验证：返回 task_id、reindex_task.target_node_ids 正确落库，且 worker 完成
    后指定节点被向量化。
    """
    import asyncio

    _mock_embed(mock_embedding_client)
    monkeypatch.setattr(
        background_tasks, "get_embedding_client", lambda: mock_embedding_client
    )
    # 捕获调度的 asyncio.Task，便于测试内 await 完成
    scheduled: list[asyncio.Task] = []
    original_create = asyncio.create_task

    def _capture(task, *a, **k):
        t = original_create(task, *a, **k)
        scheduled.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _capture)

    project_id = uuid.uuid4()
    await _seed_nodes(project_id, 2)
    async with AsyncSessionLocal() as s:
        seeded = (
            await s.execute(
                select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
            )
        ).scalars().all()
    target_ids = [seeded[0].id]

    task_id = await start_embed_nodes_task(project_id, target_ids, "ak_admin")
    record = await get_task_record(task_id)
    assert record.target_node_ids == target_ids

    # 驱动后台 worker 完成
    for t in scheduled:
        await t
    record = await get_task_record(task_id)
    assert record.status == TASK_STATUS_DONE
    assert record.reindexed == 1

    async with AsyncSessionLocal() as s:
        refreshed = (
            await s.execute(
                select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
            )
        ).scalars().all()
    by_id = {n.id: n for n in refreshed}
    assert by_id[seeded[0].id].content_vector is not None
    assert by_id[seeded[1].id].content_vector is None


async def test_find_running_task_states(init_tables):
    """find_running_task 仅返回 pending/running；done/failed 不返回。"""
    project_id = uuid.uuid4()

    pending_id = await create_task_record(project_id, "ak_admin")  # pending
    running_id = uuid.uuid4()
    async with background_tasks.transactional_session() as session:
        session.add(ReindexTask(
            id=running_id, project_id=project_id, status=TASK_STATUS_RUNNING,
            created_by="ak_admin",
            started_at=datetime.datetime.now(datetime.timezone.utc),
        ))
    done_id = uuid.uuid4()
    async with background_tasks.transactional_session() as session:
        session.add(ReindexTask(
            id=done_id, project_id=project_id, status=TASK_STATUS_DONE,
            created_by="ak_admin",
        ))

    found = await find_running_task(project_id)
    assert found is not None
    # 返回最新一条 running/pending
    assert found.id in (pending_id, running_id)
    assert found.id != done_id


async def test_reconcile_orphan_tasks(init_tables):
    """启动对账：pending/running 置为 failed，done 保持不变。"""
    project_id = uuid.uuid4()
    orphan_pending = uuid.uuid4()
    orphan_running = uuid.uuid4()
    finished = uuid.uuid4()
    async with background_tasks.transactional_session() as session:
        session.add(ReindexTask(id=orphan_pending, project_id=project_id, status=TASK_STATUS_PENDING, created_by="ak"))
        session.add(ReindexTask(id=orphan_running, project_id=project_id, status=TASK_STATUS_RUNNING, created_by="ak"))
        session.add(ReindexTask(id=finished, project_id=project_id, status=TASK_STATUS_DONE, created_by="ak"))

    orphan_count = await reconcile_orphan_tasks()
    # 至少覆盖本测试插入的 2 个残留任务（共享 DB 可能含其他测试的残留，故用 >=）
    assert orphan_count >= 2

    async with AsyncSessionLocal() as s:
        recs = (await s.execute(
            select(ReindexTask).where(ReindexTask.project_id == project_id)
        )).scalars().all()
    by_id = {r.id: r for r in recs}
    assert by_id[orphan_pending].status == TASK_STATUS_FAILED
    assert by_id[orphan_running].status == TASK_STATUS_FAILED
    assert by_id[finished].status == TASK_STATUS_DONE


async def test_start_reindex_task_schedules_worker(init_tables, mock_embedding_client, monkeypatch):
    """start_reindex_task 立即返回 task_id，并调度后台 worker 完成重嵌（落库跨进程可用）。"""
    _mock_embed(mock_embedding_client)
    monkeypatch.setattr(
        background_tasks, "get_embedding_client", lambda: mock_embedding_client
    )
    project_id = uuid.uuid4()
    await _seed_nodes(project_id, 5)

    task_id = await start_reindex_task(project_id, "ak_admin", batch_size=2)

    # 等待调度出的后台 worker 完成
    scheduled = next(iter(background_tasks.ACTIVE_TASKS))
    await scheduled

    record = await get_task_record(task_id)
    assert record.status == TASK_STATUS_DONE
    assert record.processed == 5
    assert record.total == 5


async def test_batch_regenerate_vectors_and_count(init_tables, mock_embedding_client):
    """batch_regenerate_vectors 批量写回向量；count_nodes_by_project 计数准确。"""
    _mock_embed(mock_embedding_client)
    project_id = uuid.uuid4()
    await _seed_nodes(project_id, 4, with_vector=True)  # 预置向量，验证被覆盖

    async with background_tasks.transactional_session() as session:
        nodes = await background_tasks.list_nodes_by_project(
            session, project_id=project_id, status="approved"
        )
        assert len(nodes) == 4
        count = await count_nodes_by_project(session, project_id=project_id, status="approved")
        assert count == 4

        updated = await batch_regenerate_vectors(
            session, embedding_client=mock_embedding_client, nodes=nodes, actor="ak_admin"
        )
        assert updated == 4

    # 验证向量被覆盖为 0.1（来自 mock）
    async with AsyncSessionLocal() as s:
        recs = (await s.execute(
            select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        )).scalars().all()
    assert all(n.content_vector == [0.1] * 1024 for n in recs)
