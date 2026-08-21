"""融合精排 _apply_rerank 单元测试（mock embedding client 与配置）。"""

import uuid

import pytest

from mem_lake.embedding.client import EmbeddingError
from mem_lake.search.fusion import SearchResult, _apply_rerank

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"


def _result(node_id: str, title: str, content: str) -> SearchResult:
    return SearchResult(
        node_id=uuid.UUID(node_id),
        title=title,
        content=content,
        node_type="Requirement",
        score=0.9,
        source="fused",
        properties={},
        tags=[],
    )


# 稳定构造三个结果：a / b / c
def _three_results() -> list[SearchResult]:
    return [_result(UUID_A, "A", "a"), _result(UUID_B, "B", "b"), _result(UUID_C, "C", "c")]


class _FakeSettings:
    """模拟配置：默认启用精排并指定模型路径。"""

    ENABLE_RERANK = True
    RERANK_MODEL_PATH = "/models/bge-reranker-base"
    RERANK_TOP_K = 30


class _FakeRerankClient:
    def __init__(self, scores: list[float] | None = None, has: bool = True, error: bool = False):
        self._scores = scores or []
        self._has = has
        self._error = error
        self.calls: list[tuple[str, list[str]]] = []

    async def has_rerank(self) -> bool:
        return self._has

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.calls.append((query, texts))
        if self._error:
            raise EmbeddingError("rerank 服务不可用")
        return self._scores


def _fake_settings(**overrides):
    class _S:
        ENABLE_RERANK = overrides.get("ENABLE_RERANK", _FakeSettings.ENABLE_RERANK)
        RERANK_MODEL_PATH = overrides.get(
            "RERANK_MODEL_PATH", _FakeSettings.RERANK_MODEL_PATH
        )
        RERANK_TOP_K = overrides.get("RERANK_TOP_K", _FakeSettings.RERANK_TOP_K)

    return _S


@pytest.fixture
def _patch_settings(monkeypatch):
    def _patch(**kwargs):
        monkeypatch.setattr(
            "mem_lake.search.fusion.get_settings", lambda: _fake_settings(**kwargs)
        )

    return _patch


async def test_rerank_disabled_when_no_model_path(_patch_settings):
    """RERANK_MODEL_PATH 为空时不精排，返回原序。"""
    _patch_settings(RERANK_MODEL_PATH="")
    client = _FakeRerankClient(scores=[1.0, 0.5, 0.0])
    results = _three_results()
    out = await _apply_rerank(results, client, top_n=10, query="q")
    assert [r.node_id for r in out] == [r.node_id for r in results]
    assert client.calls == []  # 未请求


async def test_rerank_disabled_when_flag_off(_patch_settings):
    """ENABLE_RERANK=False 时不精排。"""
    _patch_settings(ENABLE_RERANK=False)
    client = _FakeRerankClient(scores=[1.0, 0.5, 0.0])
    results = [_result(UUID_A, "A", "a"), _result(UUID_B, "B", "b")]
    out = await _apply_rerank(results, client, top_n=10, query="q")
    assert [r.node_id for r in out] == [r.node_id for r in results]
    assert client.calls == []


async def test_rerank_reorders_by_score(_patch_settings):
    """按精排分数降序重排候选。"""
    _patch_settings()
    # 原序 a(0.2) b(0.8) c(0.5) → 精排后 b > c > a
    client = _FakeRerankClient(scores=[0.2, 0.8, 0.5])
    results = _three_results()
    out = await _apply_rerank(results, client, top_n=10, query="q")
    assert [r.node_id for r in out] == [
        uuid.UUID(UUID_B), uuid.UUID(UUID_C), uuid.UUID(UUID_A)
    ]
    assert client.calls[0][0] == "q"  # 查询词透传


async def test_rerank_error_falls_back(_patch_settings):
    """rerank 异常时静默回退原 RRF 序。"""
    _patch_settings()
    client = _FakeRerankClient(has=True, error=True)
    results = [_result(UUID_A, "A", "a"), _result(UUID_B, "B", "b")]
    out = await _apply_rerank(results, client, top_n=10, query="q")
    assert [r.node_id for r in out] == [r.node_id for r in results]


async def test_rerank_not_loaded_falls_back(_patch_settings):
    """服务端未加载 rerank 模型时回退原 RRF 序。"""
    _patch_settings()
    client = _FakeRerankClient(has=False)
    results = [_result(UUID_A, "A", "a"), _result(UUID_B, "B", "b")]
    out = await _apply_rerank(results, client, top_n=10, query="q")
    assert [r.node_id for r in out] == [r.node_id for r in results]
    assert client.calls == []


async def test_rerank_respects_top_n(_patch_settings):
    """最终长度不超过 top_n。"""
    _patch_settings(RERANK_TOP_K=10)
    client = _FakeRerankClient(scores=[0.9, 0.1, 0.5])
    results = _three_results()
    out = await _apply_rerank(results, client, top_n=2, query="q")
    assert len(out) == 2
