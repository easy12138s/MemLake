"""pytest fixtures：测试 DB、Mock Embedding。

event_loop：session 级 event_loop，支持 session 级 async fixture（建表）。
init_tables：session 级，建表一次（独立 session + commit），DDL 与 DML 隔离。
db_session：function 级，事务回滚隔离的测试 DB session，依赖 init_tables。
mock_embedding_client：对齐真实 EmbeddingClient 签名，返回固定 1024 维向量。
"""

import asyncio
import sys
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

# Windows 下 psycopg3 async 不兼容默认的 ProactorEventLoop，切换到 SelectorEventLoop。
# 仅影响测试环境；Linux 生产部署不存在此问题。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.db.session import AsyncSessionLocal


@pytest.fixture(scope="session")
def event_loop():
    """session 级 event_loop，支持 session 级 async fixture。

    覆盖 pytest-asyncio 默认 function-scoped event_loop。
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def init_tables():
    """session 级建表 fixture。

    调用 create_tables 建表一次（独立 session + commit），session 结束不 drop（幂等）。
    DDL 在此提交，DML 在 db_session fixture 回滚，两者隔离。
    """
    from mem_lake.db.init import create_tables

    async with AsyncSessionLocal() as session:
        await create_tables(session)
        await session.commit()


@pytest.fixture
async def db_session(init_tables) -> AsyncGenerator[AsyncSession, None]:
    """事务回滚隔离的测试 DB session。

    整个测试在一个事务内执行，结束 rollback，不提交任何变更。
    适用于只读测试与写测试（写后 rollback）。依赖 init_tables 确保表已建。
    """
    async with AsyncSessionLocal() as session:
        await session.begin()
        yield session
        await session.rollback()


@pytest.fixture
def mock_embedding_client() -> MagicMock:
    """返回固定 1024 维向量的 mock embedding 客户端。

    方法对齐真实 EmbeddingClient（embed/embed_one/health/close）。
    """
    client = MagicMock()
    client.embed.return_value = [[0.1] * 1024]
    client.embed_one.return_value = [0.1] * 1024
    client.health.return_value = {"status": "ok", "model": "mock", "dimension": 1024}
    client.close = MagicMock()
    return client
