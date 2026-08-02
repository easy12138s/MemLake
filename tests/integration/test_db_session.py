"""M1 集成测试：验证数据库基础设施。

验证项：
1. async engine 连接与连接池
2. 三个扩展（age/vector/zhparser）已安装
3. 中文全文检索配置 'chinese' 存在
4. AGE 图 'mem_lake_graph' 存在
5. Base 可被 ORM 模型继承

所有测试连真实 PostgreSQL（localhost:5432），通过 conftest.db_session fixture 访问。
"""

from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column

from mem_lake.db.base import Base


async def test_async_engine_connect(db_session):
    """验证 async engine 可执行基本查询。"""
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


async def test_extensions_installed(db_session):
    """验证 age/vector/zhparser 三个扩展已安装。"""
    result = await db_session.execute(
        text(
            "SELECT extname FROM pg_extension "
            "WHERE extname = ANY(ARRAY['age', 'vector', 'zhparser'])"
        )
    )
    names = {row[0] for row in result}
    assert {"age", "vector", "zhparser"}.issubset(names)


async def test_chinese_fts_config(db_session):
    """验证中文全文检索配置 'chinese' 存在。"""
    result = await db_session.execute(
        text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'")
    )
    assert result.first() is not None


async def test_age_graph_exists(db_session):
    """验证 AGE 图 'mem_lake_graph' 存在。"""
    result = await db_session.execute(
        text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'mem_lake_graph'")
    )
    assert result.first() is not None


def test_base_declarative_subclassable():
    """验证 Base 可被 ORM 模型继承（不实际建表）。"""

    class _TmpModel(Base):
        __tablename__ = "test_m1_tmp"
        id: Mapped[int] = mapped_column(primary_key=True)

    assert _TmpModel.__tablename__ == "test_m1_tmp"
    assert "id" in _TmpModel.__dict__
