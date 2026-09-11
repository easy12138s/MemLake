"""FastMCP 实例创建、工具注册、生命周期管理。

对齐 PDD 6.1：FastMCP 4.0 实例，cache_ttl=300s + cache_scope=private，
4 个中间件按序注册，lifespan 初始化共享资源（EmbeddingClient/GraphStore/VectorSearcher）。

不设置 auth= 参数：AccessKeyAuthMiddleware 在 on_request hook 中直接设置
request.scope["user"]，使 get_access_token() 正常工作（详见 gateway/middleware.py）。
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from mem_lake.knowledge.age_store import AGEGraphStore

logger = logging.getLogger("mem_lake.gateway.server")


@dataclass
class LifespanContext:
    """lifespan 共享资源容器。

    通过 ctx.lifespan_context 访问，供工具函数获取共享的 embedding/graph/search 实例。
    避免每次工具调用重新创建（EmbeddingClient 连接池/GraphStore 会话初始化有成本）。

    此 dataclass 的类型由 tools/_shared.get_lifespan_context() 经 cast 取回，
    工具层统一据此访问共享属性（FastMCP 泛化的 lifespan_context 类型为 dict）。
    """

    embedding_client: EmbeddingClient
    graph_store: "AGEGraphStore"  # 字符串前向引用；AGEGraphStore 仅 TYPE_CHECKING 导入（避免工具↔age_store 模块循环）
    vector_searcher: VectorSearcher


async def _detect_embedding_change(embedding_client: EmbeddingClient) -> None:
    """启动时检测 embedding 模型/provider 是否切换，切换则告警并留痕。

    读 embedding 服务 /health 的 provider+model 构造当前签名，与 embedding_state
    表最近一条比对：无历史写基线；一致跳过；不一致打 WARNING 并写入新记录。
    任何异常静默降级（不阻断启动）。
    """
    from sqlalchemy import select

    from mem_lake.db.session import AsyncSessionLocal
    from mem_lake.embedding.consistency import (
        compute_signature,
        decide_embedding_change,
    )
    from mem_lake.knowledge.models import EmbeddingState

    try:
        health = await embedding_client.health()
    except Exception as exc:  # noqa: BLE001 - 健康检查失败不阻断启动
        logger.warning("embedding 健康检查失败，跳过模型一致性检测: %s", exc)
        return

    current_sig = compute_signature(health)

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(EmbeddingState).order_by(EmbeddingState.detected_at.desc()).limit(1)
            )
            latest_row = result.scalar_one_or_none()
        latest = latest_row.signature if latest_row is not None else None

        action, changed = decide_embedding_change(latest, current_sig)

        if changed:
            logger.warning(
                "检测到 embedding 模型/提供商变更：%s -> %s。存量向量由旧模型生成、"
                "检索可能不准；请用 reindex_project_vectors 逐项目重嵌，或切回原 provider。",
                latest,
                current_sig,
            )

        if action != "unchanged":
            async with AsyncSessionLocal() as session:
                session.add(EmbeddingState(signature=current_sig))
                await session.commit()
    except Exception as exc:  # noqa: BLE001 - 检测失败不阻断启动
        logger.warning("embedding 模型一致性检测失败，跳过: %s", exc)


@lifespan
async def app_lifespan(server: FastMCP) -> AsyncIterator[Any]:
    """应用生命周期：启动时初始化共享资源，关闭时清理。

    PDD 6.1：共享资源通过 lifespan_context 传递给工具函数，
    避免每次工具调用重新创建（连接池/会话初始化有成本）。

    yield 前的代码在服务器启动时执行（初始化资源），
    yield 后的代码在服务器关闭时执行（清理资源）。

    返回类型标注为 AsyncIterator[Any]：fastmcp 的 lifespan 泛化类型为
    AsyncIterator[dict]，而本函数实际 yield LifespanContext 实例（工具层经
    get_lifespan_context() cast 取回），两者在运行时等价，故用 Any 兼容。
    """
    settings = get_settings()
    logger.info(
        "初始化 lifespan 资源：embedding=%s:%d, graph=%s",
        settings.EMBEDDING_HOST,
        settings.EMBEDDING_PORT,
        settings.AGE_GRAPH_NAME,
    )

    # DB 初始化：检查扩展 + 建业务表 + tsvector 触发器 + Alembic 迁移版本校验。
    # 项目隔离由应用层 validate_project_access + FilterSpec 实现（见 db/init.py 设计说明）。
    from mem_lake.db.init import (
        check_migrations_synced,
        create_tables,
        init_database,
        init_knowledge_schema,
    )
    from mem_lake.db.session import AsyncSessionLocal

    logger.info("执行数据库初始化检查...")
    await init_database()
    logger.info("扩展/FTS/AGE 图检查通过，开始建表...")
    async with AsyncSessionLocal() as session:
        await create_tables(session)
        await init_knowledge_schema(session)
        await session.commit()
    logger.info("业务表与 schema 初始化完成")

    # FIX-01：Alembic 迁移版本校验（create_tables 之后，确保增量迁移未被遗漏登记）
    async with AsyncSessionLocal() as session:
        await check_migrations_synced(session)
    logger.info("Alembic 迁移版本校验通过")

    # 启动对账：把残留 pending/running 的 reindex 任务置为 failed（进程重启后其 worker 已不存在）
    from mem_lake.gateway.background_tasks import reconcile_orphan_tasks

    await reconcile_orphan_tasks()

    # 初始化共享资源
    embedding_client = EmbeddingClient(
        base_url=f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}",
        dimension=settings.EMBEDDING_DIMENSION,
    )
    graph_store = get_graph_store()
    vector_searcher = VectorSearcher(embedding_client)

    # embedding 模型一致性检测：比对本次启动的 embedding 签名与上次记录，切换则告警。
    # 仅提醒（打 WARNING 日志），不阻断、不强制，由 admin 自行决定重嵌或回退。
    # 检测失败不阻断启动（与 embedding 依赖可降级的语义一致）。
    await _detect_embedding_change(embedding_client, settings)

    try:
        yield LifespanContext(
            embedding_client=embedding_client,
            graph_store=graph_store,
            vector_searcher=vector_searcher,
        )
    finally:
        logger.info("清理 lifespan 资源")
        # 显式关闭 httpx.AsyncClient（依赖 GC 在 asyncio 下有连接残留 /
        # ResourceWarning，见 client.close），GraphStore 无状态无需清理
        await embedding_client.close()


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
