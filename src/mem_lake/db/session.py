"""连接池管理、会话工厂（SQLAlchemy async engine）。

基于 psycopg3 async 驱动，URL scheme 必须为 postgresql+psycopg_async。
连接池参数从 config.Settings 读取。AsyncSessionLocal 为全局会话工厂，
由 gateway 层（dependencies.py）的 transactional_session / get_readonly_session
使用。expire_on_commit=False 避免 commit 后访问属性触发隐式 IO（async 不支持）。
"""

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
