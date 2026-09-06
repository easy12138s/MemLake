"""Alembic 迁移环境：挂接 Mem Lake 项目配置。

技术决策：
- 同步引擎执行迁移（URL 由 settings.DATABASE_URL 的 psycopg_async 改写为 psycopg 同步驱动）：
  * psycopg[binary] 已含同步驱动，无新增依赖
  * 迁移脚本（op.create_*）为同步 API，同步执行最直接
  * 规避 Windows 下 psycopg3 async 与 ProactorEventLoop 不兼容问题
    （与 tests/conftest.py 的 SelectorEventLoop 处理同因）
- target_metadata 指向 Base.metadata：与 db/init.py create_tables 同一注册方式
  （import 各模块 models 触发 ORM 注册）
- 迁移不负责扩展/图/FTS 配置：由 deploy/init/001_extensions.sql 在容器启动时创建，
  迁移仅管理业务表与索引（对齐 db/init.py 职责边界）
"""

import re
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config 对象，提供 .ini 文件内的配置值
config = context.config

# 解释配置文件用于 Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 复用项目连接配置：psycopg_async → psycopg（迁移同步执行）
from mem_lake.config import get_settings  # noqa: E402

_settings = get_settings()
_async_url = _settings.DATABASE_URL
if not re.match(r"^postgresql\+psycopg(_async)?://", _async_url):
    raise RuntimeError(
        f"DATABASE_URL 必须为 postgresql+psycopg(_async) 驱动，当前: {_async_url}"
    )
config.set_main_option("sqlalchemy.url", _async_url.replace("+psycopg_async", "+psycopg"))

# 触发各模块 ORM 注册到 Base.metadata（import 即注册，与 db/init.py create_tables 同法）
import mem_lake.approval.models  # noqa: E402,F401
import mem_lake.audit.models  # noqa: E402,F401
import mem_lake.auth.models  # noqa: E402,F401
import mem_lake.db.base  # noqa: E402,F401
import mem_lake.gateway.models  # noqa: E402,F401
import mem_lake.knowledge.models  # noqa: E402,F401

target_metadata = mem_lake.db.base.Base.metadata


def run_migrations_offline() -> None:
    """离线模式：仅输出 SQL，不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：同步引擎执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
