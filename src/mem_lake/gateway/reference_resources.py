"""按需获取渠道：角色技能文档（MCP Resource）。

技能文档随镜像分发，通过 `memlake://skills/{role}/{file}` 读取；
`memlake://skills/index` 给出当前角色可读的文档清单（含版本与 sha256）。
不支持 MCP Resources 的客户端可用 `get_skill` 工具兜底。

鉴权：资源读取走 `on_request` 认证，RBAC 中间件只过滤工具，因此这里必须
自行用 `get_current_role()` 校验调用者角色（未认证会被拒绝）。
"""

from __future__ import annotations

import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_lake.gateway.dependencies import get_current_role
from mem_lake.gateway.skills_store import (
    SkillDocError,
    list_skill_entries,
    read_skill_doc,
)


def register_reference_resources(mcp: FastMCP) -> None:
    """注册技能文档资源（索引 + 角色文档模板）。"""

    @mcp.resource(  # type: ignore[untyped-decorator]
        "memlake://skills/index", mime_type="application/json"
    )
    async def skills_index() -> str:
        """当前角色可读的技能文档索引（role/file/version/sha256/uri）。"""
        entries = list_skill_entries(caller_role=get_current_role())
        return json.dumps(entries, ensure_ascii=False)

    @mcp.resource(  # type: ignore[untyped-decorator]
        "memlake://skills/{role}/{file}", mime_type="text/markdown"
    )
    async def skill_doc(role: str, file: str) -> str:
        """读取角色技能文档（SKILL=主文档，REFERENCE=详细参数）。"""
        try:
            return read_skill_doc(role, file, caller_role=get_current_role()).content
        except SkillDocError as e:
            raise ToolError(str(e)) from e
