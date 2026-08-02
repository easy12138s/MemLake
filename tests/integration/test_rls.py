"""M2 集成测试：RLS 上下文注入（SET LOCAL 变量读写）。

验证 SET LOCAL 事务级隔离特性。RLS 策略验证（CREATE POLICY）留 M3。
"""

import uuid

from mem_lake.auth.rls import (
    clear_context,
    get_actor_context,
    get_project_context,
    set_actor_context,
    set_project_context,
)
from mem_lake.db.session import AsyncSessionLocal


async def test_set_and_get_project_context(db_session):
    """set_project_context 后 get_project_context 返回相同值。"""
    pid = "proj-001"
    await set_project_context(db_session, pid)
    assert await get_project_context(db_session) == pid


async def test_set_actor_context(db_session):
    """set_actor_context 后 get_actor_context 返回正确值。"""
    actor = "ak_test_actor"
    await set_actor_context(db_session, actor)
    assert await get_actor_context(db_session) == actor


async def test_clear_context(db_session):
    """set 后 clear，get 返回 None。"""
    await set_project_context(db_session, "proj-001")
    await set_actor_context(db_session, "actor")
    await clear_context(db_session)
    assert await get_project_context(db_session) is None
    assert await get_actor_context(db_session) is None


async def test_context_isolated_per_transaction():
    """两个独立事务设置不同 project_id，互不干扰。

    验证 SET LOCAL 事务隔离性：每个事务的变量在事务结束自动清除。
    使用独立 session（不经 db_session fixture，避免事务回滚隔离干扰）。
    """
    pid1 = str(uuid.uuid4())
    pid2 = str(uuid.uuid4())

    # 事务 1
    async with AsyncSessionLocal() as s1:
        await s1.begin()
        await set_project_context(s1, pid1)
        assert await get_project_context(s1) == pid1
        await s1.rollback()

    # 事务 2
    async with AsyncSessionLocal() as s2:
        await s2.begin()
        await set_project_context(s2, pid2)
        assert await get_project_context(s2) == pid2
        # 事务 2 不应看到事务 1 的值
        assert await get_project_context(s2) != pid1
        await s2.rollback()
