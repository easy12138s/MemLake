"""冲突阈值标定脚本的纯函数单测（analyze_distribution / cosine / parse_vector）。"""

import math

from scripts.calibrate_conflict_threshold import (
    _is_duplicate_pair,
    analyze_distribution,
    cosine,
    parse_vector,
)


def _unit_vector(seed: int, dim: int = 1024) -> list[float]:
    v = [math.sin(seed + i) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def test_parse_vector():
    assert parse_vector("[0.1, 0.2, 0.3]") == [0.1, 0.2, 0.3]
    assert parse_vector("") == []
    assert parse_vector(None) == []
    assert parse_vector("not-a-vector") == []


def test_cosine_same_and_orthogonal():
    a = [1.0, 0.0]
    assert math.isclose(cosine(a, a), 1.0, abs_tol=1e-9)
    assert math.isclose(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-9)
    assert math.isclose(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0, abs_tol=1e-9)


def test_is_duplicate_pair_by_key_field():
    """Solution 单关键标识字段 approach：相同 → 重复；不同 → 不重复。"""
    a_props = {"approach": "JWT 鉴权", "version": "1.0"}
    b_props = {"approach": "JWT 鉴权", "version": "2.0"}  # 关键标识相同
    c_props = {"approach": "Session 鉴权"}  # 关键标识不同
    assert _is_duplicate_pair("Solution", a_props, "A", "Solution", b_props, "B", 40)
    assert not _is_duplicate_pair("Solution", a_props, "A", "Solution", c_props, "B", 40)


def test_is_duplicate_pair_requirement_no_key_fields():
    """Requirement 无关键标识字段（主键由服务端分配）→ 属性不同不构成判重依据。"""
    a_props = {"priority": "P0", "module": "auth"}
    b_props = {"priority": "P1", "module": "auth"}
    assert not _is_duplicate_pair("Requirement", a_props, "标题A", "Requirement", b_props, "标题B", 40)
    # 标题归一相同仍视为疑似重复（与检测器同口径）
    assert _is_duplicate_pair("Requirement", a_props, "同一标题", "Requirement", b_props, "同一标题", 40)


def test_is_duplicate_pair_requires_all_key_fields():
    """与检测器 L2 同口径：多关键标识字段的类型须全部相同才算同一实体。

    仅 file_path 相同、name 不同的两个 CodeSnippet 不是重复（历史上按
    "任一字段相同"误判，导致标定 dup 组被污染、建议区间失真）。
    """
    a_props = {"name": "mod_a", "file_path": "src/svc.py"}
    b_same = {"name": "mod_a", "file_path": "src/svc.py"}
    b_partial = {"name": "mod_b", "file_path": "src/svc.py"}  # 仅 file_path 相同
    b_missing = {"name": "mod_a"}  # 缺 file_path
    assert _is_duplicate_pair("CodeSnippet", a_props, "A", "CodeSnippet", b_same, "B", 40)
    assert not _is_duplicate_pair("CodeSnippet", a_props, "A", "CodeSnippet", b_partial, "B", 40)
    assert not _is_duplicate_pair("CodeSnippet", a_props, "A", "CodeSnippet", b_missing, "B", 40)


def test_is_duplicate_pair_by_title():
    assert _is_duplicate_pair("Pitfall", {}, "同一标题", "Pitfall", {}, "同一标题", 40)
    assert not _is_duplicate_pair("Pitfall", {}, "标题一", "Pitfall", {}, "标题二", 40)


def test_is_duplicate_pair_diff_type():
    assert not _is_duplicate_pair("Requirement", {"priority": "P0"}, "T",
                                  "CodeSnippet", {"name": "x"}, "T", 40)


def test_analyze_distribution_separates_dups():
    """疑似重复对余弦应稳定高、一般不重复对应低一些，且能给出建议区间。"""
    # 构造一组成对近重复（同标题 + 同向向量）与一组一般不同的节点
    nodes = []
    # 5 对近重复：标题归一相同，向量几乎相同
    for pid in range(5):
        vec_a = _unit_vector(pid)
        vec_b = [x * 0.999 for x in vec_a]  # 近重复
        nodes.append({"type": "Requirement", "title": f"需求{pid}",
                      "properties": {"priority": "P0", "module": "auth"}, "vector": vec_a})
        nodes.append({"type": "Requirement", "title": f"需求{pid}",
                      "properties": {"priority": "P1", "module": "auth"}, "vector": vec_b})
    # 混入若干一般独立节点
    for i in range(5):
        nodes.append({"type": "Requirement", "title": f"独立{i}",
                      "properties": {"priority": "P2", "module": "report"}, "vector": _unit_vector(i + 100)})

    result = analyze_distribution(nodes, pairs=200_000, seed=1)
    assert result["mode"] == "doc-doc"
    assert result["dup_samples"] > 0
    assert result["nondup_samples"] > 0
    # 疑似重复组的 p50 应明显高于一般不重复组的 p50
    dup_p50 = result["dup_distribution"]["p50"]
    nondup_p50 = result["nondup_distribution"]["p50"]
    assert dup_p50 > nondup_p50
    assert result["suggested_threshold"] is not None
    # Top 对明细：最高分应为近重复对且非空
    assert result["top_pairs"]
    assert result["top_pairs"][0]["score"] >= result["top_pairs"][-1]["score"]


def test_analyze_distribution_cross_type_pairs_excluded():
    """跨类型对不参与统计（运行时 L1 按 node_type 过滤）。"""
    nodes = [
        {"type": "Requirement", "title": "A", "properties": {"priority": "P0"},
         "vector": _unit_vector(1)},
        {"type": "CodeSnippet", "title": "A", "properties": {},
         "vector": _unit_vector(1)},  # 与节点 0 向量相同但类型不同
        {"type": "Requirement", "title": "B", "properties": {"priority": "P1"},
         "vector": _unit_vector(2)},
    ]
    result = analyze_distribution(nodes, pairs=100, seed=1)
    # 仅 Requirement-Requirement 一对参与统计
    assert result["sampled_pairs"] == 1


def test_analyze_distribution_query_mode():
    """query-doc 模式：相似度取双向最大，query 向量与 nodes 对齐、None 跳过。"""
    nodes = []
    qvecs = []
    # 5 对近重复：同标题，双方 doc 向量与 query 向量均近似同向
    # （模拟"i 的 query 编码能召回 j 的落库向量"的运行时条件）
    for pid in range(5):
        base = _unit_vector(pid)
        nodes.append({"type": "Requirement", "title": f"需求{pid}",
                      "properties": {"priority": "P0", "module": "auth"}, "vector": base})
        qvecs.append([x * 0.98 for x in base])
        nodes.append({"type": "Requirement", "title": f"需求{pid}",
                      "properties": {"priority": "P1", "module": "auth"}, "vector": [x * 0.999 for x in base]})
        qvecs.append([x * 0.98 * 0.999 for x in base])
    # 独立节点 + 一个无 query 向量的节点（应被跳过）
    for i in range(5):
        vec = _unit_vector(i + 100)
        nodes.append({"type": "Requirement", "title": f"独立{i}",
                      "properties": {"priority": "P2", "module": "report"}, "vector": vec})
        qvecs.append(vec)
    nodes.append({"type": "Requirement", "title": "无查询向量",
                  "properties": {"priority": "P3", "module": "misc"}, "vector": _unit_vector(999)})
    qvecs.append(None)

    result = analyze_distribution(nodes, pairs=200_000, seed=1, query_vectors=qvecs)
    assert result["mode"] == "query-doc"
    assert result["total_nodes"] == 15  # 跳过无 query 向量节点
    assert result["dup_samples"] > 0
    # 近重复对（同标题）的 query-doc 分数应显著高于一般对
    assert result["dup_distribution"]["p50"] > result["nondup_distribution"]["p50"]
    assert result["suggested_threshold"] is not None


def test_analyze_distribution_query_mode_length_mismatch():
    nodes = [{"type": "Pitfall", "title": "A", "properties": {}, "vector": [1.0]}]
    try:
        analyze_distribution(nodes, query_vectors=[[1.0], [1.0]])
        raise AssertionError("应抛出 ValueError")
    except ValueError:
        pass
