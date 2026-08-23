"""Embedding 服务 HTTP 客户端。

通过 httpx.AsyncClient 调用独立运行的 embedding 容器（FastAPI + sentence-transformers），
不在 app 进程本地加载模型。容器端点：GET /health、POST /embed。
返回向量经服务端 normalize_embeddings=True 归一化，pgvector 余弦距离检索可直接使用。
"""

import time
from functools import lru_cache

import httpx

from mem_lake.config import get_settings
from mem_lake.observability.metrics import EMBEDDING_CALLS, EMBEDDING_DURATION


class EmbeddingError(Exception):
    """Embedding 服务不可用、响应非 200 或维度不符时抛出。"""


class EmbeddingClient:
    """Embedding 服务 HTTP 客户端。

    构造时从 config.EMBEDDING_HOST/PORT 拼接 base_url，内部持有 httpx.AsyncClient。
    进程内通过 get_embedding_client() 单例复用，显式 close() 释放连接。
    """

    def __init__(self, base_url: str, dimension: int, timeout: float = 30.0) -> None:
        self._base_url = base_url
        self._dimension = dimension
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def embed(
        self,
        texts: list[str],
        prompt: str | None = None,
        prompt_name: str | None = None,
    ) -> list[list[float]]:
        """批量向量化。

        POST /embed body {"texts": [...], "prompt"?, "prompt_name"?}
        → {"embeddings": [[...]], "dimension": 1024}。

        prompt / prompt_name 为指令感知参数，仅查询侧（如 VectorSearcher）按需传入，
        文档侧（落库节点）保持 None 以维持与历史向量的兼容。二者均为 None 时退化为默认编码。
        校验响应 dimension 与 config.EMBEDDING_DIMENSION 一致，不符抛 EmbeddingError。
        """
        body: dict = {"texts": texts}
        if prompt is not None:
            body["prompt"] = prompt
        if prompt_name is not None:
            body["prompt_name"] = prompt_name
        EMBEDDING_CALLS.labels(op="embed").inc()
        t = time.time()
        try:
            resp = await self._client.post("/embed", json=body)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding 服务请求失败: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingError(
                f"Embedding 服务返回非 200: status={resp.status_code} body={resp.text}"
            )

        data = resp.json()
        dim = data.get("dimension")
        if dim != self._dimension:
            raise EmbeddingError(
                f"Embedding 维度不符: 期望 {self._dimension}, 实际 {dim}"
            )

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbeddingError(f"Embedding 响应格式错误: embeddings={embeddings}")

        EMBEDDING_DURATION.labels(op="embed").observe(time.time() - t)
        return embeddings

    async def embed_one(
        self,
        text: str,
        prompt: str | None = None,
        prompt_name: str | None = None,
    ) -> list[float]:
        """单文本向量化，便捷方法，调用 embed([text]) 取首元素。

        prompt / prompt_name 透传指令感知参数（见 embed）。
        """
        result = await self.embed([text], prompt=prompt, prompt_name=prompt_name)
        return result[0]

    async def health(self) -> dict:
        """健康检查。

        GET /health → {"status":"ok","model":"...","dimension":1024,"has_rerank":bool}。
        校验 status == "ok"，返回完整响应 dict。
        """
        try:
            resp = await self._client.get("/health")
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding 健康检查失败: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingError(
                f"Embedding 健康检查非 200: status={resp.status_code} body={resp.text}"
            )

        data = resp.json()
        if data.get("status") != "ok":
            raise EmbeddingError(f"Embedding 服务状态异常: {data}")

        return data

    async def has_rerank(self) -> bool:
        """查询服务端是否已加载 rerank 模型。

        精排为可降级依赖：健康检查失败返回 False（不抛异常），调用方回退 RRF 原序。
        """
        try:
            data = await self.health()
            return bool(data.get("has_rerank", False))
        except EmbeddingError:
            return False

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """对候选文本做 cross-encoder 精排，返回与 texts 同序的分数。

        POST /rerank body {"query": str, "texts": [...]} → {"scores": [...], "order": [...]}。
        scores 与 order 同长、均按分数降序：order[i] 为原 texts 中的索引，据此还原为原序分数。
        """
        if not texts:
            return []
        body = {"query": query, "texts": texts}
        EMBEDDING_CALLS.labels(op="rerank").inc()
        t = time.time()
        try:
            resp = await self._client.post("/rerank", json=body)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Rerank 服务请求失败: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingError(
                f"Rerank 返回非 200: status={resp.status_code} body={resp.text}"
            )

        data = resp.json()
        scores = data.get("scores")
        order = data.get("order")
        if not isinstance(scores, list) or not isinstance(order, list):
            raise EmbeddingError(f"Rerank 响应格式错误: {data}")
        if len(scores) != len(order) or len(scores) != len(texts):
            raise EmbeddingError(
                f"Rerank 返回长度不符: scores={len(scores)} order={len(order)} texts={len(texts)}"
            )

        # order 按分数降序给出原索引；还原为与 texts 同序的分数
        restored = [0.0] * len(texts)
        for rank, idx in enumerate(order):
            restored[idx] = scores[rank]
        EMBEDDING_DURATION.labels(op="rerank").observe(time.time() - t)
        return restored

    async def close(self) -> None:
        """关闭底层 httpx 连接池。"""
        await self._client.aclose()


@lru_cache
def get_embedding_client() -> EmbeddingClient:
    """返回 EmbeddingClient 进程单例。

    base_url 与 dimension 从 config 读取，进程内通过 lru_cache 复用。
    """
    settings = get_settings()
    base_url = f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}"
    return EmbeddingClient(
        base_url=base_url,
        dimension=settings.EMBEDDING_DIMENSION,
    )
