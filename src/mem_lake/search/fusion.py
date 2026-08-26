"""RRF 融合排序 + 并行三引擎检索入口。

对齐 PDD 3.3/5.4：向量与全文结果通过 RRF 算法融合排序，融合在应用层完成。
PDD 硬约束：三引擎检索使用自定义轻量级融合层，并行 asyncio.gather，统一 FilterSpec 编译。

RRF 算法参考 Cormack 等人 2009 年 SIGIR 论文《Reciprocal Rank Fusion outperforms Condorcet
and individual rank learning methods》。k=60 是论文在 TREC 数据集上扫描 k=1~100 后确定的
最优默认值，已成为工业界默认（Elasticsearch、Vespa、Weaviate 等均采用）。

公式：score(d) = Σ_i 1/(k + rank_i(d))，rank 从 1 开始，同节点在多列表出现分数累加。
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace

from mem_lake.config import get_settings
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.embedding.client import EmbeddingClient, EmbeddingError
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.observability.metrics import RERANK_FALLBACK, SEARCH_ENGINE_DURATION
from mem_lake.search.filters import FilterSpec

logger = logging.getLogger("mem_lake.search.fusion")


@dataclass
class SearchResult:
    """三引擎统一检索结果。

    向量/全文引擎的 score 是原始分数（cosine 相似度 0~1 / ts_rank_cd 0.x），
    图遍历的 score 为 None（图遍历无相似度概念）。
    RRF 融合后 score 替换为 RRF 分数，source 替换为 "fused"。
    """

    node_id: uuid.UUID
    title: str
    content: str  # 摘要，截断前 200 字符
    node_type: str
    score: float | None
    source: str  # "vector" / "fulltext" / "graph" / "fused"
    properties: dict
    tags: list
    # 图遍历上下文（仅 get_requirement_context 路径填充，其他来源为 None）：
    edge_types: list[str] | None = None  # 路径边类型列表（从起点出发每一跳一个）
    graph_depth: int | None = None  # 路径跳数（1=直接关联）

    # 注意：fused 结果的 score 为向量余弦分（0~1，来自 vector 引擎），便于判相关性；
    # 排序仍由 RRF 排名决定。RRF 原始分数仅用于融合排序，不直接透出。


# 摘要截断长度（PDD 3.3：返回结果含原文摘要）
SUMMARY_MAX_LENGTH = 200


def _truncate(text: str, max_length: int = SUMMARY_MAX_LENGTH) -> str:
    """截断文本为摘要长度，超出部分以省略号标记。"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def _apply_vector_scores(
    fused: list[SearchResult],
    vector_results: list[SearchResult],
) -> list[SearchResult]:
    """将融合结果的 score 替换为对应节点的向量余弦分（0~1）。

    排序仍由 RRF 决定（fused 已按 RRF 排好），此处仅替换展示/阈值用的 score。
    无向量分（仅全文命中）的节点保留原 RRF 分（不入 map）。
    """
    vector_score_map = {
        r.node_id: r.score for r in vector_results if r.score is not None
    }
    if not vector_score_map:
        return fused
    return [
        replace(r, score=vector_score_map[r.node_id])
        if r.node_id in vector_score_map
        else r
        for r in fused
    ]


