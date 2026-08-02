"""pytest fixtures：测试 DB、Mock Embedding。

db_session：连接真实 PostgreSQL（localhost:5432），用 begin/rollback 事务隔离，
            测试结束回滚，避免污染数据。供 integration 测试复用。
mock_embedding_client：返回固定 1024 维向量，供 unit 测试避免加载真实 bge 模型。
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


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """事务回滚隔离的测试 DB session。

    整个测试在一个事务内执行，结束 rollback，不提交任何变更。
    适用于 M1 的只读测试，以及 M2+ 的写测试（写后 rollback）。
    """
    async with AsyncSessionLocal() as session:
        await session.begin()
        yield session
        await session.rollback()


@pytest.fixture
def mock_embedding_client() -> MagicMock:
    """返回固定 1024 维向量的 mock embedding 客户端。

    M1 占位实现，M2 完善 embed/health 等方法签名对齐真实 EmbeddingClient。
    """
    client = MagicMock()
    client.embed.return_value = [[0.1] * 1024]
    client.health.return_value = {"status": "ok", "dimension": 1024}
    return client
