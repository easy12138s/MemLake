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
import re
import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, computed_field

from mem_lake.config import get_settings
from mem_lake.gateway.dependencies import (
    get_current_role,
    get_current_system_scope,
    get_readonly_session,
    validate_project_access,
    validate_system_access,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    get_lifespan_context,
    resolve_search_scope_fallback,
    to_tool_error,
)
from mem_lake.knowledge.repository import (
    get_node,
    get_system_project_ids,
    list_nodes_by_project,
)
from mem_lake.knowledge.schema import SchemaValidationError
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import SearchResult, hybrid_search
from mem_lake.search.tag_expansion import expand_tags_for_project

if TYPE_CHECKING:
    # 仅类型标注用；get_lifespan_context 返回 LifespanContext，GraphSearcher 惰性导入
    from mem_lake.gateway.server import LifespanContext
    from mem_lake.knowledge.models import KnowledgeNode
    from mem_lake.search.graph import GraphSearcher

logger = logging.getLogger("mem_lake.gateway.tools.search")


# ============================================================================
# 输出模型
# ============================================================================


class SearchItemOutput(BaseModel):
    """单个检索结果项（content 为前 200 字摘要；source=引擎；score/vector_score=相似度）。"""

    node_id: uuid.UUID = Field(description="节点 ID")
    title: str = Field(description="标题")
    content: str = Field(description="摘要")
    node_type: str = Field(description="类型")
    score: float | None = Field(description="分数")
    vector_score: float | None = Field(default=None, description="向量余弦分")
    source: str = Field(description="来源")
    properties: dict[str, Any] = Field(default={}, description="属性")
    tags: list[str] = Field(default=[], description="标签")


class HybridSearchOutput(BaseModel):
    """search_similar_requirements / search_code_snippets 出参。

    出参仅保留调用方决策所需字段：query（原始查询）、fused（融合精排结果）、
    query_terms（分词自诊）、candidates_total/truncated/returned（候选计数与
    翻页提示）。引擎原始明细不入出参，检索质量评估经 admin 渠道直查。
    """

    query: str = Field(description="原始查询文本")
    fused: list[SearchItemOutput] = Field(
        description="融合结果（向量+全文，rerank 后），按相关性降序"
    )
    query_terms: list[str] | None = Field(
        default=None,
        description="全文引擎实际分词结果（保序去重）——检索没命中时可据此自诊",
    )
    candidates_total: int = Field(
        default=0,
        description="min_score 过滤后、top_n 截断前的真实候选数（同 query 恒定，"
        "评估命中量以本字段为准——批次五语义修正：不再随 top_n 变化）",
    )
    truncated: bool = Field(
        default=False,
        description="候选池是否大于本页 top_n（true=还有更多候选，翻大 top_n 可取）",
    )

    @computed_field  # type: ignore[prop-decorator, untyped-decorator]
    @property
    def returned(self) -> int:
        """实际返回条数，恒等于 len(fused)（computed，构造无需传参）。"""
        return len(self.fused)


