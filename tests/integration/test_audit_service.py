"""M2 集成测试：审计日志写入与查询服务。

事务回滚隔离，验证 write_audit_log 与 query_audit_logs。
"""

import uuid

from mem_lake.audit.service import query_audit_logs, write_audit_log


async def test_write_audit_log(db_session):
    """写入后 flush，对象含 id 与 created_at，字段正确。"""
    log = await write_audit_log(
        db_session,
        actor="ak_test_actor",
        action="write",
        target_type="node",
        target_id=uuid.uuid4(),
        operation_id="op_001",
        detail={"key": "value"},
    )
    assert log.id is not None
    assert log.created_at is not None
    assert log.actor == "ak_test_actor"
    assert log.action == "write"
    assert log.target_type == "node"
    assert log.operation_id == "op_001"
    assert log.detail == {"key": "value"}


async def test_query_by_actor(db_session):
    """按 actor 过滤返回正确子集。"""
    await write_audit_log(db_session, actor="actor_a", action="write", target_type="node")
    await write_audit_log(db_session, actor="actor_b", action="write", target_type="node")

    result = await query_audit_logs(db_session, actor="actor_a")
    assert len(result) == 1
    assert result[0].actor == "actor_a"


async def test_query_by_action(db_session):
    """按 action 过滤。"""
    await write_audit_log(db_session, actor="a1", action="write", target_type="node")
    await write_audit_log(db_session, actor="a2", action="approve", target_type="node")

    result = await query_audit_logs(db_session, action="approve")
    assert len(result) == 1
    assert result[0].action == "approve"


async def test_query_pagination(db_session):
    """写入 3 条，limit=2 offset=0 返回 2 条，offset=2 返回 1 条。

    用唯一 actor 过滤，隔离库中已提交的历史审计行（e2e 中间件写入）。
    """
    actor = "pagination_probe"
    for i in range(3):
        await write_audit_log(
            db_session, actor=actor, action="write", target_type="node"
        )

    page1 = await query_audit_logs(db_session, actor=actor, limit=2, offset=0)
    page2 = await query_audit_logs(db_session, actor=actor, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 1


async def test_target_id_nullable(db_session):
    """target_id=None 时正常写入与查询。"""
    log = await write_audit_log(
        db_session, actor="actor_x", action="archive", target_type="access_key"
    )
    assert log.target_id is None

    result = await query_audit_logs(db_session, actor="actor_x")
    assert len(result) == 1
    assert result[0].target_id is None


async def test_query_by_project_id_isolation(db_session):
    """按 project_id 过滤实现项目隔离（问题 4 修复）。

    写入两个不同项目的 node 写日志，按 project_a 查询只返回该项目的日志，
    不混入 project_b 的日志。
    """
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()

    await write_audit_log(
        db_session,
        actor="ak_a",
        action="write",
        target_type="node",
        target_id=uuid.uuid4(),
        project_id=project_a,
    )
    await write_audit_log(
        db_session,
        actor="ak_b",
        action="write",
        target_type="node",
        target_id=uuid.uuid4(),
        project_id=project_b,
    )

    result_a = await query_audit_logs(db_session, project_id=project_a)
    assert len(result_a) == 1
    assert result_a[0].project_id == project_a

    result_b = await query_audit_logs(db_session, project_id=project_b)
    assert len(result_b) == 1
    assert result_b[0].project_id == project_b


async def test_query_project_id_excludes_null(db_session):
    """跨项目 admin 操作（project_id 为空）在按项目过滤时不返回。"""
    project_a = uuid.uuid4()
    await write_audit_log(
        db_session,
        actor="ak_a",
        action="write",
        target_type="node",
        target_id=uuid.uuid4(),
        project_id=project_a,
    )
    # access_key 管理类操作无项目维度
    await write_audit_log(
        db_session, actor="ak_admin", action="create", target_type="access_key"
    )

    result = await query_audit_logs(db_session, project_id=project_a)
    assert len(result) == 1
    assert result[0].project_id == project_a
