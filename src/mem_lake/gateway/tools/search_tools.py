"""检索类工具：基于三引擎融合（向量/全文/图）的智能检索（只读）。

工具职责：转发 search 模块的三引擎融合检索与 graph.impact_analysis，
不在工具层写业务逻辑。所有工具为只读（READ_TOOL_ANNOTATIONS）。

包含工具（PDD 6.1）：
- search_similar_requirements（PM/Dev）：向量+全文融合检索相似需求
- search_code_snippets（Dev）：向量+全文融合检索研发资产（CodeSnippet/Pitfall/Solution/DesignIntent）
- analyze_impact_scope（PM/Dev）：图检索分析变更影响范围（Requirement→Code→Solution→Intent）
- check_requirement_conflicts（PM）：向量检索检测需求冲突（同项目同类型高相似度）
- list_knowledge（Admin）：分页列出项目知识节点（不走融合检索）

设计要点：
- 角色 RBAC 由中间件层控制，本文件不区分角色
- 三引擎融合检索委托给 search/fusion.py（RRF 算法）
- search_similar_requirements 与 search_code_snippets 共用 _run_hybrid_search 辅助函数，
  仅 FilterSpec.node_types 不同（Requirement / CodeSnippet）
- check_requirement_conflicts 复用 search_similar_requirements 的检索逻辑，
  额外做相似度阈值过滤（score >= threshold）与自身排除（exclude_node_id）
- list_knowledge 直接调用 repository.list_nodes_by_project，不走融合检索
- 全部使用 get_readonly_session，无需事务控制
"""

import logging
import uuid
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import BaseModel, Field

from mem_lake.config import get_settings
from mem_lake.gateway.dependencies import (
    get_readonly_session,
    validate_project_access,
    validate_system_access,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    to_tool_error,
)
from mem_lake.knowledge.repository import list_nodes_by_project
from mem_lake.knowledge.schema import SchemaValidationError
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import SearchResult, hybrid_search
from mem_lake.search.tag_expansion import expand_tags_for_project

logger = logging.getLogger("mem_lake.gateway.tools.search")


# ============================================================================
# 输出模型
# ============================================================================


class SearchItemOutput(BaseModel):
    """单个检索结果项。"""

    node_id: uuid.UUID = Field(description="节点 ID")
    title: str = Field(description="节点标题")
    content: str = Field(description="节点摘要（前 200 字符）")
    node_type: str = Field(description="节点类型")
    score: float | None = Field(description="分数(fused=向量余弦分 0~1/vector=cosine/fulltext=ts_rank/graph=None)")
    vector_score: float | None = Field(
        default=None,
        description="向量余弦相似度（0~1）；fused 结果附带其原始向量分，便于判相关性",
    )
    source: str = Field(
        description="来源引擎：vector/fulltext/graph/fused"
    )
    properties: dict[str, Any] = Field(default={}, description="节点属性")
    tags: list[str] = Field(default=[], description="标签数组")


class HybridSearchOutput(BaseModel):
    """search_similar_requirements / search_code_snippets 出参。"""

    query: str = Field(description="原始查询文本")
    fused: list[SearchItemOutput] = Field(
        description="RRF 融合结果（向量+全文），按分数降序"
    )
    vector: list[SearchItemOutput] = Field(
        description="向量引擎原始结果", default=[]
    )
    fulltext: list[SearchItemOutput] = Field(
        description="全文引擎原始结果", default=[]
    )
    total: int = Field(description="融合结果数量")


class ImpactScopeOutput(BaseModel):
    """analyze_impact_scope 出参。"""

    requirement_id: uuid.UUID = Field(description="需求节点 ID")
    requirement: dict[str, Any] | None = Field(
        default=None, description="需求节点详情（None 表示不存在）"
    )
    codes: list[dict[str, Any]] = Field(
        default=[], description="直接实现该需求的代码节点列表"
    )
    dependencies: list[dict[str, Any]] = Field(
        default=[], description="代码依赖链节点列表（去重）"
    )
    solutions: list[dict[str, Any]] = Field(
        default=[], description="代码对应的实现方案节点列表"
    )
    design_intents: list[dict[str, Any]] = Field(
        default=[], description="方案体现的设计意图节点列表"
    )