class ImpactScopeOutput(BaseModel):
    """analyze_impact_scope 出参。"""

    requirement_id: uuid.UUID = Field(description="需求节点 ID")
    requirement: dict[str, Any] | None = Field(
        default=None, description="需求节点详情"
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
    pitfalls: list[dict[str, Any]] = Field(
        default=[], description="需求挂载的踩坑节点（described_by/references 链）"
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


# 全文引擎查询构造：match_mode=any 时按空白拆词 OR 连接（websearch_to_tsquery
# 原生支持 OR 关键字）；all 保持原样（空格即 AND/短语语义，现状不破坏）。
def _build_fulltext_query(query: str, match_mode: str) -> str:
    """按 match_mode 构造全文引擎的查询串。

    - all（默认）：原样返回——websearch_to_tsquery 的空格分隔即 AND 语义
    - any：空白拆词后用 OR 连接，多词任一命中即召回（多词宽召回场景，
      直击「多词混合查询全文 0 贡献、被向量噪声占据」的反馈问题）
    连续中文串不拆（无分隔信息），交由 zhparser 整体切词——与索引端口径一致。
    """
    if match_mode == "all":
        return query
    if match_mode == "any":
        terms = [t for t in query.split() if t]
        if len(terms) <= 1:
            return query
        return " OR ".join(terms)
    raise ValueError(f"非法 match_mode: {match_mode!r}，合法值: all/any")


# tsquery lexeme 解析：从 websearch_to_tsquery 的文本形态提取分词结果，
# 回显 query_terms 供调用方自诊「实际用什么词在匹配」（反馈建议 3）。
_LEXEME_RE = re.compile(r"'([^']+)'")


def _parse_tsquery_lexemes(tsquery_text: str) -> list[str]:
    """解析 tsquery 文本（如 '扫'<->'码' & '搜索'）为 lexeme 列表（保序去重）。"""
    seen: list[str] = []
    for m in _LEXEME_RE.finditer(tsquery_text or ""):
        lex = m.group(1)
        if lex and lex not in seen:
            seen.append(lex)
    return seen


# ============================================================================
# 检索工具注册
# ============================================================================


def register_search_tools(mcp: FastMCP) -> None:
    """注册检索类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def search_similar_requirements(
        query: str = Field(description="查询文本（需求描述/关键词）"),
        system_id: uuid.UUID | None = Field(
            default=None, description="归属 system 域（可选；与 project_id 均不传时按"
            " Access Key 绑定的 system 兜底——仅绑定唯一 system 时自动用之，多个则报错列出候选；"
            "admin 不兜底，须显式传入其一）"
        ),
        project_id: uuid.UUID | None = Field(
            default=None, description="归属项目 ID（与 system_id 至少其一必填或走 Key 兜底）"
        ),
        top_n: int = Field(default=20, description="返回数量上限"),
        tags: list[str] | None = Field(default=None, description="标签过滤"),
        tags_op: str = Field(
            default="all", description="标签语义：all=AND（默认）/any=OR"
        ),
        min_score: float | None = Field(
            default=0.5,
            description="仅过滤纯向量命中（0~1）；有全文命中的节点不受影响；"
            "0.99=只要全文精确命中（清单穷举）；None=关闭阈值",
        ),
        semantic_tags: bool = Field(
            default=False,
            description="标签语义扩展（embedding 近义召回），默认精确匹配；"
            "仅显式传 project_id 时生效（system 维度检索不扩展）",
        ),
        match_mode: Literal["all", "any"] = Field(
            default="all",
            description="多词语义（全文引擎）：all=AND 全词命中（默认）/any=任一词命中即召回",
        )
    ) -> HybridSearchOutput:
        """向量+全文融合检索相似需求（Requirement；按 system/project 隔离，仅 approved）。

        - system_id/project_id 均不传时按 Key 绑定的唯一 system 兜底
        - 出参 fused 的 score 为向量余弦分（0~1）；query_terms 回显全文实际分词
        - 要查某需求的关联产物用 get_requirement_context；影响分析用 analyze_impact_scope
        """
        try:
            project_id, system_id = resolve_search_scope_fallback(
                get_current_role(),
                get_current_system_scope(),
                project_id=project_id,
                system_id=system_id,
            )
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
                match_mode=match_mode,
            )
        except (SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def search_code_snippets(
        query: str = Field(description="查询文本（代码功能/关键词）"),
        project_id: uuid.UUID | None = Field(
            default=None,
            description="归属项目 ID（与 system_id 均不传时按 Key 绑定 system 兜底，"
            "检索该 system 下全部项目的资产）",
        ),
        system_id: uuid.UUID | None = Field(
            default=None,
            description="归属 system 域——检索该 system 下全部项目的资产"
            "（症状式检索跨项目可见，消「图读得到/检索恒空」的可见性割裂）",
        ),
        top_n: int = Field(default=20, description="返回数量上限"),
        tags: list[str] | None = Field(default=None, description="标签过滤"),
        tags_op: str = Field(
            default="all", description="标签语义：all=AND（默认）/any=OR"
        ),
        min_score: float | None = Field(
            default=0.5,
            description="仅过滤纯向量命中（0~1）；有全文命中的节点不受影响；"
            "0.99=只要全文精确命中；None=关闭阈值",
        ),
        semantic_tags: bool = Field(
            default=False,
            description="标签语义扩展（embedding 近义召回），默认精确匹配；"
            "仅显式传 project_id 时生效（system 维度检索不扩展）",
        ),
        match_mode: Literal["all", "any"] = Field(
            default="all",
            description="多词语义（全文引擎）：all=AND 全词命中（默认）/any=任一词命中即召回",
        ),
    ) -> HybridSearchOutput:
        """向量+全文融合检索研发资产（CodeSnippet/Pitfall/Solution/DesignIntent，仅 approved）。

        scope 三态：传 project_id 检索单项目；传 system_id 检索该系统下全部项目
        （症状式检索的推荐用法——踩坑/方案跨项目可见）；均不传按 Key 绑定
        system 兜底。踩坑/方案/意图与代码一并召回，node_type 区分类型。
        要找需求本身用 search_similar_requirements；需求关联产物用 get_requirement_context。
        """
        try:
            project_id, system_id = resolve_search_scope_fallback(
                get_current_role(),
                get_current_system_scope(),
                project_id=project_id,
                system_id=system_id,
            )
            if project_id is not None:
                validate_project_access(project_id)
            if system_id is not None:
                validate_system_access(system_id)

            # system 维度 → 该 system 下全部项目集（asset 节点无 system_id 列，
            # 经 SystemProject 关联表展开；09-29 报告 P0-2 可见性统一）
            project_ids: tuple[uuid.UUID, ...] | None = None
            if project_id is None and system_id is not None:
                session = await get_readonly_session()
                try:
                    pids = await get_system_project_ids(session, system_id=system_id)
                finally:
                    await session.close()
                if not pids:
                    raise ValueError(
                        f"system {system_id} 下未挂载任何项目，无资产可检索"
                        "（admin 可用 manage_system add_projects 挂载）"
                    )
                project_ids = tuple(pids)

            _validate_query(query)
            return await _run_hybrid_search(
                project_id=project_id,
                project_ids=project_ids,
                query=query,
                node_types=("CodeSnippet", "Pitfall", "Solution", "DesignIntent"),
                top_n=top_n,
                tags=tuple(tags) if tags else None,
                tags_op=tags_op,
                min_score=min_score,
                semantic_tags=semantic_tags,
                match_mode=match_mode,
            )
        except (SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def analyze_impact_scope(
        requirement_id: uuid.UUID = Field(description="需求节点 ID"),
        project_id: uuid.UUID | None = Field(
            default=None,
            description="归属项目 ID（可选：不传时按需求自身归属校验——悬浮需求"
            "走 system 权限，纯 system 绑定 Key 可直接调用）",
        ),
        max_depth: int = Field(
            default=5, description="depends_on 依赖链遍历深度"
        ),
    ) -> ImpactScopeOutput:
        """图检索分析变更影响范围（需求→代码→方案→设计意图→踩坑）。

        PM/Dev 工具。从需求出发遍历：
        Requirement --implements--> CodeSnippet --depends_on--> CodeSnippet
        CodeSnippet --realized_by--> Solution --embodies--> DesignIntent
        另含 described_by/references 挂载的资产：踩坑归 pitfalls 段，references
        引用的代码/方案/意图分别并入对应段。
        权限锚定：显式传 project_id 校验项目权限；不传则按需求自身归属
        （project → 项目权限 / 悬浮 → system 权限）。
        用途边界：本工具做**变更影响范围**遍历。若只想看某需求的**直接关联节点**，用 get_requirement_context。
        """
        try:
            if project_id is not None:
                validate_project_access(project_id)
            lifespan_ctx = get_lifespan_context()

            session = await get_readonly_session()
            try:
                if project_id is None:
                    # 批次六（09-29 报告缺口）：不传 project_id 时按需求自身
                    # 归属校验——悬浮需求（project=None）走 system 权限，
                    # 纯 system 绑定 Key 不再结构性不可用
                    anchor = await get_node(session, requirement_id)
                    if anchor.project_id is not None:
                        validate_project_access(anchor.project_id)
                    elif anchor.system_id is not None:
                        validate_system_access(anchor.system_id)
                    elif get_current_role() != "admin":
                        raise ToolError(
                            f"需求 {requirement_id} 无 project/system 归属，非 admin 无权访问"
                        )
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
                    pitfalls=result.get("pitfalls", []),
                )
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e) from e

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
        """检测需求冲突（同项目同类型高相似度节点）。

        PM 工具。融合检索召回后按向量余弦阈值过滤，检测与指定需求冲突的
        潜在重复/矛盾需求。自动排除自身节点，仅返回 score >= threshold 的结果。
        threshold 缺省读配置 CONFLICT_SIMILARITY_THRESHOLD（与审批流冲突检测同一
        阈值来源，避免双源脱钩；AUDIT §2.10）。本工具为相似度过滤，不含审批层
        detect_conflicts 的 L2 关键属性比对——二者定位不同（前者给 PM 主动
        排查，后者是审批质量门禁）。
        has_conflict=true 时 suggestion 推荐 review（人工核查）或 manual_merge（高相似度合并）。
        """
        try:
            validate_project_access(project_id)
            if threshold is None:
                threshold = get_settings().CONFLICT_SIMILARITY_THRESHOLD
            lifespan_ctx = get_lifespan_context()

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
                # 最高相似度 >= CONFLICT_SUGGEST_PENDING(0.95) 推荐 manual_merge，否则 review
                # （FIX-21：阈值可配置；conflicts 已在上面过滤 score is not None，
                #   此处再显式过滤以收敛类型——生成的 score 必为 float 参与 max 比较）
                max_score = max(c.score for c in conflicts if c.score is not None)
                suggest_threshold = get_settings().CONFLICT_SUGGEST_PENDING
                suggestion = "manual_merge" if max_score >= suggest_threshold else "review"

            return ConflictCheckOutput(
                requirement_id=requirement_id,
                has_conflict=has_conflict,
                conflicts=conflicts,
                threshold=threshold,
                suggestion=suggestion,
            )
        except Exception as e:
            raise to_tool_error(e) from e

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

        Admin 工具。默认仅返回 approved 节点，传 status 可含已归档。
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
            raise to_tool_error(e) from e


# ============================================================================
# 辅助函数
# ============================================================================


async def _run_hybrid_search(
    *,
    project_id: uuid.UUID | None = None,
    project_ids: tuple[uuid.UUID, ...] | None = None,
    system_id: uuid.UUID | None = None,
    query: str,
    node_types: tuple[str, ...],
    top_n: int,
    tags: tuple[str, ...] | None,
    tags_op: str = "all",
    min_score: float | None = None,
    semantic_tags: bool = False,
    match_mode: str = "all",
) -> HybridSearchOutput:
    """执行三引擎融合检索的共享辅助函数。

    search_similar_requirements 与 search_code_snippets 共用此函数，
    仅 node_types 参数不同。min_score 过滤规则（ISSUE-02 修复）：**仅过滤
    纯向量命中**——有全文命中的节点不受阈值影响（此前实现把向量低分捎带
    召回的全文命中一并误杀，docstring 已声明的语义以本行为为准）。
    semantic_tags=true 时，先用 embedding 将 tags 扩展为项目内语义相近标签再过滤。

    match_mode=any 时全文引擎按空白拆词 OR 连接（多词任一命中即召回），
    向量引擎仍用原 query；默认 all 保持 websearch 的 AND 语义。

    system 维度：传 project_id 检索该 project 资产；传 system_id 检索该系统全部需求
    （含悬浮需求）。二者都不传时用 project_scope 内全部 project 检索。
    project_ids：system 维度资产检索的展开形式（system 下全部项目集）——asset
    节点无 system_id 列，工具层经 SystemProject 关联表解析后传入。

    出参仅含 fused（融合+精排后的最终结果）——引擎原始明细为调试数据，
    已随 include_engine_details 一并删除（批次三工具面治理）。
    """
    lifespan_ctx = get_lifespan_context()

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
        project_ids=project_ids,
        system_id=system_id,
        node_types=node_types,
        tags=effective_tags,
        tags_op=tags_op,
    )

    # 引擎候选池联动 top_n（ISSUE-02 现象 A）：此前 top_k 恒默认 50，
    # 用户请求 top_n=200/500 时候选池仍被 50 掐死、结果集莫名偏小。
    # clamp 上限防极端 top_n 拖库。
    engine_top_k = min(max(top_n, 50), 500)

    # hybrid_search 内部为每引擎自建独立 session（AsyncSession 非并发安全）
    result = await hybrid_search(
        query=query,
        embedding_client=lifespan_ctx.embedding_client,
        graph_store=lifespan_ctx.graph_store,
        top_k=engine_top_k,
        top_n=top_n,
        filters=filters,
        fulltext_query=_build_fulltext_query(query, match_mode),
    )

    # 向量 cosine 分映射，用于 min_score 过滤与 fused 结果附带 vector_score
    vector_score_map = {
        r.node_id: r.score for r in result.get("vector", []) if r.score is not None
    }
    # 全文引擎命中集：min_score 豁免依据（ISSUE-02 现象 B/C 修复）——
    # 有全文命中的节点不受阈值影响。此前实现只看 vector_score_map 归属，
    # 把"向量低分捎带召回 + 精确全文命中"的节点一并误杀（0 召回的根因）。
    fulltext_ids = {r.node_id for r in result.get("fulltext", [])}

    # 批次五（报告 P0-3）：candidates_total = min_score 过滤后、top_n 截断前的
    # 真实候选数——从融合全量池（fused_pool）计数，与 top_n 无关；此前从
    # 截断后的 fused_raw 计数导致 top_n=3 → 3，Agent 无法自证穷举。
    fused_pool = result.get("fused_pool") or result.get("fused", [])

    def _passes(r: "SearchResult") -> bool:
        # 仅过滤"纯向量命中"节点；有全文命中或无向量分的节点予以保留
        return (
            r.node_id in fulltext_ids
            or r.node_id not in vector_score_map
            or vector_score_map.get(r.node_id, -1) >= (min_score if min_score is not None else 0)
        )

    candidates_total = sum(1 for r in fused_pool if _passes(r))

    fused_raw = result.get("fused", [])
    if min_score is not None:
        fused_raw = [r for r in fused_raw if _passes(r)]

    return HybridSearchOutput(
        query=query,
        fused=[_to_search_item_output(r, vector_score_map.get(r.node_id)) for r in fused_raw],
        query_terms=_parse_tsquery_lexemes(result.get("fulltext_tsquery", "")),
        candidates_total=candidates_total,
        truncated=candidates_total > len(fused_raw),
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


def _to_knowledge_node_output(node: "KnowledgeNode") -> KnowledgeNodeOutput:
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


def _get_graph_searcher(lifespan_ctx: "LifespanContext") -> "GraphSearcher":
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
