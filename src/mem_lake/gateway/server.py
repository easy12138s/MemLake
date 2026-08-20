"""FastMCP 实例创建、工具注册、生命周期管理。

对齐 PDD 6.1：FastMCP 4.0 实例，cache_ttl=300s + cache_scope=public，
4 个中间件按序注册，lifespan 初始化共享资源（EmbeddingClient/GraphStore/VectorSearcher）。

不设置 auth= 参数：AccessKeyAuthMiddleware 在 on_request hook 中直接设置
request.scope["user"]，使 get_access_token() 正常工作（详见 gateway/middleware.py）。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from mem_lake.config import get_settings
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.gateway.middleware import (
    AccessKeyAuthMiddleware,
    AuditLogMiddleware,
    RateLimitMiddleware,
    RBACMiddleware,
)
from mem_lake.knowledge.age_store import get_graph_store
from mem_lake.search.vector import VectorSearcher

logger = logging.getLogger("mem_lake.gateway.server")


@dataclass
class LifespanContext:
    """lifespan 共享资源容器。

    通过 ctx.lifespan_context 访问，供工具函数获取共享的 embedding/graph/search 实例。
    避免每次工具调用重新创建（EmbeddingClient 连接池/GraphStore 会话初始化有成本）。
    """

    embedding_client: EmbeddingClient
    graph_store: "AGEGraphStore"  # noqa: F821（避免循环导入，用字符串引用）
    vector_searcher: VectorSearcher


@lifespan
async def app_lifespan(server: FastMCP) -> AsyncIterator[LifespanContext]:
    """应用生命周期：启动时初始化共享资源，关闭时清理。

    PDD 6.1：共享资源通过 lifespan_context 传递给工具函数，
    避免每次工具调用重新创建（连接池/会话初始化有成本）。

    yield 前的代码在服务器启动时执行（初始化资源），
    yield 后的代码在服务器关闭时执行（清理资源）。
    """
    settings = get_settings()
    logger.info(
        "初始化 lifespan 资源：embedding=%s:%d, graph=%s",
        settings.EMBEDDING_HOST,
        settings.EMBEDDING_PORT,
        settings.AGE_GRAPH_NAME,
    )

    # DB 初始化：检查扩展 + 建业务表 + tsvector 触发器 + RLS 策略
    from mem_lake.db.init import init_database, create_tables, init_knowledge_schema
    from mem_lake.db.session import AsyncSessionLocal

    logger.info("执行数据库初始化检查...")
    await init_database()
    logger.info("扩展/FTS/AGE 图检查通过，开始建表...")
    async with AsyncSessionLocal() as session:
        await create_tables(session)
        await init_knowledge_schema(session)
        await session.commit()
    logger.info("业务表与 schema 初始化完成")

    # 初始化共享资源
    embedding_client = EmbeddingClient(
        base_url=f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}",
        dimension=settings.EMBEDDING_DIMENSION,
    )
    graph_store = get_graph_store()
    vector_searcher = VectorSearcher(embedding_client)

    try:
        yield LifespanContext(
            embedding_client=embedding_client,
            graph_store=graph_store,
            vector_searcher=vector_searcher,
        )
    finally:
        logger.info("清理 lifespan 资源")
        # EmbeddingClient 使用 httpx.AsyncClient，由 GC 自动清理
        # GraphStore 无需显式清理（使用共享连接池）


def create_mcp_server() -> FastMCP:
    """创建 FastMCP 实例并注册所有工具。

    配置：
    - cache_ttl=300s + cache_scope=private：MCP 2026-07-28 缓存特性
      （private 避免角色相关的 tools/list 响应被共享缓存跨角色复用）
    - middleware：4 个中间件按序注册（认证→鉴权→限流→审计）
    - lifespan：初始化共享资源
    - auth 不设：AccessKeyAuthMiddleware 负责 X-MCP-Key 认证 + 设置 scope["user"]
    """
    settings = get_settings()

    mcp = FastMCP(
        name=settings.MCP_SERVER_NAME,
        cache_ttl=300,
        cache_scope="private",
        # auth 不设：AccessKeyAuthMiddleware 负责 X-MCP-Key 认证 + 设置 scope["user"]
        middleware=[
            AccessKeyAuthMiddleware(),
            RBACMiddleware(),
            RateLimitMiddleware(),
            AuditLogMiddleware(),
        ],
        lifespan=app_lifespan,
    )

    # 注册所有角色工具
    from mem_lake.gateway.tools import register_all_tools

    register_all_tools(mcp)

    logger.info("FastMCP 实例创建完成，工具已注册")
    return mcp
