"""扩展安装与初始化：CREATE EXTENSION age/pgvector/zhparser、AGE 图创建、业务表建表、
tsvector 触发器。

职责边界：
- init_database()：幂等存在性检查（fail-fast），不重复 CREATE EXTENSION。
  扩展与图的实体创建由 deploy/init/001_extensions.sql 在容器启动时完成，
  应用启动时调用验证它们就位，缺失则抛 RuntimeError 指引修复。
- create_tables()：用 Base.metadata.create_all 幂等建业务表（v1.0.0 全新安装-only，
  schema 由 create_all 按 models.py 全量生成）。
  通过 import 各模块 models 触发 ORM 注册到 Base.metadata。
- init_knowledge_schema()：在 knowledge_node 表存在后，创建 tsvector 触发器。
  必须在 create_tables 之后调用。幂等设计：DROP IF EXISTS + CREATE。

技术决策：
- tsvector 自动维护用内置触发器函数 tsvector_update_trigger(column, config, text_cols)
- 项目隔离由应用层 validate_project_access + FilterSpec 实现（部署连接用户是表 owner，
  RLS 不 FORCE 时天然绕过，故不创建 RLS 策略）
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.config import get_settings

logger = logging.getLogger("mem_lake.db.init")

REQUIRED_EXTENSIONS = ("age", "vector", "zhparser")


async def create_tables(session: AsyncSession) -> None:
    """幂等创建所有已注册的业务表（v1.0.0 全新安装-only，schema 由 create_all 全量生成）。

    通过 import 各模块 models 触发 ORM 注册到 Base.metadata，再调用 create_all。
    create_all 对已存在的表跳过，幂等安全。
    """
    # 触发各模块 ORM 注册到 Base.metadata（import 即注册）
    from mem_lake.approval import models as _approval_models  # noqa: F401
    from mem_lake.audit import models as _audit_models  # noqa: F401
    from mem_lake.auth import models as _auth_models  # noqa: F401
    from mem_lake.db.base import Base
    from mem_lake.gateway import models as _gateway_models  # noqa: F401
    from mem_lake.knowledge import models as _knowledge_models  # noqa: F401

    conn = await session.connection()
    await conn.run_sync(Base.metadata.create_all)


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


async def init_knowledge_schema(session: AsyncSession) -> None:
    """在 knowledge_node 表存在后，幂等创建 tsvector 触发器。

    必须在 create_tables 之后调用。

    创建内容：
    - tsvector 触发器 trg_knowledge_node_tsv：
      BEFORE INSERT OR UPDATE 时用内置 tsvector_update_trigger 维护 content_tsv
      基于 'chinese' 配置（zhparser 分词）从 title + content 拼接生成

    幂等：DROP TRIGGER IF EXISTS + CREATE TRIGGER。

    注：项目隔离由应用层 validate_project_access + FilterSpec（project_id 过滤）
    实现，不创建 RLS 策略（部署连接用户是表 owner，RLS 不 FORCE 时天然绕过）。
    """
    # tsvector 触发器（PG 内置函数，自动维护 content_tsv）。
    # 内置 tsvector_update_trigger 支持多列：tsvector_update_trigger(tsv, 'cfg', col1, col2)
    # 会按权重 D 自动累加，符合需求。
    await session.execute(text("DROP TRIGGER IF EXISTS trg_knowledge_node_tsv ON knowledge_node"))
    await session.execute(
        text(
            "CREATE TRIGGER trg_knowledge_node_tsv "
            "BEFORE INSERT OR UPDATE ON knowledge_node "
            "FOR EACH ROW EXECUTE FUNCTION "
            "tsvector_update_trigger(content_tsv, 'public.chinese', title, content)"
        )
    )


async def init_database() -> None:
    """应用启动时的幂等检查：扩展、中文 FTS 配置、AGE 图。

    任一缺失抛 RuntimeError，指引运行 deploy/init/001_extensions.sql。

    注意：本函数不创建业务表与触发器/RLS，由应用启动流程在 init_database 后
    调用 create_tables + init_knowledge_schema 完成。
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


async def check_migrations_synced(session: AsyncSession) -> None:
    """启动时校验 Alembic 迁移版本与脚本目录一致（FIX-01 迁移机制）。

    比对数：
    - 数据库 alembic_version 表登记的当前版本
    - 脚本目录 alembic/versions 的 head revision

    不一致（含缺 alembic_version 表的全新/存量库）→ 抛 RuntimeError，
    指明处置命令（upgrade head / stamp head），避免 schema 静默漂移。

    过渡约定：v1.0.x 内 create_tables 的幂等 ALTER 与 Alembic revision 并存
    且保持一致；本校验确保两者之一缺失时启动即暴露。
    """
    from alembic.config import Config
    from sqlalchemy import inspect

    conn = await session.connection()
    tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    if "alembic_version" not in tables:
        raise RuntimeError(
            "数据库未登记 Alembic 迁移版本（缺 alembic_version 表）。"
            "全新安装请执行：alembic upgrade head；存量库请执行：alembic stamp head。"
            "详细见 docs/架构优化修复方案.md FIX-01。"
        )

    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    db_current = result.scalar()

    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    from alembic.script import ScriptDirectory

    script_head = ScriptDirectory.from_config(cfg).get_current_head()

    if db_current != script_head:
        action = (
            "alembic upgrade head"
            if db_current != script_head
            else "alembic stamp head"
        )
        raise RuntimeError(
            f"数据库 Alembic 版本（{db_current!r}）与脚本目录 head（{script_head!r}）不一致。"
            f"处置命令：{action}（在项目根目录、使用 conda memlake 环境执行）。"
        )
