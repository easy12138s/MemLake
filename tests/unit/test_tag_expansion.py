"""标签语义扩展单元测试（纯函数 + 编排层，无需 DB / embedding 服务）。"""

import uuid
from unittest.mock import AsyncMock

import pytest

from mem_lake.search import tag_expansion as te

# 2D 单位向量，便于手工控制余弦相似度
VEC = {
    "性能": [1.0, 0.0],
    "N+1": [0.95, 0.31],       # cos ≈ 0.95
    "慢查询": [0.87, 0.5],     # cos ≈ 0.87
    "登录": [0.0, 1.0],        # cos = 0
}


def make_embed_fn(vecs: dict[str, list[float]]):
    def fn(texts: list[str]) -> list[list[float]]:
        return [vecs[t] for t in texts]

    return fn


def test_expand_no_query_tags_returns_empty():
    assert te.expand_query_tags([], ["a", "b"], make_embed_fn(VEC)) == []


def test_expand_no_vocab_returns_query_tags():
    out = te.expand_query_tags(["性能"], [], make_embed_fn(VEC))
    assert out == ["性能"]


def test_expand_picks_semantically_close_tags():
    out = te.expand_query_tags(
        ["性能"], ["N+1", "慢查询", "登录"], make_embed_fn(VEC), threshold=0.7
    )
    assert set(out) == {"性能", "N+1", "慢查询"}
    assert "登录" not in out


def test_expand_threshold_boundary():
    # 「慢查询」与「性能」cos ≈ 0.87；阈值 0.9 应排除，0.8 应包含
    out_high = te.expand_query_tags(["性能"], ["慢查询"], make_embed_fn(VEC), threshold=0.9)
    assert "慢查询" not in out_high
    out_low = te.expand_query_tags(["性能"], ["慢查询"], make_embed_fn(VEC), threshold=0.8)
    assert "慢查询" in out_low


def test_expand_dedup_and_keeps_original_order():
    out = te.expand_query_tags(
        ["性能", "登录"], ["N+1", "慢查询"], make_embed_fn(VEC), threshold=0.7
    )
    assert out[0] == "性能"
    assert out.count("性能") == 1
    assert set(out) == {"性能", "登录", "N+1", "慢查询"}


def test_expand_embed_fn_wrong_length_raises():
    def bad_fn(texts):
        return [[0.0, 1.0]]  # 长度不符

    with pytest.raises(ValueError):
        te.expand_query_tags(["性能"], ["N+1"], bad_fn)


async def test_expand_tags_for_project_orchestration():
    vocab = ["N+1", "慢查询", "登录"]
    client = AsyncMock()
    client.embed = AsyncMock(side_effect=lambda texts: [VEC[t] for t in texts])

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            te,
            "fetch_project_tag_vocab",
            AsyncMock(return_value=vocab),
        )
        out = await te.expand_tags_for_project(
            client, object(), project_id=uuid.uuid4(), tags=["性能"], threshold=0.7
        )

    assert set(out) == {"性能", "N+1", "慢查询"}
    client.embed.assert_awaited_once()


async def test_expand_tags_for_project_empty_vocab_returns_tags():
    client = AsyncMock()
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(te, "fetch_project_tag_vocab", AsyncMock(return_value=[]))
        out = await te.expand_tags_for_project(
            client, object(), project_id=uuid.uuid4(), tags=["性能"]
        )
    assert out == ["性能"]
    client.embed.assert_not_awaited()
