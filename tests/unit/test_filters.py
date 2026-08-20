"""search/filters FilterSpec 单元测试：tags_op AND/OR 语义。

纯单测，无 DB 依赖。验证 tags_op 编译为不同 SQL 操作符（@> vs &&）。
"""

import pytest
from sqlalchemy.dialects import postgresql

from mem_lake.search.filters import FilterSpec, compile_sqlalchemy


def _tags_sql(tags_op: str) -> str:
    spec = FilterSpec(project_id=None, tags=("a", "b"), tags_op=tags_op)
    clauses = compile_sqlalchemy(spec)
    return " ".join(
        str(c.compile(dialect=postgresql.dialect())) for c in clauses
    )


def test_tags_op_all_compiles_contains():
    """tags_op=all 编译为 JSONB @>（AND，包含全部标签）。"""
    sql = _tags_sql("all")
    assert "@>" in sql


def test_tags_op_any_compiles_has_any_key():
    """tags_op=any 编译为 JSONB ?|（has_any_key，OR，命中任一标签）。"""
    sql = _tags_sql("any")
    assert "?|" in sql


def test_tags_op_invalid_raises():
    """非法 tags_op 在构造时抛 ValueError。"""
    with pytest.raises(ValueError):
        FilterSpec(project_id=None, tags=("a",), tags_op="xor")