class ConflictCheckOutput(BaseModel):
    """check_requirement_conflicts 出参。"""

    requirement_id: uuid.UUID = Field(description="被检测的需求节点 ID")
    has_conflict: bool = Field(description="是否检测到冲突")
    conflicts: list[SearchItemOutput] = Field(
        default=[], description="冲突节点列表（相似度 >= 阈值）"
    )
    threshold: float = Field(description="相似度阈值")
    suggestion: str | None = Field(
        default=None, description="建议动作：review/manual_merge/None"
    )


class KnowledgeNodeOutput(BaseModel):
    """知识节点列表项。"""

    node_id: uuid.UUID = Field(description="节点 ID")
    type: str = Field(description="节点类型")
    title: str = Field(description="节点标题")
    status: str = Field(description="节点状态")
    version: int = Field(description="版本号")
    created_at: Any = Field(description="创建时间（ISO 8601）")
    created_by: str = Field(description="创建者")
    tags: list[str] = Field(default=[], description="标签数组")


class ListKnowledgeOutput(BaseModel):
    """list_knowledge 出参。"""

    project_id: uuid.UUID = Field(description="项目 ID")
    nodes: list[KnowledgeNodeOutput] = Field(description="节点列表")
    total: int = Field(description="返回数量（非总数）")
    limit: int = Field(description="当前分页上限")
    offset: int = Field(description="当前分页偏移")


# ============================================================================
# 工具注册
# ============================================================================


