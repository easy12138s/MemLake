"""图能力管理工具（ENH-01，Admin 专属）。

职责：转发 knowledge/graph_stats 服务层能力，薄封装为 MCP 工具。角色访问权由
RBAC 中间件层控制（本文件不区分角色）。

包含工具：
- get_graph_stats：图统计（节点/边按 type、system 维度聚合）。
- get_graph_quality_report：图质量基线报告（孤儿节点/重复度/连通分量）。
- generate_rule_edges：规则边生成（按节点属性规则批量建边，走审批批次，不直写）。

读写语义：前两者为只读查询；generate_rule_edges 产生审批批次（state 迁入
pending_review），为写语义。
"""

import logging
import uuid
from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from mem_lake.approval.service import PayloadValidationError, submit_batch
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    get_readonly_session,
    transactional_session,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    WRITE_TOOL_ANNOTATIONS,
    get_lifespan_context,
    to_tool_error,
)
from mem_lake.knowledge.graph_stats import (
    RULE_SNIPPET_MODULE_REFERENCES,
    build_rule_edge_items,
)
from mem_lake.knowledge.graph_stats import (
    get_graph_quality_report as svc_graph_quality_report,
)
from mem_lake.knowledge.graph_stats import (
    get_graph_stats as svc_graph_stats,
)

logger = logging.getLogger("mem_lake.gateway.tools.graph")

# generate_rule_edges 提交的审批批次类型（已登记入 approval BATCH_TYPES）
RULE_EDGE_BATCH_TYPE = "generate_rule_edges"


# ============================================================================
# 输出模型
# ============================================================================


class GraphStatsOutput(BaseModel):
    """get_graph_stats 工具出参。"""

    nodes_by_type: dict[str, int] = Field(description="节点数按 type（label）分布")
    nodes_by_system: dict[str, int] = Field(
        description="节点数按 system_id 分布（未挂 system 的不计）"
    )
    edges_by_type: dict[str, int] = Field(description="边数按边类型分布")
    edges_by_system: dict[str, int] = Field(
        description="有向边数按源端点 system_id 分布（未挂 system 的不计）"
    )


class GraphQualityReportOutput(BaseModel):
    """get_graph_quality_report 工具出参。"""

    total_nodes: int = Field(description="节点总数")
    total_edges: int = Field(description="有向边总数")
    orphan_nodes: int = Field(description="孤儿节点数（连边数为 0）")
    duplicate_groups_count: int = Field(
        description="重复分组数（同 type+title 出现 >1 的分组数）"
    )
    duplicate_node_count: int = Field(description="落入重复分组的节点总数")
    connected_components: int = Field(description="连通分量数")


class RuleEdgeMatch(BaseModel):
    """规则边生成匹配详情。"""

    source: uuid.UUID = Field(description="源节点 ID（CodeSnippet）")
    source_title: str = Field(description="源节点标题")
    target: uuid.UUID = Field(description="目标节点 ID（Requirement）")
    target_title: str = Field(description="目标节点标题")
    module: str = Field(description="命中的 Requirement.module 属性")


class RuleEdgeOutput(BaseModel):
    """generate_rule_edges 工具出参。"""

    rule: str = Field(description="使用的规则名")
    batch_type: str = Field(description="审批批次类型（generate_rule_edges）")
    batch_id: uuid.UUID | None = Field(
        default=None, description="审批批次 ID；无匹配时不产生批次（None）"
    )
    status: str = Field(
        description="结果状态：pending_review（已提交批次待审批）/ no_match（无匹配不建批）"
    )
    edge_count: int = Field(description="本次产出的 references 边数")
    matches: list[RuleEdgeMatch] = Field(
        default_factory=list, description="规则匹配明细供 admin 人工复核"
    )


# ============================================================================
# 工具注册
# ============================================================================


def register_graph_tools(mcp: FastMCP) -> None:
    """注册图能力管理工具到 FastMCP 实例。"""

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_graph_stats() -> GraphStatsOutput:
        """图统计：节点/边按 type、system 维度聚合计数（AGE Cypher）。

        Admin 工具。反映团队知识图谱规模与分布，供容量评估与数据健康度排查：
        - nodes_by_type / edges_by_type：各节点/边类型数量
        - nodes_by_system / edges_by_system：按 system 域的分布（未挂 system 的不计）
        """
        try:
            lifespan_ctx = get_lifespan_context()
            session = await get_readonly_session()
            try:
                stats = await svc_graph_stats(session, lifespan_ctx.graph_store)
                return GraphStatsOutput(**stats)
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_graph_quality_report() -> GraphQualityReportOutput:
        """图质量基线报告：孤儿节点数、重复度、连通分量数。

        Admin 工具。度量图谱结构质量：
        - orphan_nodes：孤立节点（连边数为 0）
        - duplicate_groups_count / duplicate_node_count：同 type+title 的重复度
        - connected_components：连通分量数（≥1；过大提示图谱分裂）
        """
        try:
            lifespan_ctx = get_lifespan_context()
            session = await get_readonly_session()
            try:
                report = await svc_graph_quality_report(session, lifespan_ctx.graph_store)
                return GraphQualityReportOutput(**report)
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def generate_rule_edges(
        rule: str = Field(
            default=RULE_SNIPPET_MODULE_REFERENCES,
            description="规则名（当前支持：snippet_module_references）",
        ),
        project_id: uuid.UUID | None = Field(
            default=None,
            description="限定项目域（None=全部项目；匹配仍按同项目配对）",
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键（网络重试防重复建批）"
        ),
    ) -> RuleEdgeOutput:
        """规则边生成：按节点属性规则批量建边，走审批流程提交（不直写图谱）。

        Admin 工具。当前规则 snippet_module_references：将 CodeSnippet 与其
        responsibility 文本中引用的同项目 Requirement（按 module 属性匹配）建
        references 边。全部匹配产出为审批批次（status=pending_review），审批通过后
        由审核者 review_approve 落地；已存在的 references 边自动跳过（幂等）。
        """
        try:
            lifespan_ctx = get_lifespan_context()
            async with transactional_session() as session:
                items, matched = await build_rule_edge_items(
                    session,
                    lifespan_ctx.graph_store,
                    rule=rule,
                    project_id=project_id,
                )
                if not items:
                    return RuleEdgeOutput(
                        rule=rule,
                        batch_type=RULE_EDGE_BATCH_TYPE,
                        batch_id=None,
                        status="no_match",
                        edge_count=0,
                        matches=_to_matches(matched),
                    )
                batch = await submit_batch(
                    session,
                    project_id=project_id,
                    batch_type=RULE_EDGE_BATCH_TYPE,
                    submitted_by=get_current_key_id(),
                    submitter_role="admin",
                    items=items,
                    operation_id=operation_id,
                )
            return RuleEdgeOutput(
                rule=rule,
                batch_type=RULE_EDGE_BATCH_TYPE,
                batch_id=batch.id,
                status=batch.status,
                edge_count=len(items),
                matches=_to_matches(matched),
            )
        except (ValueError, PayloadValidationError) as e:
            raise to_tool_error(e) from e


def _to_matches(
    matched: list[dict[str, Any]],
) -> list[RuleEdgeMatch]:
    """把服务层 matched_detail 列表转换为 RuleEdgeMatch 出参。"""
    return [
        RuleEdgeMatch(
            source=uuid.UUID(item["source"]),
            source_title=item["source_title"],
            target=uuid.UUID(item["target"]),
            target_title=item["target_title"],
            module=item["module"],
        )
        for item in matched
    ]
