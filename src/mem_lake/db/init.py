"""扩展安装与初始化：CREATE EXTENSION age/pgvector/zhparser、AGE 图创建。

职责边界：仅做幂等存在性检查（fail-fast），不重复 CREATE EXTENSION。
扩展与图的实体创建由 deploy/init/001_extensions.sql 在容器启动时完成，
应用启动时调用 init_database() 验证它们就位，缺失则抛 RuntimeError 指引修复。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.config import get_settings

REQUIRED_EXTENSIONS = ("age", "vector", "zhparser")


async def check_extensions(session: AsyncSession) -> dict[str, bool]:
    """检查三个扩展是否已安装。返回 {扩展名: 是否已安装}。"""
    result = await session.execute(
        text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
        {"names": list(REQUIRED_EXTENSIONS)},
    )
    installed = {row[0] for row in result}
    return {ext: ext in installed for ext in REQUIRED_EXTENSIONS}


async def check_chinese_fts(session: AsyncSession) -> bool:
    """检查中文全文检索配置 'chinese' 是否存在。"""
    result = await session.execute(
        text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'")
    )
    return result.first() is not None


async def check_graph_exists(session: AsyncSession, graph_name: str) -> bool:
    """检查指定名称的 AGE 图是否存在。"""
    result = await session.execute(
        text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = :name"),
        {"name": graph_name},
    )
    return result.first() is not None


async def init_database() -> None:
    """应用启动时的幂等检查：扩展、中文 FTS 配置、AGE 图。

    任一缺失抛 RuntimeError，指引运行 deploy/init/001_extensions.sql。
    """
    from mem_lake.db.session import AsyncSessionLocal

    settings = get_settings()
    async with AsyncSessionLocal() as session:
        exts = await check_extensions(session)
        missing = [k for k, v in exts.items() if not v]
        if missing:
            raise RuntimeError(
                f"缺少 PostgreSQL 扩展: {missing}。"
                "请确认 PostgreSQL 容器已启动且 deploy/init/001_extensions.sql 已执行。"
            )

        if not await check_chinese_fts(session):
            raise RuntimeError(
                "缺少中文全文检索配置 'chinese'。"
                "请确认 deploy/init/001_extensions.sql 已执行。"
            )

        if not await check_graph_exists(session, settings.AGE_GRAPH_NAME):
            raise RuntimeError(
                f"缺少 AGE 图 '{settings.AGE_GRAPH_NAME}'。"
                "请确认 deploy/init/001_extensions.sql 已执行。"
            )