def register_search_tools(mcp: FastMCP) -> None:
    """注册检索类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def search_similar_requirements(
        query: str = Field(description="查询文本（需求描述/关键词）"),
        system_id: uuid.UUID | None = Field(
            default=None, description="归属 system 域（可选；有值则检索该系统全部需求含悬浮）"
        ),
        project_id: uuid.UUID | None = Field(
            default=None, description="归属项目 ID（与 system_id 至少其一必填）"
        ),
        top_n: int = Field(default=10, description="融合后返回数量上限"),
        tags: list[str] | None = Field(
            default=None, description="标签过滤（tags_op 控制 AND/OR）"
        ),
        tags_op: str = Field(
            default="all",
            description="标签匹配语义：all=AND（默认，需包含所有标签），any=OR（命中任一标签）",
        ),
        min_score: float | None = Field(
            default=0.5,
            description="向量余弦相似度下限（0~1）；低于此值的结果被过滤；传 None 可关闭默认阈值（返回相对 top_n）",
        ),
        semantic_tags: bool = Field(
            default=False,
            description="标签语义扩展：开启后用 embedding 将给定标签扩展为项目中语义相近的标签"
            "（如「性能」≈「N+1」），放宽精确匹配；关闭时仅做精确 AND/OR 匹配",
        ),
    ) -> HybridSearchOutput:
        """向量+全文融合检索相似需求（Requirement 类型；按 system 或 project 隔离）。

        PM/Dev 工具。三引擎并行：向量（pgvector cosine）+ 全文（tsvector chinese 分词），
        RRF 融合后返回 top_n 结果。仅检索 approved 状态节点。
        system 维度：传 system_id 检索该系统全部需求（含悬浮，project 可空）；dev 可用
        system_id 定位可见 system 的需求 UUID，再引用实现建边。
        注意：默认 min_score=0.5 会滤除弱相关噪声（返回绝对更相关的结果）；
        无向量分的全文命中结果不被该阈值过滤（保留关键词精确匹配）；
        fused 中仅全文命中的节点 score 为 RRF 小数量纲，min_score 对其不生效。
        fused 结果的 score 已透出向量余弦分（0~1），可据此判相关性。
        tags 默认精确匹配（AND/OR 由 tags_op 控制）；如需语义相近召回，设 semantic_tags=true。
        query 不能为空（空查询下全文引擎无排序依据）。
        """
        try:
            if project_id is None and system_id is None:
                raise ValueError("project_id 与 system_id 至少提供一个")
            if project_id is not None:
                validate_project_access(project_id)
            if system_id is not None:
                validate_system_access(system_id)
            _validate_query(query)
            return await _run_hybrid_search(
                project_id=project_id,
                system_id=system_id,
                query=query,
                node_types=("Requirement",),
                top_n=top_n,
                tags=tuple(tags) if tags else None,
                tags_op=tags_op,
                min_score=min_score,
                semantic_tags=semantic_tags,
            )
        except (SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def search_code_snippets(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        query: str = Field(description="查询文本（代码功能/关键词）"),
        top_n: int = Field(default=10, description="融合后返回数量上限"),
        tags: list[str] | None = Field(
            default=None, description="标签过滤（tags_op 控制 AND/OR）"
        ),
        tags_op: str = Field(
            default="all",
            description="标签匹配语义：all=AND（默认，需包含所有标签），any=OR（命中任一标签）",
        ),
        min_score: float | None = Field(
            default=0.5,
            description="向量余弦相似度下限（0~1）；低于此值的结果被过滤；传 None 可关闭默认阈值（返回相对 top_n）",
        ),
        semantic_tags: bool = Field(
            default=False,
            description="标签语义扩展：开启后用 embedding 将给定标签扩展为项目中语义相近的标签"
            "（如「性能」≈「N+1」），放宽精确匹配；关闭时仅做精确 AND/OR 匹配",
        ),
    ) -> HybridSearchOutput:
        """向量+全文融合检索研发资产（同项目内 CodeSnippet/Pitfall/Solution/DesignIntent 类型）。

        Dev 工具。三引擎并行：向量（pgvector cosine）+ 全文（tsvector chinese 分词），
        RRF 融合后返回 top_n 结果。仅检索 approved 状态节点。
        除代码片段外，踩坑(Pitfall)/方案(Solution)/设计意图(DesignIntent) 也会一并召回，
        便于「踩过的坑」「采用的方案」等经验类检索。返回项的 node_type 区分具体类型。
        注意：默认 min_score=0.5 会滤除弱相关噪声（返回绝对更相关的结果）；
        无向量分的全文命中结果不被该阈值过滤（保留关键词精确匹配）；
        fused 中仅全文命中的节点 score 为 RRF 小数量纲，min_score 对其不生效。
        fused 结果的 score 已透出向量余弦分（0~1），可据此判相关性。
        tags 默认精确匹配（AND/OR 由 tags_op 控制）；如需语义相近召回，设 semantic_tags=true。
        query 不能为空（空查询下全文引擎无排序依据）。
        """
        try:
            validate_project_access(project_id)
            _validate_query(query)
            return await _run_hybrid_search(
                project_id=project_id,
                query=query,
                node_types=("CodeSnippet", "Pitfall", "Solution", "DesignIntent"),
                top_n=top_n,
                tags=tuple(tags) if tags else None,
                tags_op=tags_op,
                min_score=min_score,
                semantic_tags=semantic_tags,
            )
        except (SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def analyze_impact_scope(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        requirement_id: uuid.UUID = Field(description="需求节点 ID"),
        max_depth: int = Field(
            default=5, description="depends_on 依赖链遍历深度"
        ),
    ) -> ImpactScopeOutput:
        """图检索分析变更影响范围（需求→代码→方案→设计意图）。

        PM/Dev 工具。从需求出发遍历：
        Requirement --implements--> CodeSnippet --depends_on--> CodeSnippet
        CodeSnippet --realized_by--> Solution --embodies--> DesignIntent
        返回需求节点、直接实现代码、依赖链、方案、设计意图的完整影响范围。
        """
        try:
            validate_project_access(project_id)
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context

            session = await get_readonly_session()
            try:
                graph_searcher = _get_graph_searcher(lifespan_ctx)
                result = await graph_searcher.impact_analysis(
                    session,
                    requirement_id=requirement_id,
                    max_depth=max_depth,
                )
                return ImpactScopeOutput(
                    requirement_id=requirement_id,
                    requirement=result.get("requirement"),
                    codes=result.get("codes", []),
                    dependencies=result.get("dependencies", []),
                    solutions=result.get("solutions", []),
                    design_intents=result.get("design_intents", []),
                )
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def check_requirement_conflicts(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        requirement_id: uuid.UUID = Field(
            description="被检测的需求节点 ID（自动排除自身）"
        ),
        threshold: float | None = Field(
            default=None,
            description="相似度阈值（0~1），仅返回 score >= threshold 的结果；"
            "None 时用配置 CONFLICT_SIMILARITY_THRESHOLD（默认 0.85）",
        ),
        top_n: int = Field(
            default=20, description="检索召回数量上限（融合后）"
        ),
    ) -> ConflictCheckOutput:
        """向量检索检测需求冲突（同项目同类型高相似度节点）。

        PM 工具。基于向量相似度检测与指定需求冲突的潜在重复/矛盾需求。
        自动排除自身节点，仅返回 score >= threshold 的结果。threshold 缺省读
        配置 CONFLICT_SIMILARITY_THRESHOLD（与审批流冲突检测同一阈值来源，
        避免双源脱钩；AUDIT §2.10）。本工具为纯向量相似度过滤，不含审批层
        detect_conflicts 的 L2 关键属性比对——二者定位不同（前者给 PM 主动
        排查，后者是审批质量门禁）。
        has_conflict=true 时 suggestion 推荐 review（人工核查）或 manual_merge（高相似度合并）。
        """
        try:
            validate_project_access(project_id)
            if threshold is None:
                threshold = get_settings().CONFLICT_SIMILARITY_THRESHOLD
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context

            # 获取被检测需求的标题用作查询文本（build_embed_text 含属性段，
            # 与落库向量构造一致）
            session = await get_readonly_session()
            try:
                from mem_lake.knowledge.embed import build_embed_text
                from mem_lake.knowledge.repository import get_node
                target_node = await get_node(session, requirement_id)
            finally:
                await session.close()
            query_text = build_embed_text(
                target_node.type,
                target_node.title,
                target_node.content,
                target_node.properties,
            )

            # 复用 hybrid_search 检索同项目 Requirement 节点（内部自建独立 session）
            filters = FilterSpec(
                project_id=project_id,
                node_types=("Requirement",),
            )
            result = await hybrid_search(
                query=query_text,
                embedding_client=lifespan_ctx.embedding_client,
                graph_store=lifespan_ctx.graph_store,
                top_n=top_n,
                filters=filters,
            )

            # 过滤：排除自身 + score >= threshold
            conflicts = [
                _to_search_item_output(r)
                for r in result.get("fused", [])
                if r.node_id != requirement_id
                and r.score is not None
                and r.score >= threshold
            ]

            has_conflict = len(conflicts) > 0
            suggestion = None
            if has_conflict:
                # 最高相似度 >= 0.95 推荐 manual_merge，否则 review
                # （conflicts 已在上面过滤 score is not None）
                max_score = max(c.score for c in conflicts)
                suggestion = "manual_merge" if max_score >= 0.95 else "review"

            return ConflictCheckOutput(
                requirement_id=requirement_id,
                has_conflict=has_conflict,
                conflicts=conflicts,
                threshold=threshold,
                suggestion=suggestion,
            )
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def list_knowledge(
        project_id: uuid.UUID = Field(description="项目 ID"),
        node_type: str | None = Field(
            default=None, description="节点类型过滤（None 表示所有类型）"
        ),
        status: str | None = Field(
            default="approved",
            description="状态过滤：approved/archived/None（None 表示所有状态）",
        ),
        limit: int = Field(default=100, description="返回数量上限"),
        offset: int = Field(default=0, description="分页偏移"),
    ) -> ListKnowledgeOutput:
        """分页列出项目知识节点（不走融合检索，直接按时间倒序）。

        Admin 工具。用于查看项目下所有节点（含已归档）。
        status="approved"（默认）仅返回已审批节点，"archived" 仅返回已归档，
        None 返回所有状态。
        """
        try:
            validate_project_access(project_id)
            session = await get_readonly_session()
            try:
                nodes = await list_nodes_by_project(
                    session,
                    project_id=project_id,
                    node_type=node_type,
                    status=status,
                    limit=limit,
                    offset=offset,
                )
                return ListKnowledgeOutput(
                    project_id=project_id,
                    nodes=[_to_knowledge_node_output(n) for n in nodes],
                    total=len(nodes),
                    limit=limit,
                    offset=offset,
                )
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e)


# ============================================================================
# 辅助函数
# ============================================================================


async def _run_hybrid_search(
    *,
    project_id: uuid.UUID | None = None,
    system_id: uuid.UUID | None = None,
    query: str,
    node_types: tuple[str, ...],
    top_n: int,
    tags: tuple[str, ...] | None,
    tags_op: str = "all",
    min_score: float | None = None,
    semantic_tags: bool = False,
) -> HybridSearchOutput:
    """执行三引擎融合检索的共享辅助函数。

    search_similar_requirements 与 search_code_snippets 共用此函数，
    仅 node_types 参数不同。min_score 按向量余弦分过滤融合结果。
    semantic_tags=true 时，先用 embedding 将 tags 扩展为项目内语义相近标签再过滤。

    system 维度：传 project_id 检索该 project 资产；传 system_id 检索该系统全部需求
    （含悬浮需求）。二者都不传时用 project_scope 内全部 project 检索。
    """
    ctx = get_context()
    lifespan_ctx = ctx.lifespan_context

    effective_tags = tags
    if semantic_tags and tags and project_id is not None:
        # 标签语义扩展：拉取项目标签词表 + 向量扩展；embedding 异常时降级为精确匹配
        session = await get_readonly_session()
        try:
            expanded = await expand_tags_for_project(
                lifespan_ctx.embedding_client,
                session,
                project_id=project_id,
                tags=list(tags),
                node_type=node_types[0] if len(node_types) == 1 else None,
                threshold=0.7,
            )
            effective_tags = tuple(expanded)
        except Exception as e:  # noqa: BLE001 - 降级而非让检索整体失败
            logger.warning("semantic tag expansion failed, fall back to exact tags: %s", e)
        finally:
            await session.close()

    filters = FilterSpec(
        project_id=project_id,
        system_id=system_id,
        node_types=node_types,
        tags=effective_tags,
        tags_op=tags_op,
    )

    # hybrid_search 内部为每引擎自建独立 session（AsyncSession 非并发安全）
    result = await hybrid_search(
        query=query,
        embedding_client=lifespan_ctx.embedding_client,
        graph_store=lifespan_ctx.graph_store,
        top_n=top_n,
        filters=filters,
    )

    # 向量 cosine 分映射，用于 min_score 过滤与 fused 结果附带 vector_score
    vector_score_map = {
        r.node_id: r.score for r in result.get("vector", []) if r.score is not None
    }

    fused_raw = result.get("fused", [])
    if min_score is not None:
        # 仅对"有向量分"的节点按 min_score 过滤；无向量分的全文命中节点予以保留
        fused_raw = [
            r
            for r in fused_raw
            if r.node_id not in vector_score_map
            or vector_score_map.get(r.node_id, -1) >= min_score
        ]

    return HybridSearchOutput(
        query=query,
        fused=[_to_search_item_output(r, vector_score_map.get(r.node_id)) for r in fused_raw],
        vector=[_to_search_item_output(r) for r in result.get("vector", [])],
        fulltext=[_to_search_item_output(r) for r in result.get("fulltext", [])],
        total=len(fused_raw),
    )


def _to_search_item_output(
    result: SearchResult, vector_score: float | None = None
) -> SearchItemOutput:
    """从 SearchResult 构造 SearchItemOutput。vector_score 用于 fused 结果附带余弦分。"""
    return SearchItemOutput(
        node_id=result.node_id,
        title=result.title,
        content=result.content,
        node_type=result.node_type,
        score=result.score,
        vector_score=vector_score,
        source=result.source,
        properties=result.properties,
        tags=result.tags,
    )


def _to_knowledge_node_output(node) -> KnowledgeNodeOutput:
    """从 KnowledgeNode ORM 对象构造 KnowledgeNodeOutput。"""
    return KnowledgeNodeOutput(
        node_id=node.id,
        type=node.type,
        title=node.title,
        status=node.status,
        version=node.version,
        created_at=node.created_at.isoformat() if node.created_at else None,
        created_by=node.created_by,
        tags=node.tags or [],
    )


def _get_graph_searcher(lifespan_ctx):
    """从 lifespan 上下文获取 GraphSearcher 实例。

    lifespan_ctx 已注入 graph_store，构造 GraphSearcher 包装。
    """
    from mem_lake.search.graph import GraphSearcher
    return GraphSearcher(lifespan_ctx.graph_store)


def _validate_query(query: str) -> None:
    """校验检索 query 非空（空查询全文引擎无排序依据，返回任意顺序）。"""
    if not query or not query.strip():
        from mem_lake.approval.service import PayloadValidationError

        raise PayloadValidationError("query 不能为空")