async def _apply_rerank(
    fused: list[SearchResult],
    embedding_client: EmbeddingClient,
    top_n: int,
    query: str,
) -> list[SearchResult]:
    """对融合候选做 cross-encoder 精排（可选、可降级）。

    - 仅当配置启用（settings.ENABLE_RERANK 且 RERANK_MODEL_PATH 非空）且服务端已加载
      rerank 模型时执行。
    - 取 fused[:RERANK_TOP_K] 交精排，按精排分数降序重组；尾部（超出 RERANK_TOP_K）
      保持原 RRF 序，最终长度不超过 top_n。
    - 容错：任何 rerank 异常（服务不可用/未加载/响应异常）静默回退为原顺序（截断到
      top_n），不阻断检索。
    - score 字段保持不变（保留向量余弦分），仅改变返回顺序。
    """
    settings = get_settings()
    if not settings.ENABLE_RERANK or not settings.RERANK_MODEL_PATH or not fused:
        return fused[:top_n]

    rerank_candidate = fused[: settings.RERANK_TOP_K]
    try:
        if not await embedding_client.has_rerank():
            return fused[:top_n]
        # 精排对象用 "标题\n正文"（标题在前，与 build_embed_text 的构造顺序对齐；
        # 空正文退化为标题本身）。
        texts = [f"{r.title}\n{r.content or ''}".strip() for r in rerank_candidate]
        scores = await embedding_client.rerank(query, texts)
    except EmbeddingError:
        RERANK_FALLBACK.inc()
        logger.warning("rerank 精排不可用，回退 RRF 原序", exc_info=True)
        return fused[:top_n]

    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    reranked = [rerank_candidate[i] for i in order]
    return (reranked + fused[settings.RERANK_TOP_K :])[:top_n]


def rrf_fuse(
    result_lists: list[list[SearchResult]],
    k: int = 60,
    top_n: int = 10,
) -> list[SearchResult]:
    """RRF 倒数排名融合。

    参数：
        result_lists: 多个引擎的检索结果列表（每个列表内已按相关性降序排序）
        k: 平滑常数，默认 60（Cormack 2009 论文最优值）。k 越大排名差异影响越小，融合越"民主"
        top_n: 融合后返回前 N 个结果，默认 10（PDD 5.4）

    返回：
        融合后按 RRF 分数降序排序的 SearchResult 列表，长度 <= top_n。
        每个 SearchResult 的 score 替换为 RRF 分数，source 替换为 "fused"，
        其余字段保留首次出现时的值。

    边界：
        - 空列表输入返回空列表
        - 单列表输入等价于按原顺序取前 top_n（rank 单一来源）
        - 同一 node_id 在多列表出现分数累加（RRF 核心优势：多引擎一致认可得分更高）
    """
    if not result_lists:
        return []

    scores: dict[uuid.UUID, float] = defaultdict(float)
    meta: dict[uuid.UUID, SearchResult] = {}

    for result_list in result_lists:
        # rank 从 1 开始，rank=1 是第一名，RRF 分数 1/(k+1)
        for rank, result in enumerate(result_list, start=1):
            scores[result.node_id] += 1.0 / (k + rank)
            # 保留首次出现的 SearchResult（含原始 title/content/node_type 等）
            if result.node_id not in meta:
                meta[result.node_id] = result

    # 按累加 RRF 分数降序排序
    fused_sorted = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # 替换 score 与 source，截断 top_n
    return [
        replace(meta[nid], score=score, source="fused")
        for nid, score in fused_sorted[:top_n]
    ]


