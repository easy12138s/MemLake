"""Embedding 服务：基于 sentence-transformers + Qwen3-Embedding-0.6B 的独立推理服务。

可选加载 bge-reranker-base（CrossEncoder）提供 /rerank 精排；未配置 RERANK_MODEL_PATH 时
仅提供 /embed 与 /health，不阻断启动（精排为可降级依赖）。
"""

import os

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder, SentenceTransformer

app = FastAPI(title="Mem Lake Embedding Service")

model_path = os.environ.get("MODEL_PATH", "/models/Qwen3-Embedding-0.6B")
device = os.environ.get("DEVICE", "cpu")
model = SentenceTransformer(model_path, device=device)

# 可选 rerank 模型：仅当 RERANK_MODEL_PATH 非空时加载（不阻断启动）
rerank_model_path = os.environ.get("RERANK_MODEL_PATH", "")
_reranker = None
if rerank_model_path:
    try:
        _reranker = CrossEncoder(rerank_model_path, device=device)
    except Exception as exc:  # noqa: BLE001 - 加载失败不阻断启动，health 反映不可用
        _reranker = None
        import logging

        logging.getLogger("embedding_server").warning(
            "rerank 模型加载失败: %s，精排不可用", exc
        )


class EmbedRequest(BaseModel):
    texts: list[str]
    # 指令感知（instruction-aware）：可选任务指令，仅改变向量子空间，不影响归一化。
    # prompt_name 使用模型内置指令（如 "query"）；prompt 为自定义指令（建议英文）。
    # 二者均为 None 时退化为默认编码，保持与历史落库文档向量兼容。
    prompt: str | None = None
    prompt_name: str | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dimension: int


class RerankRequest(BaseModel):
    query: str
    texts: list[str]


class RerankResponse(BaseModel):
    # scores 与 order 同长。order[i] 表示第 i 个高分值在原 texts 中的索引；
    # 客户端按 order 还原即可得到按分数降序的 texts 顺序。
    scores: list[float]
    order: list[int]


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": model_path,
        "dimension": model.get_sentence_embedding_dimension(),
        "has_rerank": _reranker is not None,
    }


# 并发安全性实证结论（2026-08-22，AUDIT §2.13）：
# 对 /embed 以 16 线程 × 400 批次压测，0 错误、同文本向量最大距离 0.000000
#（与串行基准一致，无状态污染）。当前模型/引擎在本部署下并发 encode 安全，
# 因此不加全局锁（按实证而非臆测决策）；若未来换模型/引擎出现并发不稳定，
# 再考虑在 encode/rerank 外包 threading.Lock。
MAX_EMBED_TEXTS = 128   # 单次 embed 文本数上限（防超大请求 OOM）
MAX_TEXT_CHARS = 32000  # 单条文本上限（超长截断，防超长输入拖垮推理）


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    if not req.texts:
        # 空列表短路：encode([]) 返回 (0,) 无第二维，embs.shape[1] 会 IndexError
        return EmbedResponse(
            embeddings=[], dimension=model.get_sentence_embedding_dimension()
        )
    if len(req.texts) > MAX_EMBED_TEXTS:
        raise HTTPException(
            status_code=422, detail=f"texts 数量超过上限 {MAX_EMBED_TEXTS}"
        )
    texts = [t[:MAX_TEXT_CHARS] for t in req.texts]
    embs = model.encode(
        texts,
        normalize_embeddings=True,
        prompt=req.prompt,
        prompt_name=req.prompt_name,
    )
    return EmbedResponse(embeddings=embs.tolist(), dimension=embs.shape[1])


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    if _reranker is None:
        raise HTTPException(status_code=503, detail="rerank 模型未加载")
    if not req.texts:
        return RerankResponse(scores=[], order=[])
    texts = [t[:MAX_TEXT_CHARS] for t in req.texts]
    if len(texts) > MAX_EMBED_TEXTS:
        raise HTTPException(
            status_code=422, detail=f"texts 数量超过上限 {MAX_EMBED_TEXTS}"
        )
    pairs = [(req.query, t) for t in texts]
    # predict 返回数组：bge-reranker-base 为一维标量 (N,)，部分模型为 (N,1)。
    # ravel() 统一展平为标量后取 float，兼容两种形状。
    pred = np.asarray(_reranker.predict(pairs)).ravel()
    scores = [float(s) for s in pred]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return RerankResponse(scores=[scores[i] for i in order], order=order)
