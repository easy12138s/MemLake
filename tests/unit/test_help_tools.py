"""get_skill 核心与 RBAC 可见性。"""

import pytest

from mem_lake.auth.rbac import has_tool_access
from mem_lake.gateway.skills_store import SkillDocError
from mem_lake.gateway.tools.help_tools import build_skill_doc_output


def test_get_skill_defaults_to_caller_role():
    out = build_skill_doc_output(None, "SKILL", caller_role="pm")
    assert out.role == "pm"
    assert out.uri == "memlake://skills/pm/SKILL"
    assert "# PM Skills" in out.content


def test_get_skill_denies_cross_role():
    with pytest.raises(SkillDocError, match="无权读取"):
        build_skill_doc_output("admin", "SKILL", caller_role="dev")


def test_help_tool_available_to_all_roles():
    for role in ("admin", "pm", "dev"):
        assert has_tool_access(role, "get_skill") is True


async def test_get_skill_registered():
    from mem_lake.gateway.server import create_mcp_server

    names = {t.name for t in await create_mcp_server()._list_tools()}
    assert "get_skill" in names
