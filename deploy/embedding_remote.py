"""OpenAI 兼容 embedding API 转发的纯逻辑（EMBEDDING_PROVIDER=remote）。

仅含请求构造与响应解析的纯函数，供 embedding_server.py 在 remote 模式下复用。
无 httpx 依赖（HTTP 传输在 embedding_server.py 内完成），便于单元测试。
"""


def build_embed_request(model: str, texts: list[str], dimension: int) -> dict:
    """构造 OpenAI 兼容 /embeddings 请求体。

    input 传 str 数组（而非拼接单串），保证服务端按原顺序返回、客户端可按
    data[].index 还原顺序。dimensions 强制输出维度，用于把 text-embedding-3
    等默认维度不是 1024 的模型对齐到目标维度。
    """
    return {
        "model": model,
        "input": list(texts),
        "dimensions": dimension,
    }


def parse_embeddings_response(
    payload: dict, expected_count: int, expected_dim: int
) -> list[list[float]]:
    """解析 OpenAI 兼容 /embeddings 响应，按 data[].index 还原顺序并校验数量与维度。

    返回与输入 texts 顺序一致的向量列表（每个元素为 float 列表，长度 expected_dim）。
    结构异常、数量不符、维度不符或 index 不完整时抛 ValueError，由调用方转为
    HTTP 5xx 告知 app 侧 embedding 不可用。
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("embedding 响应缺少 data 数组")

    if len(data) != expected_count:
        raise ValueError(
            f"embedding 响应数量不符: 期望 {expected_count}, 实际 {len(data)}"
        )

    by_index: dict[int, list[float]] = {}
    for item in data:
        idx = item.get("index")
        vec = item.get("embedding")
        if not isinstance(idx, int):
            raise ValueError(f"embedding 响应项缺少合法 index: {item!r}")
        if not isinstance(vec, list) or len(vec) != expected_dim:
            actual = len(vec) if isinstance(vec, list) else type(vec).__name__
            raise ValueError(
                f"embedding 维度不符: 期望 {expected_dim}, 实际 {actual}"
            )
        by_index[idx] = [float(x) for x in vec]

    if len(by_index) != expected_count:
        raise ValueError(
            f"embedding 响应 index 不完整: 期望 {expected_count}, 实际 {len(by_index)}"
        )

    return [by_index[i] for i in range(expected_count)]
