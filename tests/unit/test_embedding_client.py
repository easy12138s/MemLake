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
