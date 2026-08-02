"""检索类工具：基于三引擎融合（向量/全文/图）的智能检索（只读）。

工具职责：转发 search 模块的三引擎融合检索与 graph.impact_analysis，
不在工具层写业务逻辑。所有工具为只读（READ_TOOL_ANNOTATIONS）。

包含工具（PDD 6.1）：
- search_similar_requirements（PM/Dev）：向量+全文融合检索相似需求
- search_code_snippets（Dev）：向量+全文融合检索代码片段
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

from mem_lake.gateway.dependencies import (
    get_readonly_session,
    validate_project_access,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    to_tool_error,
)
from mem_lake.knowledge.repository import list_nodes_by_project
from mem_lake.knowledge.schema import SchemaValidationError
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import SearchResult, hybrid_search

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
    score: float | None = Field(description="相似度分数（融合后为 RRF 分数，图为 None）")
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
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        query: str = Field(description="查询文本（需求描述/关键词）"),
        top_n: int = Field(default=10, description="融合后返回数量上限"),
        tags: list[str] | None = Field(
            default=None, description="标签过滤（AND 关系）"
        ),
    ) -> HybridSearchOutput:
        """向量+全文融合检索相似需求（同项目内 Requirement 类型）。

        PM/Dev 工具。三引擎并行：向量（pgvector cosine）+ 全文（tsvector chinese 分词），
        RRF 融合后返回 top_n 结果。仅检索 approved 状态节点。
        """
        try:
            validate_project_access(project_id)
            return await _run_hybrid_search(
                project_id=project_id,
                query=query,
                node_types=("Requirement",),
                top_n=top_n,
                tags=tuple(tags) if tags else None,
            )
        except (SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def search_code_snippets(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        query: str = Field(description="查询文本（代码功能/关键词）"),
        top_n: int = Field(default=10, description="融合后返回数量上限"),
        tags: list[str] | None = Field(
            default=None, description="标签过滤（AND 关系）"
        ),
    ) -> HybridSearchOutput:
        """向量+全文融合检索代码片段（同项目内 CodeSnippet 类型）。

        Dev 工具。三引擎并行：向量（pgvector cosine）+ 全文（tsvector chinese 分词），
        RRF 融合后返回 top_n 结果。仅检索 approved 状态节点。
        """
        try:
            validate_project_access(project_id)
            return await _run_hybrid_search(
                project_id=project_id,
                query=query,
                node_types=("CodeSnippet",),
                top_n=top_n,
                tags=tuple(tags) if tags else None,
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
        threshold: float = Field(
            default=0.85,
            description="相似度阈值（0~1），仅返回 score >= threshold 的结果",
        ),
        top_n: int = Field(
            default=20, description="检索召回数量上限（融合后）"
        ),
    ) -> ConflictCheckOutput:
        """向量检索检测需求冲突（同项目同类型高相似度节点）。

        PM 工具。基于向量相似度检测与指定需求冲突的潜在重复/矛盾需求。
        自动排除自身节点，仅返回 score >= threshold 的结果。
        has_conflict=true 时 suggestion 推荐 review（人工核查）或 manual_merge（高相似度合并）。
        """
        try:
            validate_project_access(project_id)
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context

            # 复用 hybrid_search 检索同项目 Requirement 节点
            session = await get_readonly_session()
            try:
                # 先获取被检测需求的标题用作查询文本
                from mem_lake.knowledge.repository import get_node
                target_node = await get_node(session, requirement_id)
                query_text = f"{target_node.title}\n{target_node.content}"

                filters = FilterSpec(
                    project_id=project_id,
                    node_types=("Requirement",),
                )
                result = await hybrid_search(
                    session,
                    query=query_text,
                    embedding_client=lifespan_ctx.embedding_client,
                    graph_store=lifespan_ctx.graph_store,
                    top_n=top_n,
                    filters=filters,
                )
            finally:
                await session.close()

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
                max_score = max(c.score for c in conflicts if c.score is not None)
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
    project_id: uuid.UUID,
    query: str,
    node_types: tuple[str, ...],
    top_n: int,
    tags: tuple[str, ...] | None,
) -> HybridSearchOutput:
    """执行三引擎融合检索的共享辅助函数。

    search_similar_requirements 与 search_code_snippets 共用此函数，
    仅 node_types 参数不同。
    """
    ctx = get_context()
    lifespan_ctx = ctx.lifespan_context

    filters = FilterSpec(
        project_id=project_id,
        node_types=node_types,
        tags=tags,
    )

    session = await get_readonly_session()
    try:
        result = await hybrid_search(
            session,
            query=query,
            embedding_client=lifespan_ctx.embedding_client,
            graph_store=lifespan_ctx.graph_store,
            top_n=top_n,
            filters=filters,
        )
    finally:
        await session.close()

    return HybridSearchOutput(
        query=query,
        fused=[_to_search_item_output(r) for r in result.get("fused", [])],
        vector=[_to_search_item_output(r) for r in result.get("vector", [])],
        fulltext=[_to_search_item_output(r) for r in result.get("fulltext", [])],
        total=len(result.get("fused", [])),
    )


def _to_search_item_output(result: SearchResult) -> SearchItemOutput:
    """从 SearchResult 构造 SearchItemOutput。"""
    return SearchItemOutput(
        node_id=result.node_id,
        title=result.title,
        content=result.content,
        node_type=result.node_type,
        score=result.score,
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
