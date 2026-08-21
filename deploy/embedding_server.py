"""Embedding 服务：基于 sentence-transformers + Qwen3-Embedding-0.6B 的独立推理服务"""

import os

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

app = FastAPI(title="Mem Lake Embedding Service")

model_path = os.environ.get("MODEL_PATH", "/models/Qwen3-Embedding-0.6B")
device = os.environ.get("DEVICE", "cpu")
model = SentenceTransformer(model_path, device=device)


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


@app.get("/health")
def health():
    return {"status": "ok", "model": model_path, "dimension": model.get_sentence_embedding_dimension()}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    embs = model.encode(
        req.texts,
        normalize_embeddings=True,
        prompt=req.prompt,
        prompt_name=req.prompt_name,
    )
    return EmbedResponse(embeddings=embs.tolist(), dimension=embs.shape[1])
