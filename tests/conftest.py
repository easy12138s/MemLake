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
import uuid
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
            "root_cause": "分布式环境下锁竞争",
            "solution": "引入 Redis 分布式锁",
            "severity": "P1",
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


# ============ M4 search fixtures ============


@pytest.fixture
def vector_searcher(real_embedding_client):
    """VectorSearcher 实例（基于真实 embedding 客户端）。

    依赖 real_embedding_client，embedding 容器未运行时自动 skip。
    用于集成测试中向量检索的实际调用场景验证。
    """
    from mem_lake.search.vector import VectorSearcher

    return VectorSearcher(real_embedding_client)


@pytest.fixture
def vector_searcher_mock(mock_embedding_client):
    """VectorSearcher 实例（基于 mock embedding 客户端）。

    用于不依赖真实 embedding 服务的测试场景（如过滤条件、top_k 限制等逻辑验证）。
    mock 返回固定 [0.1]*1024 向量，相似度计算无意义，仅验证流程正确性。
    """
    from mem_lake.search.vector import VectorSearcher

    return VectorSearcher(mock_embedding_client)


@pytest.fixture
def fulltext_searcher():
    """FullTextSearcher 实例（无构造依赖）。"""
    from mem_lake.search.fulltext import FullTextSearcher

    return FullTextSearcher()


@pytest.fixture
def graph_searcher(graph_store):
    """GraphSearcher 实例（基于 AGEGraphStore）。"""
    from mem_lake.search.graph import GraphSearcher

    return GraphSearcher(graph_store)


# ============ M5 approval fixtures ============


@pytest.fixture
def sample_batch_payloads(knowledge_helpers):
    """返回各类批次的 payload 模板，用于审批流测试快速构造 items。

    返回 dict 含三种批次类型的 items 列表（publish_requirement / submit_dev_artifacts /
    update_requirement_relations），调用方传入 submit_batch 的 items 参数。
    模板内 project_id/from_id/to_id 由调用方在提交前填入实际值。
    """
    req_props = knowledge_helpers["Requirement"]()
    code_props = knowledge_helpers["CodeSnippet"]()
    sol_props = knowledge_helpers["Solution"]()

    def _publish_requirement(project_id: uuid.UUID, created_by: str = "ak_pm") -> list[dict]:
        """publish_requirement 批次模板：1 个 Requirement 节点。"""
        return [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Requirement",
                    "title": "用户登录鉴权需求",
                    "content": "系统需要支持账号密码登录与 JWT 令牌签发",
                    "properties": req_props,
                    "tags": ["auth", "P0"],
                    "source": {"agent": "pm_agent", "tool": "publish_requirement"},
                    "created_by": created_by,
                },
            }
        ]

    def _submit_dev_artifacts(
        project_id: uuid.UUID,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        created_by: str = "ak_dev",
    ) -> list[dict]:
        """submit_dev_artifacts 批次模板：1 个 CodeSnippet + 1 个 Solution + 1 个 implements 边。"""
        return [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "CodeSnippet",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "CodeSnippet",
                    "title": "LoginService 类",
                    "content": "LoginService 负责用户登录鉴权，签发 JWT 令牌",
                    "properties": code_props,
                    "tags": ["auth", "service"],
                    "source": {"agent": "dev_agent", "tool": "submit_dev_artifacts"},
                    "created_by": created_by,
                },
            },
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Solution",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Solution",
                    "title": "JWT 鉴权方案",
                    "content": "采用 JWT 令牌方案，access token 30 分钟，refresh token 7 天",
                    "properties": sol_props,
                    "tags": ["auth", "jwt"],
                    "source": {"agent": "dev_agent", "tool": "submit_dev_artifacts"},
                    "created_by": created_by,
                },
            },
            {
                "item_type": "edge",
                "action": "create",
                "entity_type": "implements",
                "payload": {
                    "from_id": str(from_id),
                    "to_id": str(to_id),
                    "edge_type": "implements",
                    "properties": {"reason": "代码实现需求"},
                },
            },
        ]

    def _update_requirement_relations(
        from_id: uuid.UUID, to_id: uuid.UUID
    ) -> list[dict]:
        """update_requirement_relations 批次模板：1 个 conflicts_with 边。"""
        return [
            {
                "item_type": "edge",
                "action": "create",
                "entity_type": "conflicts_with",
                "payload": {
                    "from_id": str(from_id),
                    "to_id": str(to_id),
                    "edge_type": "conflicts_with",
                    "properties": {"reason": "需求间冲突"},
                },
            }
        ]

    return {
        "publish_requirement": _publish_requirement,
        "submit_dev_artifacts": _submit_dev_artifacts,
        "update_requirement_relations": _update_requirement_relations,
    }

