"""Embedding 服务：基于 sentence-transformers + Qwen3-Embedding-0.6B 的独立推理服务。

可选加载 bge-reranker-base（CrossEncoder）提供 /rerank 精排；未配置 RERANK_MODEL_PATH 时
仅提供 /embed 与 /health，不阻断启动（精排为可降级依赖）。
"""

import logging
import os
import time

import numpy as np
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

# provider 分流：local（本地 sentence-transformers 推理，默认）／ remote（OpenAI 兼容 API 转发）
PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "local")
# remote 模式目标向量维度（与 app 侧 config.EMBEDDING_DIMENSION 对齐）
REMOTE_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "1024"))

if PROVIDER == "remote":
    import httpx
    from embedding_remote import build_embed_request, parse_embeddings_response
else:
    from sentence_transformers import CrossEncoder, SentenceTransformer

# 结构化日志：与 mem-lake 网关（observability/logging.py configure_logging）保持同构——
# 相同 structlog stdlib 桥接、相同 OBS_LOG_FORMAT 驱动（json / console）。
# 独立进程不 import mem_lake，自带一份等价配置，保证三容器日志格式一致。
# 业务日志用 stdlib logging %-format（如 mem-lake 的 TOOL_CALL 行），经 ProcessorFormatter
# 输出结构化行（level/logger/timestamp 由 foreign_pre_chain 注入）。
_LOG_FMT = os.environ.get("OBS_LOG_FORMAT", "console")

