"""M2 集成测试：Embedding 服务 HTTP 客户端。

调用真实 embedding 容器（localhost:8001），验证 health / embed / embed_one / 维度校验。
"""

import pytest

from mem_lake.config import get_settings
from mem_lake.embedding.client import EmbeddingClient, EmbeddingError


@pytest.fixture
async def embedding_client():
    """每次测试独立 client，teardown 关闭连接。"""
    settings = get_settings()
    base_url = f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}"
    client = EmbeddingClient(base_url=base_url, dimension=settings.EMBEDDING_DIMENSION)
    yield client
    await client.close()


async def test_health(embedding_client):
    """health() 返回 status=ok 且 dimension=1024。"""
    data = await embedding_client.health()
    assert data["status"] == "ok"
    assert data["dimension"] == 1024


async def test_embed_single(embedding_client):
    """embed_one 返回长度 1024 的 list[float]。"""
    vec = await embedding_client.embed_one("测试文本")
    assert isinstance(vec, list)
    assert len(vec) == 1024
    assert all(isinstance(x, float) for x in vec)


async def test_embed_batch(embedding_client):
    """embed 返回 2 个长度 1024 的向量。"""
    vecs = await embedding_client.embed(["a", "b"])
    assert isinstance(vecs, list)
    assert len(vecs) == 2
    assert all(len(v) == 1024 for v in vecs)


async def test_dimension_mismatch_raises(monkeypatch):
    """配置 EMBEDDING_DIMENSION=512 时调用 embed 抛 EmbeddingError。"""
    settings = get_settings()
    base_url = f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}"
    # 构造 dimension=512 的 client，与服务端实际 1024 不符
    client = EmbeddingClient(base_url=base_url, dimension=512)
    try:
        with pytest.raises(EmbeddingError, match="维度不符"):
            await client.embed(["测试"])
    finally:
        await client.close()
