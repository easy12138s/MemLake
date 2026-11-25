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

    def test_version_1_20_0(self):
        """1.20.0：update_node 悬浮语义、角色×类型闸门、bind 边界、画像建档顺序。"""
        for role in ("admin", "pm", "dev"):
            _, version = _load_skill(role)
            assert version == "1.20.0"

    def test_reference_files_carry_version(self):
        """REFERENCE.md 也带 frontmatter 版本（get_skill 自证用）。"""
        for role in ("admin", "pm", "dev"):
            text = (_SKILLS_DIR / role / "REFERENCE.md").read_text(encoding="utf-8")
            parts = text.split("---", 2)
            frontmatter = yaml.safe_load(parts[1])
            assert str(frontmatter.get("version")) == "1.20.0"

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


def _read_reference(role: str) -> str:
    return (_SKILLS_DIR / role / "REFERENCE.md").read_text(encoding="utf-8")


class TestSkillsRoleSemantics:
    """角色×节点类型分权与悬浮语义在技能文档中的落点。"""

    def test_pm_skill_documents_floating_update(self):
        """PM 侧：update_node 的悬浮用法须写明。"""
        body, _ = _load_skill("pm")
        assert "update_node" in body
        assert "悬浮" in body
        assert "bind_requirement_to_project" in body

    def test_dev_skill_tool_list_matches_rbac(self):
        """Dev 侧：update_requirements 已收为 PM 专属；bind_requirement_to_project 已提权。"""
        body, _ = _load_skill("dev")
        assert "- **update_requirements**" not in body
        assert "- **bind_requirement_to_project**" in body

    def test_admin_skill_documents_project_onboarding_order(self):
        """Admin 侧：项目建档顺序（画像 → 挂载 → 授权）须写明。"""
        body, _ = _load_skill("admin")
        assert "manage_project_profile" in body
        assert "add_projects" in body

    def test_pm_reference_documents_optional_project_id(self):
        """PM REFERENCE：project_id 改为可选 + 四态诊断。"""
        text = _read_reference("pm")
        assert "留空 = 更新悬浮需求" in text
        assert "ProjectProfile" in text

    def test_dev_reference_documents_type_gate_and_bind_boundary(self):
        """Dev REFERENCE：类型闸门 + bind 边界（不可解绑/改绑）。"""
        text = _read_reference("dev")
        assert "PM 维护" in text
        assert "不可解绑" in text
        assert "仅可绑定悬浮需求" in text

    def test_admin_reference_documents_work_dir_required(self):
        """Admin REFERENCE：work_dir 必填 + 跨平台绝对路径。"""
        text = _read_reference("admin")
        assert "work_dir" in text
        assert "绝对路径" in text
