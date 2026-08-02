"""连接池管理、会话工厂（SQLAlchemy async engine）。

基于 psycopg3 async 驱动，URL scheme 必须为 postgresql+psycopg_async。
连接池参数从 config.Settings 读取。get_session 作为通用 async generator，
供 gateway 层（M6）以 FastAPI Depends 方式注入，或供应用代码直接 async with 使用。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mem_lake.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.DATABASE_URL,
    pool_size=_settings.DATABASE_POOL_SIZE,
    max_overflow=_settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """yield 一个 AsyncSession，上下文退出时自动关闭。

    expire_on_commit=False 避免 commit 后访问属性触发隐式 IO（async 不支持）。
    """
    async with AsyncSessionLocal() as session:
        yield session
