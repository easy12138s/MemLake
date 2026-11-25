"""技能文档读取与 MCP Resource 注册。"""

import pytest

from mem_lake.gateway.skills_store import (
    SKILL_FILES,
    SkillDocError,
    list_skill_entries,
    read_skill_doc,
)


def test_read_own_role_skill():
    doc = read_skill_doc("pm", "SKILL", caller_role="pm")
    assert doc.role == "pm"
    assert doc.file == "SKILL"
    assert "# PM Skills" in doc.content  # 内容含 YAML frontmatter，故用包含断言
    assert doc.version.count(".") == 2
    assert len(doc.sha256) == 16
    assert doc.uri == "memlake://skills/pm/SKILL"


def test_read_reference_and_md_suffix():
    doc = read_skill_doc("dev", "REFERENCE.md", caller_role="dev")
    assert doc.file == "REFERENCE"
    assert doc.uri == "memlake://skills/dev/REFERENCE"


def test_non_admin_cannot_read_other_role():
    with pytest.raises(SkillDocError, match="无权读取"):
        read_skill_doc("admin", "SKILL", caller_role="pm")


def test_admin_can_read_any_role():
    assert read_skill_doc("dev", "SKILL", caller_role="admin").role == "dev"


def test_unknown_role_or_file():
    with pytest.raises(SkillDocError, match="未知角色"):
        read_skill_doc("guest", "SKILL", caller_role="admin")
    with pytest.raises(SkillDocError, match="未知技能文件"):
        read_skill_doc("pm", "SECRET", caller_role="pm")


def test_index_is_scoped_by_caller():
    pm_entries = list_skill_entries(caller_role="pm")
    assert {e["role"] for e in pm_entries} == {"pm"}
    assert {e["file"] for e in pm_entries} == set(SKILL_FILES)
    admin_entries = list_skill_entries(caller_role="admin")
    assert {e["role"] for e in admin_entries} == {"admin", "pm", "dev"}


async def test_server_registers_skill_resources():
    from mem_lake.gateway.server import create_mcp_server

    mcp = create_mcp_server()
    templates = await mcp.list_resource_templates(run_middleware=False)
    uris = {
        getattr(t, "uri_template", None) or getattr(t, "uriTemplate", None)
        for t in templates
    }
    assert "memlake://skills/{role}/{file}" in uris
    resources = {str(r.uri) for r in await mcp.list_resources(run_middleware=False)}
    assert "memlake://skills/index" in resources
    assert "memlake://skills/" in (mcp.instructions or "")


async def test_resource_read_requires_auth():
    """无 Access Key 时资源读取必须被拒（不泄露技能内容）。"""
    from fastmcp import Client, FastMCP

    from mem_lake.gateway.reference_resources import register_reference_resources

    mcp = FastMCP("resources-no-auth")
    register_reference_resources(mcp)
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="未认证"):
            await client.read_resource("memlake://skills/pm/SKILL")
