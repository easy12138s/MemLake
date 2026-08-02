"""pytest fixtures：测试 DB、Mock Embedding、AGE GraphStore、知识测试辅助。

event_loop：session 级 event_loop，支持 session 级 async fixture（建表）。
init_tables：session 级，建表一次（独立 session + commit），DDL 与 DML 隔离。
  并调用 init_knowledge_schema 创建 tsvector 触发器与 RLS 策略。
db_session：function 级，事务回滚隔离的测试 DB session，依赖 init_tables。
mock_embedding_client：对齐真实 EmbeddingClient 签名，异步方法用 AsyncMock，
  返回固定 1024 维向量。
real_embedding_client：function 级，连真实 embedding 容器（localhost:8001），
  每个测试创建独立 client 避免跨事件循环绑定问题。
graph_store：function 级，返回 AGEGraphStore 实例（基于 config 的 graph_name）。
knowledge_helpers：function 级，提供各类节点的合法 properties 构造方法。
"""

import asyncio
import sys
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

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
    随后调用 init_knowledge_schema 创建 tsvector 触发器与 RLS 策略（同样 commit）。
    """
    from mem_lake.db.init import create_tables, init_knowledge_schema

    async with AsyncSessionLocal() as session:
        await create_tables(session)
        await session.commit()

    # 触发器与 RLS 策略必须在表创建后建立，独立 session + commit
    async with AsyncSessionLocal() as session:
        await init_knowledge_schema(session)
        await session.commit()


@pytest.fixture
async def db_session(init_tables) -> AsyncGenerator[AsyncSession, None]:
    """事务回滚隔离的测试 DB session。

    整个测试在一个事务内执行，结束 rollback，不提交任何变更。
    适用于只读测试与写测试（写后 rollback）。依赖 init_tables 确保表已建。

    注意：AGE 图操作（CREATE/DELETE vertex/edge）属于 DML，会在事务回滚时一并回滚。
    """
    async with AsyncSessionLocal() as session:
        await session.begin()
        yield session
        await session.rollback()


@pytest.fixture
def mock_embedding_client() -> MagicMock:
    """返回固定 1024 维向量的 mock embedding 客户端。

    异步方法（embed/embed_one/health/close）用 AsyncMock，保证 await 可用。
    用于 unit 测试或不依赖真实 embedding 服务的场景。
    """
    client = MagicMock()
    client.embed = AsyncMock(return_value=[[0.1] * 1024])
    client.embed_one = AsyncMock(return_value=[0.1] * 1024)
    client.health = AsyncMock(return_value={"status": "ok", "model": "mock", "dimension": 1024})
    client.close = AsyncMock()
    return client


@pytest.fixture
async def real_embedding_client():
    """连真实 embedding 容器（localhost:8001）的 EmbeddingClient。

    function 级 fixture：每个测试创建独立 EmbeddingClient 实例，
    避免 httpx.AsyncClient 跨事件循环绑定问题。
    测试结束 close 释放连接。
    跳过条件：embedding 容器未运行时 health 检查失败，标记 skip。
    """
    from mem_lake.config import get_settings
    from mem_lake.embedding.client import EmbeddingClient, EmbeddingError

    settings = get_settings()
    client = EmbeddingClient(
        base_url=f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}",
        dimension=settings.EMBEDDING_DIMENSION,
    )
    try:
        await client.health()
    except EmbeddingError:
        await client.close()
        pytest.skip("embedding 容器未运行，跳过依赖真实 embedding 的测试")
    yield client
    await client.close()


@pytest.fixture
def graph_store():
    """返回 AGEGraphStore 实例（function 级，无状态可复用）。

    graph_name 从 config 读取（mem_lake_graph）。
    """
    from mem_lake.knowledge.age_store import get_graph_store

    return get_graph_store()


@pytest.fixture
def knowledge_helpers():
    """提供各类节点的合法 properties 构造方法。

    返回 dict，键为节点类型，值为合法 properties dict 工厂函数。
    用于 repository 集成测试快速构造合规节点数据。
    """

    def _project_profile() -> dict:
        return {
            "name": "TestProject",
            "description": "测试项目",
            "tech_stack": ["Python", "PostgreSQL"],
        }

    def _requirement() -> dict:
        return {
            "requirement_id": "REQ-2026-001",
            "priority": "P0",
            "module": "auth",
            "acceptance_criteria": ["账号密码登录"],
        }

    def _code_snippet() -> dict:
        return {
            "name": "LoginService",
            "type": "class",
            "responsibility": "处理用户登录鉴权",
            "file_path": "src/auth/login.py",
            "language": "python",
        }

    def _solution() -> dict:
        return {
            "approach": "JWT 令牌签发 + 中间件校验",
            "version": "1.0",
        }

    def _design_intent() -> dict:
        return {"rationale": "无状态、易扩展、适配微服务"}

    def _decision() -> dict:
        return {
            "decision_id": "DEC-001",
            "decision": "采用 JWT",
            "context": "需要支持多服务部署",
        }

    def _pitfall() -> dict:
        return {
            "symptom": "高并发下 token 续期冲突",
            "solution": "引入 Redis 分布式锁",
        }

    return {
        "ProjectProfile": _project_profile,
        "Requirement": _requirement,
        "CodeSnippet": _code_snippet,
        "Solution": _solution,
        "DesignIntent": _design_intent,
        "Decision": _decision,
        "Pitfall": _pitfall,
    }
