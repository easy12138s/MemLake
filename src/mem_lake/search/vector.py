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

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.models import KnowledgeNode, NodeEmbedding
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

        多向量（D）检索：节点在 node_embedding 表有多个 facet 向量，按 node_id 聚合取
        各 facet 与查询向量的最大余弦（maxsim，ColBERT 式），避免单向量语义稀释。
        - NodeEmbedding.content_vector.max_inner_product(query_vector) 对应 SQL `<#>`（内积），
          归一化向量下 <#> = -余弦。distance = <#>，similarity = -distance = 余弦 ∈[0,1]。
        - 子查询：对通过过滤的节点，按 node_id 取 max(-distance) 作为该节点相似度，降序取 top_k。
        - 外层再 JOIN knowledge_node 取展示字段（title/content/type/properties/tags）。
        - 无向量节点（content_vector IS NULL）自动排除（max 对 NULL 忽略，LIMIT 截断后不出现）。
        """
        distance = NodeEmbedding.content_vector.max_inner_product(query_vector)
        sim = -distance  # 余弦相似度（归一化向量），越大越相似

        # 子查询：过滤 + 按节点聚合 max 相似度 + 取 top_k
        # 注意：is_deleted / status 过滤与历史行为一致，由传入的 FilterSpec 提供
        # （默认 status="approved"、exclude_deleted=True），此处不硬编码，避免覆盖调用方意图。
        sub = (
            select(NodeEmbedding.node_id, func.max(sim).label("sim"))
            .join(KnowledgeNode, KnowledgeNode.id == NodeEmbedding.node_id)
            .where(NodeEmbedding.content_vector.isnot(None))
        )
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            sub = sub.where(*where_clauses)
        sub = sub.group_by(NodeEmbedding.node_id).order_by(text("sim DESC")).limit(top_k)
        subq = sub.subquery()

        # 外层：取节点展示字段（保留子查询的相似度降序，避免 join 重排）
        stmt = (
            select(KnowledgeNode, subq.c.sim)
            .join(subq, subq.c.node_id == KnowledgeNode.id)
            .order_by(text("sim DESC"))
        )

        result = await session.execute(stmt)

        # 余弦 ∈[-1,1]，负值截断为 0（兼容 0~1 语义）
        search_results: list[SearchResult] = []
        for row in result:
            node: KnowledgeNode = row[0]
            similarity: float = max(0.0, float(row[1]))
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
