"""fusion 单元测试：fused 分数透出向量余弦（P4）。"""

import uuid

from mem_lake.search.fusion import SearchResult, _apply_vector_scores, rrf_fuse


def _make(node_id, score, source="vector"):
    return SearchResult(
        node_id=node_id,
        title="t",
        content="c",
        node_type="CodeSnippet",
        score=score,
        source=source,
        properties={},
        tags=[],
    )


def test_apply_vector_scores_overrides_fused_with_cosine():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vector_results = [_make(a, 0.83), _make(b, 0.55)]
    # fused 初始为 RRF 小数（模拟 rrf_fuse 输出）
    fused = [_make(a, 0.016, "fused"), _make(b, 0.0159, "fused"), _make(c, 0.0158, "fused")]

    out = _apply_vector_scores(fused, vector_results)

    by_id = {r.node_id: r for r in out}
    assert by_id[a].score == 0.83  # 透出余弦，非 RRF 小数
    assert by_id[b].score == 0.55
    # 无向量分的节点保留 RRF 分
    assert by_id[c].score == 0.0158
    # 顺序保持 RRF 排序（a 在前）
    assert out[0].node_id == a


def test_apply_vector_scores_empty_vector_map_returns_unchanged():
    a = uuid.uuid4()
    fused = [_make(a, 0.016, "fused")]
    out = _apply_vector_scores(fused, [])
    assert out[0].score == 0.016


def test_rrf_fuse_still_assigns_rrf_scores():
    # rrf_fuse 本身行为不变（分数仍为 RRF 小数），覆盖逻辑在 hybrid_search 中
    a, b = uuid.uuid4(), uuid.uuid4()
    res = rrf_fuse(
        [[_make(a, 0.9), _make(b, 0.8)]],
        k=60,
        top_n=10,
    )
    assert res[0].score == 1 / (60 + 1)
    assert res[0].source == "fused"
