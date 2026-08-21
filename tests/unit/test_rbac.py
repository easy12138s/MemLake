"""RBAC 角色工具集映射测试。

纯单测，无 DB 依赖。验证三角色工具集映射对齐 PDD 3.5 / 6.1。
"""

import pytest

from mem_lake.auth.rbac import (
    ADMIN_ONLY_TOOLS,
    ADMIN_TOOLS,
    DEV_TOOLS,
    PM_TOOLS,
    VALID_ROLES,
    get_tools_for_role,
    has_tool_access,
    validate_role,
)


def test_admin_has_all_tools():
    """admin 工具集等于 pm + dev + admin_only 的并集。"""
    assert ADMIN_TOOLS == PM_TOOLS | DEV_TOOLS | ADMIN_ONLY_TOOLS


def test_reindex_tools_admin_only():
    """reindex_project_vectors / get_reindex_status 仅 admin 可调用（不落入 pm/dev）。"""
    assert "reindex_project_vectors" in ADMIN_ONLY_TOOLS
    assert "get_reindex_status" in ADMIN_ONLY_TOOLS
    assert "reindex_project_vectors" not in PM_TOOLS
    assert "reindex_project_vectors" not in DEV_TOOLS
    assert "get_reindex_status" not in PM_TOOLS
    assert "get_reindex_status" not in DEV_TOOLS


def test_pm_tools_exact():
    """PM_TOOLS 精确匹配 PDD 3.5 的工具名（+ get_project_info）。"""
    expected = {
        "publish_requirement",
        "search_similar_requirements",
        "analyze_impact_scope",
        "check_requirement_conflicts",
        "update_requirement_relations",
        "get_project_profile",
        "get_requirement_context",
        "get_project_info",
        "get_role_skills",
    }
    assert PM_TOOLS == expected


def test_dev_tools_exact():
    """DEV_TOOLS 精确匹配 PDD 3.5 的工具名（+ get_project_info）。"""
    expected = {
        "get_project_profile",
        "get_requirement_context",
        "search_code_snippets",
        "submit_dev_artifacts",
        "search_similar_requirements",
        "analyze_impact_scope",
        "get_project_info",
        "get_role_skills",
    }
    assert DEV_TOOLS == expected


def test_get_tools_for_role_valid():
    """三角色返回非空 frozenset。"""
    for role in ("admin", "pm", "dev"):
        tools = get_tools_for_role(role)
        assert isinstance(tools, frozenset)
        assert len(tools) > 0


def test_get_tools_for_role_invalid_raises():
    """非法角色抛 ValueError。"""
    with pytest.raises(ValueError):
        get_tools_for_role("guest")


def test_has_tool_access_grant():
    """admin 调用 review_approve 返回 True。"""
    assert has_tool_access("admin", "review_approve") is True


def test_has_tool_access_deny():
    """dev 调用 review_approve 返回 False。"""
    assert has_tool_access("dev", "review_approve") is False


def test_validate_role():
    """admin/pm/dev 返回 True，其他返回 False。"""
    assert validate_role("admin") is True
    assert validate_role("pm") is True
    assert validate_role("dev") is True
    assert validate_role("guest") is False
    assert VALID_ROLES == frozenset({"admin", "pm", "dev"})
