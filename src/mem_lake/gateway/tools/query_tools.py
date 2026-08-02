"""查询类工具：读取知识图谱内容与系统元信息（只读，不产生审批批次）。

工具职责：转发 knowledge/repository 与 audit/service 的只读查询，不写业务逻辑。

包含工具（PDD 6.1）：
- get_role_skills（已实现，三角色共享）：获取角色 Skills 指导文档
- get_project_profile（M6b 待实现）：查询项目画像
- get_requirement_context（M6b 待实现）：查询需求上下文（关联节点+关系链）
- query_audit_log（M6b 待实现，admin 专属）：查询审计日志

设计要点：
- 全部为只读工具（READ_TOOL_ANNOTATIONS）
- 角色 RBAC 由中间件层控制，本文件不区分角色
- get_role_skills 为元工具，指导 Agent 如何使用工具集
"""

import logging

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from mem_lake.gateway.dependencies import get_current_role
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    ROLE_SKILLS_MD,
    ROLE_SKILLS_VERSION,
    to_tool_error,
)

logger = logging.getLogger("mem_lake.gateway.tools.query")


# ============================================================================
# 输出模型
# ============================================================================


class GetRoleSkillsOutput(BaseModel):
    """get_role_skills 工具出参。"""

    role: str = Field(description="角色")
    skills_markdown: str = Field(description="角色 Skills 指导文档（Markdown 格式）")
    version: str = Field(description="Skills 文档版本")


# ============================================================================
# 工具注册
# ============================================================================


def register_query_tools(mcp: FastMCP) -> None:
    """注册查询类工具到 FastMCP 实例。

    当前已实现：get_role_skills。
    M6b 待实现：get_project_profile / get_requirement_context / query_audit_log。
    """

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_role_skills(
        role: str | None = Field(
            default=None,
            description="指定角色（admin/pm/dev），None 表示返回当前调用者角色",
        ),
    ) -> GetRoleSkillsOutput:
        """获取角色 Skills 指导文档（Markdown 格式）。

        共享工具（三角色均可调用）。返回当前角色或指定角色的 Skills 文档，
        指导 Agent 如何使用 Mem Lake 工具集。
        """
        target_role = role or get_current_role()
        if target_role not in ROLE_SKILLS_MD:
            raise to_tool_error(
                ValueError(f"未知角色: {target_role}，合法角色: admin/pm/dev")
            )
        return GetRoleSkillsOutput(
            role=target_role,
            skills_markdown=ROLE_SKILLS_MD[target_role],
            version=ROLE_SKILLS_VERSION,
        )

    # ------------------------------------------------------------------------
    # M6b 待实现工具占位（暂不注册，实现时取消注释并补全逻辑）
    # ------------------------------------------------------------------------
    # @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    # async def get_project_profile(
    #     project_id: uuid.UUID = Field(description="项目 ID"),
    # ) -> "ProjectProfileOutput":
    #     """查询项目画像（技术栈/架构/约定/团队）。"""
    #     ...
    #
    # @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    # async def get_requirement_context(
    #     requirement_id: uuid.UUID = Field(description="需求节点 ID"),
    #     depth: int = Field(default=2, description="关系链深度"),
    # ) -> "RequirementContextOutput":
    #     """查询需求上下文（关联的代码/方案/意图/踩坑节点 + 关系链）。"""
    #     ...
    #
    # @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    # async def query_audit_log(
    #     project_id: uuid.UUID | None = Field(default=None, description="项目 ID 过滤"),
    #     actor: str | None = Field(default=None, description="操作者过滤"),
    #     action: str | None = Field(default=None, description="操作类型过滤"),
    #     target_type: str | None = Field(default=None, description="目标类型过滤"),
    #     start_time: datetime | None = Field(default=None, description="起始时间"),
    #     end_time: datetime | None = Field(default=None, description="结束时间"),
    #     limit: int = Field(default=100, description="返回数量上限"),
    #     offset: int = Field(default=0, description="分页偏移"),
    # ) -> "AuditLogOutput":
    #     """查询审计日志（admin 专属）。"""
    #     ...
