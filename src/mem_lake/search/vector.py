"""向量语义检索：pgvector HNSW + cosine_distance。

对齐 PDD 3.3 向量引擎：负责"找描述用户登录的需求"等语义检索场景。
pgvector HNSW 索引（idx_node_vector，vector_cosine_ops）在 M3 已创建，
M4 实现检索调用。向量检索只查 knowledge_node 表，不涉及 AGE 图。

技术参考（网络搜索 pgvector-python 官方文档）：
- KnowledgeNode.content_vector.cosine_distance(query_vector) 是 pgvector-python 提供的
  SQLAlchemy 混合方法，等价于 SQL `content_vector <=> :vector`
- HNSW 索引自动用于 ORDER BY ... <=> 查询（vector_cosine_ops 操作符类）
- cosine_distance 返回距离（0~2），越小越相似；score = 1 - distance 转换为相似度（0~1）
- 查询向量通过参数化传入，非字符串拼接，零注入风险
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import FilterSpec, compile_sqlalchemy
from mem_lake.search.fusion import SearchResult, _truncate


class VectorSearcher:
    """向量语义检索器。

    构造接收 EmbeddingClient 用于生成查询向量。
    search 方法基于 pgvector cosine_distance 的 top-k 检索，返回按相似度降序的 SearchResult 列表。
    """

    def __init__(self, embedding_client: EmbeddingClient) -> None:
        self._embedding_client = embedding_client

    async def search(
        self,
        session: AsyncSession,
        query: str,
        top_k: int = 50,
        filters: FilterSpec | None = None,
    ) -> list[SearchResult]:
        """向量语义检索。

        参数：
            session: 异步数据库会话
            query: 查询文本，通过 embedding_client.embed_one 生成 1024 维查询向量
            top_k: 返回前 K 个最相似节点，默认 50
            filters: 统一过滤条件，None 时不过滤（生产环境应强制 project_id）

        返回：
            按 cosine 相似度（1 - distance）降序排序的 SearchResult 列表，
            score 字段为相似度（0~1），source 字段为 "vector"。

        边界：
            - 无向量节点（content_vector IS NULL）被自动排除（cosine_distance 对 NULL 返回 NULL，
              ORDER BY NULLS LAST 排在最后，LIMIT 截断后不出现）
            - 无匹配返回空列表
        """
        # 1. 生成查询向量（1024 维）
        # 指令感知：检索查询使用模型内置 "query" 指令，将查询摆入与文档对齐的子空间，
        # 提升召回/精度（官方称通常 +1~5%）。文档落库向量保持默认（无指令），
        # 二者配套使用，不可对文档侧加 query 指令，否则空间错配。
        query_vector = await self._embedding_client.embed_one(query, prompt_name="query")

        # 2. 构造 SQLAlchemy 查询
        # cosine_distance 是 pgvector-python 提供的混合方法，返回距离表达式
        # 同时 select 节点对象与距离值，用于计算 score = 1 - distance
        distance = KnowledgeNode.content_vector.cosine_distance(query_vector)
        stmt = (
            select(KnowledgeNode, distance.label("distance"))
            # 跳过未向量化节点（content_vector 为 NULL 时 cosine_distance 为 NULL，
            # 既无意义也会在 score = 1 - dist 时报 TypeError）
            .where(KnowledgeNode.content_vector.isnot(None))
            .order_by(distance.asc())
            .limit(top_k)
        )

        # 3. 编译 FilterSpec 为 WHERE 子句
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            stmt = stmt.where(*where_clauses)

        # 4. 执行查询
        result = await session.execute(stmt)

        # 5. 构造 SearchResult 列表
        search_results: list[SearchResult] = []
        for row in result:
            node: KnowledgeNode = row[0]
            dist: float = row[1]
            # score = 1 - cosine_distance（0~1，越大越相似）
            similarity = 1.0 - dist
            search_results.append(
                SearchResult(
                    node_id=node.id,
                    title=node.title,
                    content=_truncate(node.content),
                    node_type=node.type,
                    score=similarity,
                    source="vector",
                    properties=node.properties or {},
                    tags=node.tags or [],
                )
            )

        return search_results

    async def search_by_vector(
        self,
        session: AsyncSession,
        query_vector: list[float],
        *,
        top_k: int = 50,
        filters: FilterSpec | None = None,
    ) -> list[SearchResult]:
        """向量语义检索（预计算查询向量）。

        与 search 逻辑一致，但跳过内部 embed_one，直接使用调用方提供的查询向量。
        用于冲突检测批量化：调用方一次性批量 embed 所有查询文本后，逐条传入
        预计算向量比对，避免每节点各 embed 一次。查询向量的生成方式（含指令感知
        prompt_name）由调用方保证与 search 一致。

        参数：
            query_vector: 1024 维查询向量
            top_k / filters: 同 search
        """
        # 2. 构造 SQLAlchemy 查询（复用 search 的检索逻辑，仅查询向量来自参数）
        distance = KnowledgeNode.content_vector.cosine_distance(query_vector)
        stmt = (
            select(KnowledgeNode, distance.label("distance"))
            .where(KnowledgeNode.content_vector.isnot(None))
            .order_by(distance.asc())
            .limit(top_k)
        )

        # 3. 编译 FilterSpec 为 WHERE 子句
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            stmt = stmt.where(*where_clauses)

        # 4. 执行查询
        result = await session.execute(stmt)

        # 5. 构造 SearchResult 列表
        search_results: list[SearchResult] = []
        for row in result:
            node: KnowledgeNode = row[0]
            dist: float = row[1]
            similarity = 1.0 - dist
            search_results.append(
                SearchResult(
                    node_id=node.id,
                    title=node.title,
                    content=_truncate(node.content),
                    node_type=node.type,
                    score=similarity,
                    source="vector",
                    properties=node.properties or {},
                    tags=node.tags or [],
                )
            )

        return search_results
