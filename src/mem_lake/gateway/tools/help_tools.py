"""按需帮助工具：get_skill（角色技能文档）。

技能文档主渠道是 MCP Resource（`memlake://skills/{role}/{file}`）；
本工具用于不支持 Resources 的客户端兜底，与资源共用 `skills_store`，
内容、版本、sha256 完全一致。
"""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from mem_lake.gateway.dependencies import get_current_role
from mem_lake.gateway.skills_store import SkillDocError, read_skill_doc
from mem_lake.gateway.tools._shared import READ_TOOL_ANNOTATIONS


class SkillDocOutput(BaseModel):  # type: ignore[misc]
    """get_skill 出参：技能文档内容与自证信息。"""

    role: str
    file: str
    version: str
    sha256: str
    uri: str
    content: str


def build_skill_doc_output(
    role: str | None, file: str, *, caller_role: str
) -> SkillDocOutput:
    """读取技能文档并组装出参（role 缺省为调用者角色）。"""
    doc = read_skill_doc(role or caller_role, file, caller_role=caller_role)
    return SkillDocOutput(
        role=doc.role,
        file=doc.file,
        version=doc.version,
        sha256=doc.sha256,
        uri=doc.uri,
        content=doc.content,
    )


def register_help_tools(mcp: FastMCP) -> None:
    """注册按需帮助工具。"""

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)  # type: ignore[untyped-decorator]
    async def get_skill(
        role: str | None = Field(
            default=None, description="目标角色（admin/pm/dev）；默认当前 Key 角色"
        ),
        file: Literal["SKILL", "REFERENCE"] = Field(
            default="SKILL", description="SKILL=主文档 / REFERENCE=详细参数"
        ),
    ) -> SkillDocOutput:
        """读取角色技能文档（MCP Resource 的兜底通道，按需调用，不占常驻上下文）。

        默认返回当前 Key 角色的 SKILL；`file="REFERENCE"` 取详细参数表。
        非 admin 只能读自己角色；admin 可读全部。技能文档随镜像分发，
        版本与 sha256 一并返回，可自证读到的是当前镜像版本。
        """
        try:
            return build_skill_doc_output(role, file, caller_role=get_current_role())
        except SkillDocError as e:
            raise ToolError(str(e)) from e
