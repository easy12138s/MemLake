"""EmbeddingClient 单元测试：聚焦指令感知参数的透传（无需真实 embedding 容器）。"""

import httpx
import pytest

from mem_lake.embedding.client import EmbeddingClient


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        self._captured["path"] = path
        self._captured["json"] = json
        return _FakeResponse({"embeddings": [[0.0] * 4], "dimension": 4})

    async def get(self, path: str) -> _FakeResponse:
        return _FakeResponse({"status": "ok", "model": "x", "dimension": 4})

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def _factory(*_args, **_kwargs) -> _FakeAsyncClient:
        return _FakeAsyncClient(captured)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    client = EmbeddingClient(base_url="http://fake", dimension=4)
    return client, captured


async def test_embed_forwards_prompt_name(fake_client):
    """embed 仅在传 prompt_name 时把它写进请求体。"""
    client, captured = fake_client
    await client.embed(["q"], prompt_name="query")
    assert captured["json"]["texts"] == ["q"]
    assert captured["json"]["prompt_name"] == "query"
    assert "prompt" not in captured["json"]


async def test_embed_one_forwards_prompt_name(fake_client):
    """embed_one 透传 prompt_name 并取首元素。"""
    client, captured = fake_client
    vec = await client.embed_one("q", prompt_name="query")
    assert vec == [0.0] * 4
    assert captured["json"]["prompt_name"] == "query"


async def test_embed_default_no_instruction(fake_client):
    """不传指令时请求体不含 prompt/prompt_name（与历史文档编码兼容）。"""
    client, captured = fake_client
    await client.embed(["a", "b"])
    assert "prompt" not in captured["json"]
    assert "prompt_name" not in captured["json"]


# ============ rerank / has_rerank ============


class _PathAwareFakeAsyncClient:
    """按路径返回不同响应，用于 rerank / has_rerank 测试。"""

    def __init__(self, captured: dict, rerank_payload: dict, health_payload: dict) -> None:
        self._captured = captured
        self._rerank_payload = rerank_payload
        self._health_payload = health_payload

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        self._captured["path"] = path
        self._captured["json"] = json
        return _FakeResponse(self._rerank_payload)

    async def get(self, path: str) -> _FakeResponse:
        return _FakeResponse(self._health_payload)

    async def aclose(self) -> None:
        pass


def _make_rerank_client(monkeypatch: pytest.MonkeyPatch, rerank_payload: dict, health_payload: dict):
    captured: dict = {}

    def _factory(*_args, **_kwargs) -> _PathAwareFakeAsyncClient:
        return _PathAwareFakeAsyncClient(captured, rerank_payload, health_payload)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    client = EmbeddingClient(base_url="http://fake", dimension=4)
    return client, captured


async def test_rerank_restores_original_order(monkeypatch):
    """rerank 按 order 还原为与原 texts 同序的分数。"""
    # order=[1,0,2] 表示分数降序为：原 index1(0.9)> index0(0.5)> index2(0.2)
    payload = {"scores": [0.9, 0.5, 0.2], "order": [1, 0, 2]}
    health = {"status": "ok", "has_rerank": True}
    client, captured = _make_rerank_client(monkeypatch, payload, health)

    scores = await client.rerank("q", ["a", "b", "c"])
    assert captured["json"] == {"query": "q", "texts": ["a", "b", "c"]}
    assert captured["path"] == "/rerank"
    # 还原到原序后：index0=0.5, index1=0.9, index2=0.2
    assert scores == [0.5, 0.9, 0.2]


async def test_rerank_empty_texts(monkeypatch):
    """空 texts 直接返回空列表，不请求服务。"""
    payload = {"scores": [], "order": []}
    health = {"status": "ok", "has_rerank": True}
    client, captured = _make_rerank_client(monkeypatch, payload, health)
    assert await client.rerank("q", []) == []
    assert "path" not in captured  # 未发起请求


async def test_has_rerank_true(monkeypatch):
    """health 返回 has_rerank=true 时 has_rerank() 为 True。"""
    health = {"status": "ok", "has_rerank": True}
    client, _ = _make_rerank_client(monkeypatch, {}, health)
    assert await client.has_rerank() is True


async def test_has_rerank_false_when_not_loaded(monkeypatch):
    """health 返回 has_rerank=false 时 has_rerank() 为 False。"""
    health = {"status": "ok", "has_rerank": False}
    client, _ = _make_rerank_client(monkeypatch, {}, health)
    assert await client.has_rerank() is False
