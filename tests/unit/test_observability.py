"""可观测性（批次 1+2）单元测试：指标定义/输出、结构化日志配置、embedding 客户端埋点。

无需真实 embedding 容器；embedding client 用假 HTTP 客户端，断言 prometheus 指标
在调用后被上报（进程级 REGISTRY 为准）。审计 append-only 不变，本测试不触碰 DB。
"""

import logging

import pytest

from mem_lake.embedding.client import EmbeddingClient
from mem_lake.observability.logging import configure_logging
from mem_lake.observability.metrics import (
    MCP_TOOL_CALLS,
    get_metrics_body,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        if path == "/embed":
            return _FakeResponse({"embeddings": [[0.0] * 4], "dimension": 4})
        # /rerank：scores/order 同长、order 降序给出原索引
        return _FakeResponse({"scores": [0.9, 0.5], "order": [0, 1]})

    async def get(self, path: str) -> _FakeResponse:
        return _FakeResponse({"status": "ok", "model": "x", "dimension": 4})

    async def aclose(self) -> None:
        pass


@pytest.fixture
def me_client():
    """构造嵌入 fake 的 EmbeddingClient（绕过 __init__，避免真实 httpx 连接）。"""
    client = EmbeddingClient.__new__(EmbeddingClient)
    client._client = _FakeAsyncClient()
    client._dimension = 4
    return client


def test_metric_objects_defined():
    """指标对象（Counter/Histogram）应已定义且可自增/观测。"""
    # Counter 可 inc（标签按需增加，不抛错）
    MCP_TOOL_CALLS.labels(tool="dummy", status="success").inc()
    assert get_metrics_body().decode() != ""


def test_metrics_body_contains_tool_calls():
    """/metrics 输出应包含网关工具调用指标与基础指标头信息。"""
    body = get_metrics_body().decode()
    assert "memlake_mcp_tool_calls_total" in body


# fmt: off
@pytest.mark.parametrize(
    "fmt", ["console", "json"]
)   # fmt: on
def test_configure_logging_no_raise(fmt):
    """console 与 json 两种 renderer 下 configure_logging 均不抛错。"""
    configure_logging(level=logging.DEBUG, fmt=fmt)
    assert True


def test_embedding_embed_reports_metrics(me_client):
    """embed 调用后应上报 embedding 调用数（op="embed"）。"""
    loop_result = _run_async(me_client.embed(["hello"]))
    assert loop_result == [[0.0] * 4]
    assert r'op="embed"' in get_metrics_body().decode()


def test_embedding_rerank_reports_metrics(me_client):
    """rerank 调用后应上报 embedding 调用数（op="rerank"），并返回还原后的分数。"""
    result = _run_async(me_client.rerank("q", ["a", "b"]))
    assert result == [0.9, 0.5]
    assert r'op="rerank"' in get_metrics_body().decode()


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)
