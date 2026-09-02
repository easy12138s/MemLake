"""EmbeddingClient 单元测试：聚焦指令感知参数的透传（无需真实 embedding 容器）。"""

import httpx
import pytest

from mem_lake.embedding import client as embedding_client_module
from mem_lake.embedding.client import EmbeddingClient, EmbeddingError


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
        return _FakeResponse(
            {"embeddings": [[0.0] * 4 for _ in (json or {}).get("texts", [])], "dimension": 4}
        )

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


# ============ 批量分块与重试 ============


class _StatusResponse:
    """非 200 响应（分块/重试测试用）。"""

    def __init__(self, status: int, body: str = "") -> None:
        self.status_code = status
        self.text = body

    def json(self) -> dict:
        return {}


class _ScriptedAsyncClient:
    """按脚本序列推进的假客户端：默认返回与 texts 同序同数的 4 维向量。

    script 元素：Exception（抛错）/ (status, body) 元组（非 200）/ "ok"（成功）。
    """

    def __init__(self, script: list | None = None) -> None:
        self._script = list(script or [])
        self.requests: list[dict] = []

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse | _StatusResponse:
        self.requests.append({"path": path, "json": json})
        texts = (json or {}).get("texts", [])
        behavior = self._script.pop(0) if self._script else "ok"
        if isinstance(behavior, Exception):
            raise behavior
        if isinstance(behavior, tuple):
            status, body = behavior
            return _StatusResponse(status, body)
        # 向量编码文本内容（字符码之和），便于跨块验证顺序保持
        return _FakeResponse(
            {
                "embeddings": [[float(sum(ord(c) for c in t))] * 4 for t in texts],
                "dimension": 4,
            }
        )

    async def get(self, path: str) -> _FakeResponse:
        return _FakeResponse({"status": "ok", "model": "x", "dimension": 4})

    async def aclose(self) -> None:
        pass


def _make_scripted_client(monkeypatch: pytest.MonkeyPatch, script: list | None = None):
    fake = _ScriptedAsyncClient(script)

    def _factory(*_args, **_kwargs) -> _ScriptedAsyncClient:
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    client = EmbeddingClient(base_url="http://fake", dimension=4)
    return client, fake


async def test_embed_chunks_over_limit_and_preserves_order(monkeypatch):
    """超过 128 条自动分块（128 + 剩余），结果与原输入顺序一致。"""
    client, fake = _make_scripted_client(monkeypatch)
    texts = [f"t{i}" for i in range(130)]

    vectors = await client.embed(texts)

    assert len(vectors) == 130
    # 分块：2 次请求，每块 ≤ 128
    assert len(fake.requests) == 2
    assert len(fake.requests[0]["json"]["texts"]) == 128
    assert len(fake.requests[1]["json"]["texts"]) == 2
    # 顺序保持：每条第 i 条输入的向量编码其文本内容
    expected = [float(sum(ord(c) for c in t)) for t in texts]
    assert vectors == [[v] * 4 for v in expected]


async def test_embed_empty_returns_without_request(monkeypatch):
    """空列表直接返回空，不发起请求。"""
    client, fake = _make_scripted_client(monkeypatch)
    assert await client.embed([]) == []
    assert fake.requests == []


async def test_embed_retries_on_5xx_then_success(monkeypatch):
    """5xx 瞬时错误退避重试后成功。"""
    monkeypatch.setattr(embedding_client_module, "RETRY_BASE_DELAY", 0.0)
    client, fake = _make_scripted_client(monkeypatch, [(500, "boom")])
    # "a" 的字符码之和 = 97
    assert await client.embed(["a"]) == [[97.0, 97.0, 97.0, 97.0]]
    assert len(fake.requests) == 2  # 失败 1 次 + 重试 1 次


async def test_embed_connect_error_retried_then_success(monkeypatch):
    """ConnectError（请求未到达服务端）退避重试后成功。"""
    monkeypatch.setattr(embedding_client_module, "RETRY_BASE_DELAY", 0.0)
    client, fake = _make_scripted_client(monkeypatch, [httpx.ConnectError("dns")])
    assert len(await client.embed(["a"])) == 1
    assert len(fake.requests) == 2


async def test_embed_timeout_not_retried(monkeypatch):
    """超时不重试（请求可能已到达服务端在计算，重试会叠压服务端，实测 OOM 诱因）。"""
    monkeypatch.setattr(embedding_client_module, "RETRY_BASE_DELAY", 0.0)
    client, fake = _make_scripted_client(monkeypatch, [httpx.ReadTimeout("t")])

    with pytest.raises(EmbeddingError):
        await client.embed(["a"])
    assert len(fake.requests) == 1  # 仅尝试一次


async def test_embed_retry_exhausted_raises(monkeypatch):
    """重试耗尽（MAX_RETRIES+1 次）后抛 EmbeddingError。"""
    monkeypatch.setattr(embedding_client_module, "RETRY_BASE_DELAY", 0.0)
    max_retries = embedding_client_module.MAX_RETRIES
    script = [(500, "boom")] * (max_retries + 1)
    client, fake = _make_scripted_client(monkeypatch, script)

    with pytest.raises(EmbeddingError):
        await client.embed(["a"])
    assert len(fake.requests) == max_retries + 1


async def test_embed_422_not_retried(monkeypatch):
    """422（契约不符，如超限）不重试，直接抛错。"""
    monkeypatch.setattr(embedding_client_module, "RETRY_BASE_DELAY", 0.0)
    client, fake = _make_scripted_client(monkeypatch, [(422, "too many")])

    with pytest.raises(EmbeddingError):
        await client.embed(["a"])
    assert len(fake.requests) == 1  # 仅尝试一次
