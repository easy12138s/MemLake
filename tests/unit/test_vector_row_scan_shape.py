"""向量行扫语句结构护栏（无 DB 依赖，任何环境可跑）。

对齐 search/vector.py 的性能契约：HNSW 索引只加速「ORDER BY 距离 + LIMIT」
的纯行扫形态——group_by 聚合或 knowledge_node join 前置于行扫都会使索引
失效（planner 退化全表 Seq Scan，实测 ~0.8-1.1s）。集成层的 EXPLAIN 护栏
依赖存量数据（<500 行的新库会 skip），本用例用编译 SQL 的结构断言兜底：
「聚合前置回退」在任何环境可测。
"""

from sqlalchemy.dialects import postgresql

from mem_lake.search.vector import build_hits_statement


def test_hits_statement_is_pure_row_scan_shape():
    """编译 SQL 必须是纯行扫：无聚合、无 join、有序有 LIMIT。"""
    sql = str(
        build_hits_statement([0.1] * 1024, 200).compile(
            dialect=postgresql.dialect()
        )
    ).upper()
    assert "GROUP BY" not in sql, f"聚合前置会使 HNSW 索引失效:\n{sql}"
    assert "KNOWLEDGE_NODE" not in sql, f"join 前置会使 HNSW 索引失效:\n{sql}"
    assert "ORDER BY" in sql, "缺 ORDER BY 距离表达式，索引不会被选用"
    assert "LIMIT" in sql, "缺 LIMIT，索引有序扫截断失效"
    assert "<#>" in sql, "缺内积距离算子（max_inner_product 未正确编译）"
