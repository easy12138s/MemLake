"""skills 文件内容测试（文件级）。

技能文档随镜像经 MCP Resource 分发（memlake://skills/...），get_role_skills
工具已删除；SKILL.md/REFERENCE.md 内容质量回归直接读文件断言
（frontmatter 版本 / 关键工具引用 / 资源获取说明）。
"""

from pathlib import Path

import yaml  # type: ignore[import-untyped]

_SKILLS_DIR = Path(__file__).parent.parent.parent / "src" / "mem_lake" / "skills"


def _load_skill(role: str) -> tuple[str, str]:
    """读 SKILL.md，返回 (body, version)。"""
    content = (_SKILLS_DIR / role / "SKILL.md").read_text(encoding="utf-8")
    parts = content.split("---", 2)
    frontmatter = yaml.safe_load(parts[1])
    return parts[2].strip(), str(frontmatter.get("version", "0.0.0"))


class TestSkillsFiles:
    """三角色 skills 文件存在性与结构。"""

    def test_three_roles_present(self):
        for role in ("admin", "pm", "dev"):
            body, _ = _load_skill(role)
            assert body.startswith("# ")
            assert f"{role} Skills".lower() in body.lower()

    def test_removed_tools_not_referenced(self):
        """已删工具（get_role_skills/get_project_profile）不得作为可用工具出现在清单。"""
        for role in ("admin", "pm", "dev"):
            body, _ = _load_skill(role)
            assert "- **get_role_skills**" not in body
            assert "- **get_project_profile**" not in body

    def test_list_requirements_referenced(self):
        """新枚举工具 list_requirements 进三角色工具清单。"""
        for role in ("admin", "pm", "dev"):
            body, _ = _load_skill(role)
            assert "- **list_requirements**" in body

    def test_installation_guide_section(self):
        """安装指南段随文件分发（原 INSTALLATION_GUIDE 常量内容迁入）。"""
        for role in ("admin", "pm", "dev"):
            body, _ = _load_skill(role)
            assert "## Skills 文件放置指南" in body
            assert ".agents/skills/" in body

    def test_version_1_19_0(self):
        """1.19.0：技能分发改 MCP Resource 优先、admin 收敛为只读查询口径。"""
        for role in ("admin", "pm", "dev"):
            _, version = _load_skill(role)
            assert version == "1.19.0"

    def test_reference_files_carry_version(self):
        """REFERENCE.md 也带 frontmatter 版本（get_skill 自证用）。"""
        for role in ("admin", "pm", "dev"):
            text = (_SKILLS_DIR / role / "REFERENCE.md").read_text(encoding="utf-8")
            parts = text.split("---", 2)
            frontmatter = yaml.safe_load(parts[1])
            assert str(frontmatter.get("version")) == "1.19.0"

    def test_mcp_resource_distribution_note(self):
        """文件含 MCP 资源获取说明与 get_skill 兜底。"""
        for role in ("admin", "pm", "dev"):
            body, _ = _load_skill(role)
            assert "memlake://skills/" in body
            assert "get_skill" in body

    def test_admin_skill_no_write_tools(self):
        """admin 收敛后技能文档不得再列 PM/Dev 写入工具。"""
        body, _ = _load_skill("admin")
        for banned in (
            "publish_requirement",
            "update_requirement_relations",
            "submit_dev_artifacts",
            "update_node",
            "bind_requirement_to_project",
        ):
            assert banned not in body, banned


class TestRoleSkillsContent:
    """各角色 skills 文件的关键内容回归。"""

    def test_pm_skills_content(self):
        body, _ = _load_skill("pm")
        assert "publish_requirement" in body
        assert "update_requirement_relations" in body

    def test_dev_skills_content(self):
        body, _ = _load_skill("dev")
        assert "submit_dev_artifacts" in body
        assert "search_code_snippets" in body

    def test_admin_skills_content(self):
        body, _ = _load_skill("admin")
        assert "review_pending_list" in body
        assert "review_auto_process" in body
        assert "create_access_key" in body

    def test_reference_files_exist(self):
        for role in ("admin", "pm", "dev"):
            ref = _SKILLS_DIR / role / "REFERENCE.md"
            assert ref.exists()
            assert len(ref.read_text(encoding="utf-8")) > 0
