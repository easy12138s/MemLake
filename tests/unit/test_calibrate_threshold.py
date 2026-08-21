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
    a_props = {"requirement_id": "REQ-001", "priority": "P1"}
    b_props = {"requirement_id": "REQ-001", "priority": "P2"}  # 关键标识相同
    c_props = {"requirement_id": "REQ-002"}  # 关键标识不同
    assert _is_duplicate_pair("Requirement", a_props, "A", "Requirement", b_props, "B", 40)
    assert not _is_duplicate_pair("Requirement", a_props, "A", "Requirement", c_props, "B", 40)


def test_is_duplicate_pair_diff_type():
    assert not _is_duplicate_pair("Requirement", {"requirement_id": "R1"}, "T",
                                  "CodeSnippet", {"name": "x"}, "T", 40)


def test_analyze_distribution_separates_dups():
    """疑似重复对余弦应稳定高、一般不重复对应低一些，且能给出建议区间。"""
    # 构造一组成对近重复（同 key 字段 + 同向向量）与一组一般不同的节点
    nodes = []
    # 5 对近重复：requirement_id 相同，向量几乎相同
    for pid in range(5):
        vec_a = _unit_vector(pid)
        vec_b = [x * 0.999 for x in vec_a]  # 近重复
        nodes.append({"type": "Requirement", "title": f"需求{pid}",
                      "properties": {"requirement_id": f"REQ-{pid}"}, "vector": vec_a})
        nodes.append({"type": "Requirement", "title": f"需求{pid}副本",
                      "properties": {"requirement_id": f"REQ-{pid}"}, "vector": vec_b})
    # 混入若干一般独立节点
    for i in range(5):
        nodes.append({"type": "Requirement", "title": f"独立{i}",
                      "properties": {"requirement_id": f"IND-{i}"}, "vector": _unit_vector(i + 100)})

    result = analyze_distribution(nodes, pairs=200_000, seed=1)
    assert result["dup_samples"] > 0
    assert result["nondup_samples"] > 0
    # 疑似重复组的 p50 应明显高于一般不重复组的 p50
    dup_p50 = result["dup_distribution"]["p50"]
    nondup_p50 = result["nondup_distribution"]["p50"]
    assert dup_p50 > nondup_p50
    assert result["suggested_threshold"] is not None