async def hybrid_search(
    query: str,
    embedding_client: EmbeddingClient,
    graph_store: GraphStore,
    top_k: int = 50,
    top_n: int = 10,
    filters: FilterSpec | None = None,
    graph_node_id: uuid.UUID | None = None,
    graph_depth: int = 3,
) -> dict:
    """并行三引擎混合检索。

    向量与全文引擎并行执行并 RRF 融合；图遍历作为独立路径仅在提供 graph_node_id 时执行
    （组合模式：先向量找起点，再图遍历展开），结果作为 "graph" 字段独立返回不参与融合。

    每引擎独立 DB session：AsyncSession 非并发安全（SQLAlchemy 限制），
    asyncio.gather 共享单一 session 会触发锁竞争甚至报错；独立 session 使
    三引擎在各自连接上真并行，总延迟 ≈ max(三引擎延迟)。检索为只读操作，
    独立 session 不涉及事务一致性问题。

    参数：
        query: 查询文本（向量引擎 embed_one 生成查询向量，全文引擎 websearch_to_tsquery）
        embedding_client: Embedding 客户端
        graph_store: GraphStore 实现（AGEGraphStore）
        top_k: 单引擎召回数，默认 50（PDD 5.4）
        top_n: 融合后返回数，默认 10（PDD 5.4）
        filters: 统一过滤条件
        graph_node_id: 图遍历起点节点 ID，None 时不执行图遍历
        graph_depth: 图遍历深度，默认 3

    返回：
        {"fused": [...], "vector": [...], "fulltext": [...], "graph": [...]}
        fused 长度 <= top_n，vector/fulltext 长度 <= top_k，graph 长度取决于图遍历结果
    """
    # 延迟导入避免循环依赖：fusion 被 __init__ 导出，vector/fulltext/graph 也被导出
    from mem_lake.search.fulltext import FullTextSearcher
    from mem_lake.search.graph import GraphSearcher
    from mem_lake.search.vector import VectorSearcher

    vector_searcher = VectorSearcher(embedding_client)
    fulltext_searcher = FullTextSearcher()
    graph_searcher = GraphSearcher(graph_store)

    async def _vector_task() -> list[SearchResult]:
        t = time.time()
        async with AsyncSessionLocal() as s:
            result = await vector_searcher.search(s, query, top_k, filters)
        SEARCH_ENGINE_DURATION.labels(engine="vector").observe(time.time() - t)
        return result

    async def _fulltext_task() -> list[SearchResult]:
        t = time.time()
        async with AsyncSessionLocal() as s:
            result = await fulltext_searcher.search(s, query, top_k, filters)
        SEARCH_ENGINE_DURATION.labels(engine="fulltext").observe(time.time() - t)
        return result

    async def _graph_task() -> list[SearchResult]:
        if graph_node_id is None:
            return []
        t = time.time()
        async with AsyncSessionLocal() as s:
            result = await graph_searcher.traverse(
                s, graph_node_id, depth=graph_depth, filters=filters
            )
        SEARCH_ENGINE_DURATION.labels(engine="graph").observe(time.time() - t)
        return result

    # 并行执行三引擎检索，asyncio.gather 总延迟 ≈ max(三引擎延迟)
    vector_results, fulltext_results, graph_results = await asyncio.gather(
        _vector_task(), _fulltext_task(), _graph_task()
    )

    # 向量与全文 RRF 融合。启用精排时先融合出 ≥ RERANK_TOP_K 的候选池（而非直接截断到
    # top_n），rerank 在候选池内重排后再收口到 top_n——否则 rerank 只在前 top_n 内重排，
    # 无法挽回被 RRF 排到 6~30 名的相关结果（A/B 对照验证：候选池 30 使 hit@5 提升）。
    settings = get_settings()
    candidate_n = top_n
    if settings.ENABLE_RERANK and settings.RERANK_MODEL_PATH:
        candidate_n = max(top_n, settings.RERANK_TOP_K)
    fused = rrf_fuse([vector_results, fulltext_results], k=60, top_n=candidate_n)

    # 融合结果 score 透出向量余弦分（0~1）：便于调用方判相关性，并修复
    # check_requirement_conflicts 用 r.score >= threshold(0.85) 过滤时 RRF 小数
    # 永远不触发的问题。排序仍由 RRF 决定（fused_sorted 已按 RRF 排好），此处仅
    # 替换展示/阈值用的 score；无向量分（仅全文命中）的节点保留原 RRF 分。
    fused = _apply_vector_scores(fused, vector_results)

    # 精排（可降级）：服务启用 rerank 时，对融合候选做 cross-encoder 重排。
    # 排序改由精排分数决定，score 字段仍保留向量余弦分（与 "RRF 排序 + 向量 score" 的
    # 既有设计一致，不破坏冲突检测/阈值过滤语义）。服务不可用/未加载时静默回退 RRF 原序。
    fused = await _apply_rerank(fused, embedding_client, top_n, query)

    return {
        "fused": fused,
        "vector": vector_results,
        "fulltext": fulltext_results,
        "graph": graph_results,
    }
