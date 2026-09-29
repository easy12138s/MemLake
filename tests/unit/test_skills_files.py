"""skills 文件内容测试（文件级，批次三起 skills 改 GitHub 分发）。

get_role_skills 工具已删除；SKILL.md/REFERENCE.md 作为 GitHub 分发材料保留，
其内容质量回归直接读文件断言（frontmatter 版本 / 关键工具引用 / 安装指南段）。
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

    def test_version_1_9_0(self):
        """1.11.0：批次四五——自动边四映射/system 维度检索/回执增强。"""
        for role in ("admin", "pm", "dev"):
            _, version = _load_skill(role)
            assert version == "1.11.0"

    def test_github_distribution_note(self):
        """文件含 GitHub 分发说明（raw URL 指引）。"""
        for role in ("admin", "pm", "dev"):
            body, _ = _load_skill(role)
            assert "raw.githubusercontent.com" in body


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
