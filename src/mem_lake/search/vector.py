"""向量语义检索：pgvector HNSW + 归一化内积（max_inner_product / <#>）。

对齐 PDD 3.3 向量引擎：负责"找描述用户登录的需求"等语义检索场景。
pgvector HNSW 索引（node_embedding.idx_node_embedding_vector，vector_ip_ops，
m=32/ef_construction=400）由 create_all 创建；M4 实现检索调用。向量检索只查
node_embedding + knowledge_node 关联表，不涉及 AGE 图。

技术参考（网络搜索 pgvector-python 官方文档）：
- facet 向量均来自归一化 embedding 服务（/embed normalize_embeddings=True），
  因此内积 `<#>` 与余弦等价。NodeEmbedding.content_vector.max_inner_product(query_vector)
  是 pgvector-python 提供的 SQLAlchemy 混合方法，等价于 SQL `content_vector <#> :vector`
  （注意：pgvector-python 的 `<#>` 对应方法名为 max_inner_product，无 inner_product）
- HNSW 索引自动用于 ORDER BY <#> 查询（vector_ip_ops 操作符类）；<#> 返回负内积，
  归一化下 score = -max_inner_product = 余弦 ∈[-1,1]，负值截断为 0
- 查询向量通过参数化传入，非字符串拼接，零注入风险
- knowledge_node.content_vector 列已废弃（0003 迁移 DROP），检索主路径
  走 node_embedding 多向量（facet max-pooling），单主向量不再落库

行扫形态（性能关键，勿回退为「聚合前置」）：
pgvector HNSW 索引只加速「ORDER BY 距离 + LIMIT」的行扫形态。若把
group_by 聚合或 knowledge_node 过滤前置于行扫，planner 退化为全表
Seq Scan + 排序（19748 行 ~0.8-1.1s，冷 cache 更差，且随数据量线性劣化）。
因此 _run_query 采用「count 穷尽预言 → HNSW 行扫 → Python 端 max-pooling
→ 后置过滤」，过滤饥饿（初始窗口大多落在过滤目标外）时按分诊加档，
两档探针不收敛时切换聚合前置的精确查询兜底（_exact_results）。

索引可用性依赖两个前置条件（运维口径）：
1. 行扫前须按窗口设置 hnsw.ef_search ≥ 窗口（_ensure_ef_search）——
   碎片化索引下 pgvector 默认 40 实测只探到入口连通子图（窗口 80 仅回
   0-11 行，回行数 < 窗口）；健康索引下为防御性保障；
2. HNSW 随增量插入会碎片化（入口连通子图收窄），需定期
   REINDEX INDEX idx_node_embedding_vector 重建（碎片化不损伤正确性——
   穷尽判据用 count 而非索引行数，最坏走聚合兜底，仅多耗时；探针路径
   占比经 logger.debug("vector_search_probe") 观测，骤降即该重建）。
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.models import KnowledgeNode, NodeEmbedding
from mem_lake.observability.metrics import VECTOR_PROBE_PATH
from mem_lake.search.filters import FilterSpec, compile_sqlalchemy
from mem_lake.search.fusion import SearchResult, _truncate

logger = logging.getLogger("mem_lake.search.vector")

# 行扫窗口倍数：节点平均 4-5 个 facet（content + 非空属性 facet），同节点各 facet
# 的距离排名聚簇（同内容主题），16×top_k 窗口可覆盖 max-pooling 所需的节点 facet 集。
_ROW_SCAN_FACTOR = 16
# 初始窗口硬顶（ef_search ≤ 800 是索引扫的廉价区：实测 window=800 ≈ 45ms、
# window=200 ≈ 3ms）；超出部分交给分诊/精确兜底，避免大 top_k 白付深探针。
_ROW_SCAN_CAP = 200
# 扩窗阶梯倍数（首档有信号但未足时加档）
_ROW_SCAN_STEP = 4
# hnsw.ef_search 合法范围上限（pgvector 0.8.6：1..1000）；超出时 planner 会自行
# 改选 Sort 路径（同样正确），此处仅保证参数不越界
_EF_SEARCH_MAX = 1000


def build_hits_statement(query_vector: list[float], row_limit: int) -> Select:
    """构造 HNSW 行扫语句：node_embedding 按内积距离升序取前 row_limit 行。

    返回仅含 (node_id, dist) 的 Select——**不做聚合、不过滤、不 join**
    （索引可用性的形态前提，见模块 docstring）。max-pooling 聚合与 FilterSpec
    过滤由调用方在行扫之后进行；收敛判定（候选已足 len(nodes) >= top_k /
    行扫到表尾 len(rows) >= scan_total）也依赖本语句的纯行扫形态。

    调用方必须先执行 ``SET LOCAL hnsw.ef_search = row_limit``（见
    _ensure_ef_search）：碎片化索引下 pgvector 默认 ef_search=40 小于
    窗口规模时索引扫实测只回碎片子图内的行（窗口内行丢失）。
    """
    distance = NodeEmbedding.content_vector.max_inner_product(query_vector)
    return (
        select(NodeEmbedding.node_id, distance.label("dist"))
        .where(NodeEmbedding.content_vector.isnot(None))
        .order_by(distance)
        .limit(row_limit)
    )


async def _ensure_ef_search(session: AsyncSession, ef_search: int) -> None:
    """行扫前置 GUC（事务级，当前事务内生效）：按窗口规模设置 hnsw.ef_search。

    pgvector 要求 ef_search ≥ LIMIT。碎片化索引（HNSW 长期增量插入后入口
    连通子图收窄）下实测默认 40 时窗口 80 仅回 0-11 行——索引可达行数
    不可作为穷尽判据；按窗口显式设置后索引扫才可用。健康索引下本设置为
    防御性保障（REINDEX 后实测默认 40 亦可满窗）。
    窗口足够大时 planner 自行改选 Sort（同样正确，仅慢）。
    SET 语句不支持绑定参数，值由受控整数内联（无注入面）；ef_search 合法
    上限 1000（pgvector 0.8.6），超出钳制——该规模下 planner 本就倾向 Sort。
    """
    await session.execute(
        text(f"SET LOCAL hnsw.ef_search = {min(int(ef_search), _EF_SEARCH_MAX)}")
    )


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

        前提：本向量检索依赖"node_embedding 的 facet 向量均来自经归一化的 embedding
        服务"（/embed 固定 normalize_embeddings=True），因此内积 `<#>` 与余弦等价且更快
        （索引 opclass 为 vector_ip_ops，与知识库 models.py 对齐）。若存在非归一化入向量
        （手工/历史导入），需先归一化或回退 cosine ops。

        边界：
             - 无 facet 向量节点（node_embedding.content_vector IS NULL）被自动排除
               （行扫与聚合两条路径均有显式 content_vector IS NOT NULL 谓词）
             - 无匹配返回空列表
             - top_k < 0 抛 ValueError（对齐聚合路径 limit 负值的报错语义）
        """
        # 指令感知：检索查询使用模型内置 "query" 指令，将查询摆入与文档对齐的子空间，
        # 提升召回/精度（官方称通常 +1~5%）。文档落库向量保持默认（无指令），
        # 二者配套使用，不可对文档侧加 query 指令，否则空间错配。
        # 负值在 embed 前即拒（避免白耗一次 embedding 服务调用；_run_query 内
        # 另有同名校验兜底直接调用路径）
        if top_k < 0:
            raise ValueError(f"top_k 必须 >= 0: {top_k}")
        if top_k == 0:
            return []
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
        """共享检索执行：count 穷尽预言 → HNSW 行扫 → Python max-pooling → 后置过滤
        （search / search_by_vector 复用）；行扫路径饥饿时切换聚合前置的精确查询。

        多向量（D）检索语义：节点在 node_embedding 表有多个 facet 向量，按 node_id
        取各 facet 的最大余弦（maxsim，ColBERT 式）。
        - NodeEmbedding.content_vector.max_inner_product(query_vector) 对应 SQL `<#>`
          （归一化向量下 <#> = -余弦）。distance = <#>，similarity = -distance ∈[0,1]。
        - 行扫形态保证 HNSW 索引可用（见 build_hits_statement）；聚合/过滤后置。

        行扫路径是**窗口内近似**：仅对落入探针窗口的 facet 做 max-pooling，
        若某节点的全部 facet 均在窗外则不会出现；若其最优 facet 在窗外，
        该节点 pooled score ≤ 真实 maxsim（差值为窗内外 facet 差）。因此
        「过滤后候选 ≥ top_k」收敛出的排序/分数与精确 maxsim 可能有界偏差；
        对相似度阈值敏感的场景（如冲突检测 0.85）由调用方知悉该近似界。
        精确路径（_exact_results）与聚合前置的旧实现语义完全一致。

        探针策略（有界两档 + 分诊，成本受控——ef_search 深探针超线性，
        实测 800 窗 ≈ 45ms，超过廉价区的收益不如直接精确兜底）：
        1. 首档 W0 = min(top_k × _ROW_SCAN_FACTOR, _ROW_SCAN_CAP)；
           候选已足（过滤后 ≥ top_k）或行扫到表尾（len(rows) ≥ count 口径的
           scan_total，索引健康或小表）→ 行扫路径收敛返回；
        2. 未收敛分诊：窗口已覆盖全表仍不足（索引截断无法枚举）或首档
           **零信号**（过滤域在查询邻域内稀疏，加深全局窗口边际收益趋零）
           → 直接精确兜底，免无效加档；
        3. 否则加一档 W1 = W0 × _ROW_SCAN_STEP；再不收敛 → 精确兜底。
        """
        if top_k < 0:
            raise ValueError(f"top_k 必须 >= 0: {top_k}")
        if top_k == 0:
            return []
        # 穷尽预言机：非空向量行数（不依赖索引完整性——HNSW 增量插入会碎片化，
        # 索引可达行数不可作为「已扫全」判据；count 才是表尾的可靠信号）
        scan_total = await session.scalar(
            select(func.count())
            .select_from(NodeEmbedding)
            .where(NodeEmbedding.content_vector.isnot(None))
        )
        if scan_total == 0:
            return []

        # 首档探针窗口：W0 覆盖常规候选（廉价区），加档仍压在廉价区内
        row_limit = min(max(1, top_k) * _ROW_SCAN_FACTOR, _ROW_SCAN_CAP, scan_total)
        for probe in (0, 1):
            rows, best, nodes = await self._probe(
                session, query_vector, row_limit, filters, top_k
            )
            # 收敛：候选已足 / 行扫到表尾（count 口径，索引或小表均已全枚举）
            if len(nodes) >= top_k or len(rows) >= scan_total:
                VECTOR_PROBE_PATH.labels(path="window").inc()
                # 余弦 ∈[-1,1]，负值截断为 0（兼容 0~1 语义）；降序取 top_k
                return self._to_search_results(
                    sorted(nodes, key=lambda n: best[n.id], reverse=True)[:top_k], best
                )
            if probe == 0:
                # 分诊：窗口已覆盖全表仍不足（索引截断，加档无意义）或
                # 首档零信号（过滤域稀疏，全局加深的边际收益趋零）→ 精确兜底
                if row_limit >= scan_total or not nodes:
                    break
                row_limit = min(row_limit * _ROW_SCAN_STEP, scan_total)

        # 探针未收敛：过滤域稀疏或索引截断，切聚合前置的精确查询兜底
        return await self._exact_results(session, query_vector, top_k, filters)

    async def _probe(
        self,
        session: AsyncSession,
        query_vector: list[float],
        row_limit: int,
        filters: FilterSpec | None,
        top_k: int,
    ) -> tuple[list[Any], dict[UUID, float], list[KnowledgeNode]]:
        """单档行扫探针：ef_search 前置 → HNSW 行扫 → max-pooling → 后置过滤。

        返回 (原始行, node_id→max sim, 过滤后节点)。path=window/exact 观测
        日志供运维判断索引健康（fast path 占比骤降 = 该 REINDEX 了）。
        """
        # HNSW 索引扫前置：ef_search ≥ 窗口规模（见 _ensure_ef_search）
        await _ensure_ef_search(session, row_limit)
        rows = (
            (await session.execute(build_hits_statement(query_vector, row_limit)))
            .all()
        )
        # max-pooling：同一节点多 facet 取最大相似度
        best: dict[UUID, float] = {}
        for node_id, dist in rows:
            sim = -float(dist)
            if sim > best.get(node_id, float("-inf")):
                best[node_id] = sim

        # 后置过滤：命中节点经 FilterSpec 过滤（compile_sqlalchemy 条件，
        # 口径与聚合前置时代完全一致）
        nodes: list[KnowledgeNode] = []
        if best:
            stmt = select(KnowledgeNode).where(
                KnowledgeNode.id.in_(best.keys()),
                *compile_sqlalchemy(filters),
            )
            nodes = list((await session.execute(stmt)).scalars().all())

        logger.debug(
            "vector_search_probe path=window row_limit=%d rows=%d distinct_nodes=%d "
            "filtered_nodes=%d top_k=%d",
            row_limit,
            len(rows),
            len(best),
            len(nodes),
            top_k,
        )
        return rows, best, nodes

    async def _exact_results(
        self,
        session: AsyncSession,
        query_vector: list[float],
        top_k: int,
        filters: FilterSpec | None,
    ) -> list[SearchResult]:
        """聚合前置的精确兜底查询（原单语句形态，行扫探针未收敛时使用）。

        语义：在过滤域内按 node_id 聚合 max 余弦（maxsim）后取 top_k——与
        行扫路径的差别仅在「过滤先于聚合」（全量精确）而非「过滤后置」
        （窗口近似）。planner 对该形态选全表排序（索引被聚合形态抵消），
        因此不受 HNSW 碎片化影响；代价与聚合前置的旧实现同量级。
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
        rows = result.all()
        nodes = [row[0] for row in rows]
        sims = {row[0].id: float(row[1]) for row in rows}
        logger.debug(
            "vector_search_probe path=exact（探针未收敛，聚合兜底）rows=%d top_k=%d",
            len(rows),
            top_k,
        )
        return self._to_search_results(nodes, sims)

    def _to_search_results(
        self, nodes: list[KnowledgeNode], sim_by_id: dict[UUID, float]
    ) -> list[SearchResult]:
        """节点 + 相似度 → SearchResult 列表（行扫路径与精确兜底共用）。

        余弦 ∈[-1,1]，负值截断为 0（兼容 0~1 语义）；nodes 需已按相似度
        降序、且长度 ≤ top_k（调用方负责排序截断）。
        """
        search_results: list[SearchResult] = []
        for node in nodes:
            similarity: float = max(0.0, sim_by_id[node.id])
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
                    # 数据信任字段
                    version=node.version,
                )
            )

        return search_results
