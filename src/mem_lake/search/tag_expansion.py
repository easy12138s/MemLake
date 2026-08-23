"""标签语义扩展：基于 embedding 将查询标签扩展为语义相近的项目标签。

用于检索时放宽精确标签匹配（P3）。给定查询标签与项目标签词表，
对每个查询标签做 embedding，找词表中余弦相似度 >= 阈值的标签加入候选集，
从而实现「性能」≈「N+1」这类语义相近标签的召回。

设计要点：
- expand_query_tags 为纯函数（embed_fn 可注入），便于单测与无 DB 场景复用。
- expand_tags_for_project 为编排层：拉取项目标签词表 + 调用 embedding 服务扩展。
- embed_fn(texts) -> 与输入同序的向量列表；向量假定已 L2 归一化（余弦 = 点积）。
"""

import logging
import uuid
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.embedding.client import EmbeddingClient

logger = logging.getLogger("mem_lake.search.tag_expansion")

DEFAULT_THRESHOLD = 0.7


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度；向量已归一化时等价于点积。"""
    if len(a) != len(b):
        raise ValueError("向量维度不一致")
    return float(sum(x * y for x, y in zip(a, b)))


def expand_query_tags(
    query_tags: list[str],
    vocab: list[str],
    embed_fn: Callable[[list[str]], list[list[float]]],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[str]:
    """纯函数：将 query_tags 扩展为包含语义相近 vocab 标签的候选集。

    embed_fn 接收文本列表、返回与输入同序的向量列表。
    返回去重后的标签列表（始终包含原始 query_tags）；无 vocab 时原样返回。
    """
    if not query_tags:
        return []

    if not vocab:
        return list(dict.fromkeys(query_tags))

    # 合并去重后统一 embed 一次，避免重复请求
    union = list(dict.fromkeys(list(query_tags) + list(vocab)))
    vectors = embed_fn(union)
    if len(vectors) != len(union):
        raise ValueError("embed_fn 返回的向量数量与输入不一致")
    vec_map = dict(zip(union, vectors))

    expanded: list[str] = list(dict.fromkeys(query_tags))
    for qt in query_tags:
        qv = vec_map[qt]
        for vt in vocab:
            if vt in expanded:
                continue
            if cosine_similarity(qv, vec_map[vt]) >= threshold:
                expanded.append(vt)
    return expanded


async def fetch_project_tag_vocab(
    session: AsyncSession,
    project_id: uuid.UUID,
    node_type: str | None = None,
) -> list[str]:
    """从 knowledge_node 拉取项目内去重标签词表（用于语义扩展）。"""
    from mem_lake.knowledge.repository import get_distinct_tags

    return await get_distinct_tags(session, project_id=project_id, node_type=node_type)


async def expand_tags_for_project(
    embedding_client: EmbeddingClient,
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tags: list[str],
    node_type: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[str]:
    """编排：拉取项目标签词表并对查询标签做向量语义扩展。

    无词表时原样返回 tags；embedding 异常时由调用方决定降级（本函数不吞异常）。
    """
    if not tags:
        return []

    vocab = await fetch_project_tag_vocab(session, project_id, node_type=node_type)
    if not vocab:
        return list(dict.fromkeys(tags))

    # embed 为异步，先取向量再交纯函数处理（保持 expand_query_tags 可单测）
    union = list(dict.fromkeys(list(tags) + list(vocab)))
    vectors = await embedding_client.embed(union)
    vec_lookup = dict(zip(union, vectors))
    return expand_query_tags(
        tags,
        vocab,
        lambda texts: [vec_lookup[t] for t in texts],
        threshold=threshold,
    )