structlog.configure(
    processors=[
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

_renderer = (
    structlog.processors.JSONRenderer()
    if _LOG_FMT == "json"
    else structlog.dev.ConsoleRenderer()
)
_handler = logging.StreamHandler()
_handler.setFormatter(
    structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
        processors=[
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _renderer,
        ],
    )
)

_root = logging.getLogger()
_root.setLevel(logging.INFO)
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_root.addHandler(_handler)

logger = logging.getLogger("embedding_server")

app = FastAPI(title="Mem Lake Embedding Service")

# 进程内指标（与服务进程独立命名，字符串名即可，不 import app 的 metrics 模块）
EMBED_EMBED_CALLS = Counter(
    "memlake_embedding_embed_calls_total", "embedding 服务 /embed 调用次数"
)
EMBED_RERANK_CALLS = Counter(
    "memlake_embedding_rerank_calls_total", "embedding 服务 /rerank 调用次数"
)
EMBED_DURATION = Histogram(
    "memlake_embedding_endpoint_duration_seconds",
    "embedding 服务端点耗时（秒）",
    ["op"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

model_path = os.environ.get("MODEL_PATH", "/models/Qwen3-Embedding-0.6B")
device = os.environ.get("DEVICE", "cpu")

# remote 模式：不加载本地模型，/embed 转发第三方 API（同步 httpx 客户端延迟初始化）。
# local 模式：加载 SentenceTransformer 本地推理（现状）。
if PROVIDER == "remote":
    model = None
    _remote_client = None
    _remote_base = os.environ.get("EMBEDDING_API_BASE", "").rstrip("/")
    _remote_key = os.environ.get("EMBEDDING_API_KEY", "")
    _remote_model = os.environ.get("EMBEDDING_API_MODEL", "")
else:
    _remote_client = None
    _remote_base = ""
    _remote_key = ""
    _remote_model = ""
    model = SentenceTransformer(model_path, device=device)

# OOM 防护（2026-09-02 小批量导入实测：10 条 8k-token 文档单次 encode >6min 未完成，
# 内存一路涨到 6.2GiB 逼近容器上限）。两处调参：
# 1) max_seq_length 8192→2048：token 截断上限降 4 倍，单条推理计算量/激活内存同步降 4 倍。
#    注意：截断口径变化会使超长文本的向量与旧口径不可复现——已有存量向量需重建（reindex）。
# 2) encode batch_size 默认 32→8：encode 内部按批 pad 推理，批越大峰值激活内存越高，
#    CPU 低配机器上直接压垮容器。吞吐不足时优先迁移 embedding 容器，而非调大批量。
if PROVIDER != "remote":
    model.max_seq_length = int(os.environ.get("EMBEDDING_MAX_SEQ_LENGTH", "2048"))
ENCODE_BATCH_SIZE = int(os.environ.get("EMBEDDING_ENCODE_BATCH_SIZE", "8"))


def _model_dimension() -> int:
    """模型输出维度。remote 模式返回配置的 REMOTE_DIMENSION；local 模式从模型取。

    local 优先新方法名 get_embedding_dimension（sentence-transformers 已弃用旧名），
    旧版本兜底用 get_sentence_embedding_dimension，避免 FutureWarning。
    """
    if PROVIDER == "remote":
        return REMOTE_DIMENSION
    getter = getattr(model, "get_embedding_dimension", None) or getattr(
        model, "get_sentence_embedding_dimension"
    )
    return int(getter())


# 可选 rerank 模型：仅 local 模式且 RERANK_MODEL_PATH 非空时加载（不阻断启动）。
# remote 模式无本地 CrossEncoder，禁用精排（has_rerank 恒 False，app 侧回退 RRF 原序）。
rerank_model_path = os.environ.get("RERANK_MODEL_PATH", "")
_reranker = None
if PROVIDER != "remote" and rerank_model_path:
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


@app.get("/metrics")
def metrics():
    """Prometheus 拉取端点（面向内网，默认不加鉴权）。"""
    return PlainTextResponse(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "provider": PROVIDER,
        "model": _remote_model if PROVIDER == "remote" else model_path,
        "dimension": _model_dimension(),
        "has_rerank": _reranker is not None,
    }


# 并发安全性实证结论（2026-08-22，AUDIT §2.13）：
# 对 /embed 以 16 线程 × 400 批次压测，0 错误、同文本向量最大距离 0.000000
#（与串行基准一致，无状态污染）。当前模型/引擎在本部署下并发 encode 安全，
# 因此不加全局锁（按实证而非臆测决策）；若未来换模型/引擎出现并发不稳定，
# 再考虑在 encode/rerank 外包 threading.Lock。
MAX_EMBED_TEXTS = 128   # 单次 embed 文本数上限（防超大请求 OOM）
MAX_TEXT_CHARS = 32000  # 单条文本字符硬上限（兜底，防极端超长输入拖垮推理）

# 32k 适配：字符 ≠ token，字符级截断会"隐形误切"（中英文 token 比不同），
# 且与 sentence-transformers 内部截断口径不一致导致向量不可复现。改用 tokenizer 计数，
# 按模型 max_seq_length 截断（预留特殊 token 余量），与推理端编码口径一致。
_TOKEN_MARGIN = 8


def _truncate_to_tokens(text: str) -> str:
    """按模型 tokenizer 将文本截断到 max_seq_length - 余量，返回解码后文本。

    字符硬上限先挡住极端输入，再 token 截断。多向量 facet 文本通常很短，极少触发。
    """
    if not text:
        return ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    max_len = int(getattr(model, "max_seq_length", 8192)) - _TOKEN_MARGIN
    if max_len <= 0:
        max_len = 1
    tok = model.tokenizer
    ids = tok.encode(text, max_length=max_len, truncation=True, add_special_tokens=False)
    return tok.decode(ids, skip_special_tokens=False)


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    EMBED_EMBED_CALLS.inc()
    t = time.time()
    try:
        resp = _embed_impl(req)
        logger.info(
            "EMBED_CALL op=embed texts=%d dim=%d duration=%.0fms",
            len(req.texts),
            resp.dimension,
            (time.time() - t) * 1000,
        )
        return resp
    except Exception as exc:
        logger.warning("EMBED_CALL op=embed texts=%d status=error error=%s", len(req.texts), exc)
        raise
    finally:
        EMBED_DURATION.labels(op="embed").observe(time.time() - t)


def _ensure_remote_client() -> "httpx.Client":
    """延迟初始化 remote 同步 HTTP 客户端（进程单例，惰性避免 local 模式引入开销）。"""
    global _remote_client
    if _remote_client is None:
        _remote_client = httpx.Client(timeout=600.0)
    return _remote_client


def _remote_embed(texts: list[str]) -> EmbedResponse:
    """remote 模式：转发 OpenAI 兼容 /embeddings，按 index 还原顺序并校验维度。"""
    if not _remote_base or not _remote_key or not _remote_model:
        raise HTTPException(
            status_code=503,
            detail="remote 模式缺少 EMBEDDING_API_BASE/EMBEDDING_API_KEY/EMBEDDING_API_MODEL 配置",
        )

    # 字符硬上限兜底（第三方 API 自截断，无需本地 tokenizer）
    clipped = [t[:MAX_TEXT_CHARS] for t in texts]
    body = build_embed_request(_remote_model, clipped, REMOTE_DIMENSION)
    try:
        resp = _ensure_remote_client().post(
            f"{_remote_base}/embeddings",
            headers={"Authorization": f"Bearer {_remote_key}"},
            json=body,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"第三方 embedding 请求失败: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"第三方 embedding 返回 {resp.status_code}: {resp.text[:200]}",
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail=f"第三方 embedding 响应非 JSON: {exc}"
        ) from exc

    try:
        embeddings = parse_embeddings_response(payload, len(clipped), REMOTE_DIMENSION)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return EmbedResponse(embeddings=embeddings, dimension=REMOTE_DIMENSION)


def _embed_impl(req: EmbedRequest) -> EmbedResponse:
    if not req.texts:
        # 空列表短路：encode([]) 返回 (0,) 无第二维，embs.shape[1] 会 IndexError
        return EmbedResponse(embeddings=[], dimension=_model_dimension())
    if len(req.texts) > MAX_EMBED_TEXTS:
        raise HTTPException(
            status_code=422, detail=f"texts 数量超过上限 {MAX_EMBED_TEXTS}"
        )

    if PROVIDER == "remote":
        return _remote_embed(req.texts)

    texts = [_truncate_to_tokens(t) for t in req.texts]
    embs = model.encode(
        texts,
        normalize_embeddings=True,
        prompt=req.prompt,
        prompt_name=req.prompt_name,
        show_progress_bar=False,
        batch_size=ENCODE_BATCH_SIZE,
    )
    return EmbedResponse(embeddings=embs.tolist(), dimension=embs.shape[1])


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    EMBED_RERANK_CALLS.inc()
    t = time.time()
    try:
        resp = _rerank_impl(req)
        logger.info(
            "RERANK_CALL op=rerank texts=%d duration=%.0fms",
            len(req.texts),
            (time.time() - t) * 1000,
        )
        return resp
    except Exception as exc:
        logger.warning("RERANK_CALL op=rerank texts=%d status=error error=%s", len(req.texts), exc)
        raise
    finally:
        EMBED_DURATION.labels(op="rerank").observe(time.time() - t)


def _rerank_impl(req: RerankRequest) -> RerankResponse:
    if _reranker is None:
        raise HTTPException(status_code=503, detail="rerank 模型未加载")
    if not req.texts:
        return RerankResponse(scores=[], order=[])
    texts = [_truncate_to_tokens(t) for t in req.texts]
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
