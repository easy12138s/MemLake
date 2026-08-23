"""向量语义检索：pgvector HNSW + 归一化内积（max_inner_product / <#>）。

对齐 PDD 3.3 向量引擎：负责"找描述用户登录的需求"等语义检索场景。
pgvector HNSW 索引（idx_node_vector，vector_ip_ops，m=32/ef_construction=400）由
create_all 创建；M4 实现检索调用。向量检索只查 knowledge_node 表，不涉及 AGE 图。

技术参考（网络搜索 pgvector-python 官方文档）：
- content_vector 均来自归一化 embedding 服务（/embed normalize_embeddings=True），
  因此内积 `<#>` 与余弦等价。KnowledgeNode.content_vector.max_inner_product(query_vector)
  是 pgvector-python 提供的 SQLAlchemy 混合方法，等价于 SQL `content_vector <#> :vector`
  （注意：pgvector-python 的 `<#>` 对应方法名为 max_inner_product，无 inner_product）
- HNSW 索引自动用于 ORDER BY <#> 查询（vector_ip_ops 操作符类）；<#> 返回负内积，
  归一化下 score = -max_inner_product = 余弦 ∈[-1,1]，负值截断为 0
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
    search 方法基于 pgvector 内积（归一化向量下等价余弦）的 top-k 检索，返回按相似度降序的 SearchResult 列表。
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
            按相似度（归一化向量下内积 = 余弦，`-max_inner_product`）降序排序的 SearchResult 列表，
            score 字段为相似度（0~1），source 字段为 "vector"。

        前提：本向量检索依赖"content_vector 均来自经归一化的 embedding 服务"
        （/embed 固定 normalize_embeddings=True），因此内积 `<#>` 与余弦等价且更快
        （索引 opclass 为 vector_ip_ops，与知识库 models.py 对齐）。若存在非归一化入向量
        （手工/历史导入），需先归一化或回退 cosine ops。

        边界：
            - 无向量节点（content_vector IS NULL）被自动排除（max_inner_product 对 NULL 返回 NULL，
              ORDER BY NULLS LAST 排在最后，LIMIT 截断后不出现）
            - 无匹配返回空列表
        """
        # 指令感知：检索查询使用模型内置 "query" 指令，将查询摆入与文档对齐的子空间，
        # 提升召回/精度（官方称通常 +1~5%）。文档落库向量保持默认（无指令），
        # 二者配套使用，不可对文档侧加 query 指令，否则空间错配。
        query_vector = await self._embedding_client.embed_one(query, prompt_name="query")
        return await self._run_query(session, query_vector, top_k, filters)

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
        return await self._run_query(session, query_vector, top_k, filters)

    async def _run_query(
        self,
        session: AsyncSession,
        query_vector: list[float],
        top_k: int,
        filters: FilterSpec | None,
    ) -> list[SearchResult]:
        """共享检索执行：构造语句 → 执行 → 构造 SearchResult（search 与 search_by_vector 复用）。

        max_inner_product 是 pgvector-python 提供的混合方法，对应 SQL `<#>`（内积），
        归一化向量下 <#> = -余弦。dist 为该表达式的值（0~-1→cos∈[0,1]，越大越相似）。
        ORDER BY <#> ASC 等价于余弦降序（最近在前）。
        """
        distance = KnowledgeNode.content_vector.max_inner_product(query_vector)
        stmt = (
            select(KnowledgeNode, distance.label("distance"))
            # 跳过未向量化节点（content_vector 为 NULL 时 max_inner_product 为 NULL）
            .where(KnowledgeNode.content_vector.isnot(None))
            .order_by(distance.asc())
            .limit(top_k)
        )

        # 编译 FilterSpec 为 WHERE 子句
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            stmt = stmt.where(*where_clauses)

        result = await session.execute(stmt)

        # 归一化向量下 score = -max_inner_product = 余弦 ∈[-1,1]，负值截断为 0（兼容 0~1 语义）
        search_results: list[SearchResult] = []
        for row in result:
            node: KnowledgeNode = row[0]
            dist: float = row[1]
            similarity = max(0.0, -dist)
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
