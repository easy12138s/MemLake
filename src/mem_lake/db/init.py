"""扩展安装与初始化：CREATE EXTENSION age/pgvector/zhparser、AGE 图创建、业务表建表、
tsvector 触发器、RLS 策略。

职责边界：
- init_database()：幂等存在性检查（fail-fast），不重复 CREATE EXTENSION。
  扩展与图的实体创建由 deploy/init/001_extensions.sql 在容器启动时完成，
  应用启动时调用验证它们就位，缺失则抛 RuntimeError 指引修复。
- create_tables()：用 Base.metadata.create_all 幂等建业务表。
  通过 import 各模块 models 触发 ORM 注册到 Base.metadata；
  M2 注册 audit_log + access_key，M3 注册 knowledge_node。
- init_knowledge_schema()：在 knowledge_node 表存在后，创建 tsvector 触发器与 RLS 策略。
  必须在 create_tables 之后调用。幂等设计：DROP IF EXISTS + CREATE。

技术决策（网络搜索 PostgreSQL 17 官方文档）：
- tsvector 自动维护用内置触发器函数 tsvector_update_trigger(column, config, text_cols)
- RLS 策略基于 current_setting('app.current_project_id', true) 实现项目隔离
- 不 FORCE RLS（owner 可绕过），生产环境通过非 owner 用户连接受 RLS 约束
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.config import get_settings

REQUIRED_EXTENSIONS = ("age", "vector", "zhparser")


async def create_tables(session: AsyncSession) -> None:
    """幂等创建所有已注册的业务表。

    通过 import 各模块 models 触发 ORM 注册到 Base.metadata，再调用 create_all。
    create_all 对已存在的表跳过，幂等安全。
    """
    from mem_lake.db.base import Base
    # 触发各模块 ORM 注册到 Base.metadata（import 即注册）
    from mem_lake.audit import models as _audit_models  # noqa: F401
    from mem_lake.auth import models as _auth_models  # noqa: F401
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
    """在 knowledge_node 表存在后，幂等创建 tsvector 触发器与 RLS 策略。

    必须在 create_tables 之后调用。

    创建内容：
    1. tsvector 触发器 trg_knowledge_node_tsv：
       BEFORE INSERT OR UPDATE 时用内置 tsvector_update_trigger 维护 content_tsv
       基于 'chinese' 配置（zhparser 分词）从 title + content 拼接生成
    2. RLS 策略 knowledge_project_isolation：
       USING (project_id::text = current_setting('app.current_project_id', true))
       ENABLE ROW LEVEL SECURITY（不 FORCE，owner 可绕过，生产用非 owner 用户）

    幂等：DROP TRIGGER IF EXISTS + CREATE TRIGGER；DROP POLICY IF EXISTS + CREATE POLICY。

    注：PostgreSQL 17 的 CREATE POLICY 语法不支持 IF NOT EXISTS 子句（与 CREATE TRIGGER 不同），
    官方语法见 https://www.postgresql.org/docs/17/sql-createpolicy.html。
    因此采用 DROP POLICY IF EXISTS + CREATE POLICY 的幂等模式。
    """
    # 1. tsvector 触发器（PG 内置函数，自动维护 content_tsv）
    #    使用 to_tsvector('chinese', title || ' ' || content) 自定义函数触发器，
    #    因为内置 tsvector_update_trigger 不支持多列拼接表达式，只能逐列累加。
    #    实际上内置触发器支持多列：tsvector_update_trigger(tsv, 'cfg', col1, col2)
    #    会按权重 D 自动累加，符合我们的需求。
    await session.execute(text("DROP TRIGGER IF EXISTS trg_knowledge_node_tsv ON knowledge_node"))
    await session.execute(
        text(
            "CREATE TRIGGER trg_knowledge_node_tsv "
            "BEFORE INSERT OR UPDATE ON knowledge_node "
            "FOR EACH ROW EXECUTE FUNCTION "
            "tsvector_update_trigger(content_tsv, 'public.chinese', title, content)"
        )
    )

    # 2. RLS 策略：项目级隔离
    #    current_setting(name, true) 第二参数 true 表示缺失时返回 NULL 而非报错
    #    NULL = project_id 永远为 NULL（NULL 语义），未注入上下文时返回 0 行
    #    幂等：DROP POLICY IF EXISTS + CREATE POLICY（CREATE POLICY 不支持 IF NOT EXISTS）
    await session.execute(text("ALTER TABLE knowledge_node ENABLE ROW LEVEL SECURITY"))
    await session.execute(
        text("DROP POLICY IF EXISTS knowledge_project_isolation ON knowledge_node")
    )
    await session.execute(
        text(
            "CREATE POLICY knowledge_project_isolation ON knowledge_node "
            "USING (project_id::text = current_setting('app.current_project_id', true))"
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
