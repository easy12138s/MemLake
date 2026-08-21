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


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dimension: int


@app.get("/health")
def health():
    return {"status": "ok", "model": model_path, "dimension": model.get_sentence_embedding_dimension()}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    embs = model.encode(req.texts, normalize_embeddings=True)
    return EmbedResponse(embeddings=embs.tolist(), dimension=embs.shape[1])
