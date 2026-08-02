"""关键词全文检索：tsvector + GIN + ts_rank_cd。

对齐 PDD 3.3 全文引擎：负责"找包含 JWT 的知识"等关键词检索场景。
tsvector GIN 索引（idx_node_tsv）与 tsvector 触发器（trg_knowledge_node_tsv）
在 M3 已创建并自动维护 content_tsv，M4 实现检索调用。

技术参考（PostgreSQL 官方文档 + 网络搜索）：
- ts_rank_cd（cover density）比 ts_rank 更精确：考虑匹配词的接近程度，词聚集在一起得分更高
- websearch_to_tsquery 优于 plainto_tsquery/to_tsquery：对用户输入容错性好，支持引号短语、
  OR/NOT 操作符，不会因特殊字符报错；plainto_tsquery 不支持操作符；to_tsquery 对输入不宽容
- content_tsv @@ tsquery 是 tsvector 匹配操作符，GIN 索引自动使用
- websearch_to_tsquery('chinese', :query)：'chinese' 是 M3 已配置的 FTS 配置（zhparser 分词）
- :query 通过参数化传入防注入
- score 用 ts_rank_cd 原始值（0.x 浮点数），RRF 融合时基于 rank 而非 score，无需归一化
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import FilterSpec, compile_sqlalchemy
from mem_lake.search.fusion import SearchResult, _truncate


class FullTextSearcher:
    """关键词全文检索器。

    无构造依赖（纯 SQL 查询，不需 embedding client）。
    search 方法基于 tsvector + ts_rank_cd 的 top-k 检索，返回按相关性降序的 SearchResult 列表。
    """

    async def search(
        self,
        session: AsyncSession,
        query: str,
        top_k: int = 50,
        filters: FilterSpec | None = None,
    ) -> list[SearchResult]:
        """关键词全文检索。

        参数：
            session: 异步数据库会话
            query: 查询关键词，通过 websearch_to_tsquery 生成 tsquery
            top_k: 返回前 K 个最相关节点，默认 50
            filters: 统一过滤条件

        返回：
            按 ts_rank_cd 降序排序的 SearchResult 列表，score 字段为 ts_rank_cd 原始值，source="fulltext"。

        边界：
            - 查询无匹配返回空列表（content_tsv @@ tsquery 为 False）
            - 空查询字符串：websearch_to_tsquery('chinese', '') 返回空 tsquery，
              @@ 匹配所有行（空 tsquery 匹配任意 tsvector），但 ts_rank_cd 为 0，
              按 ts_rank_cd 降序后顺序不定，建议调用方校验 query 非空
        """
        # 1. 构造 tsquery（websearch_to_tsquery 对用户输入容错，支持引号/OR/NOT）
        tsquery = func.websearch_to_tsquery("chinese", query)
        # 2. ts_rank_cd 计算相关性（cover density，考虑词聚集程度）
        rank = func.ts_rank_cd(KnowledgeNode.content_tsv, tsquery)

        # 3. 构造查询：@@ 匹配 + 按 rank 降序 + LIMIT
        stmt = (
            select(KnowledgeNode, rank.label("rank"))
            .where(KnowledgeNode.content_tsv.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(top_k)
        )

        # 4. 编译 FilterSpec 为 WHERE 子句
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            stmt = stmt.where(*where_clauses)

        # 5. 执行查询
        result = await session.execute(stmt)

        # 6. 构造 SearchResult 列表
        search_results: list[SearchResult] = []
        for row in result:
            node: KnowledgeNode = row[0]
            rank_value: float = row[1]
            search_results.append(
                SearchResult(
                    node_id=node.id,
                    title=node.title,
                    content=_truncate(node.content),
                    node_type=node.type,
                    score=float(rank_value),
                    source="fulltext",
                    properties=node.properties or {},
                    tags=node.tags or [],
                )
            )

        return search_results
