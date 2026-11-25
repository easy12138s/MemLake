"""RBAC 角色工具集映射测试。

纯单测，无 DB 依赖。验证三角色工具集映射对齐 PDD 3.5 / 6.1。
"""

import pytest

from mem_lake.auth.rbac import (
    ADMIN_ONLY_TOOLS,
    ADMIN_READ_TOOLS,
    ADMIN_TOOLS,
    DEV_TOOLS,
    PM_TOOLS,
    VALID_ROLES,
    get_tools_for_role,
    has_tool_access,
    validate_role,
)


def test_admin_is_management_plus_read():
    """admin 只保留管理类 + 查询类，不再等于三角色并集。"""
    assert ADMIN_TOOLS == ADMIN_ONLY_TOOLS | ADMIN_READ_TOOLS
    assert ADMIN_TOOLS != PM_TOOLS | DEV_TOOLS | ADMIN_ONLY_TOOLS


def test_admin_excludes_pm_dev_write_tools():
    """admin 不得持有 PM/Dev 的写入工具（admin 是管理角色，不代写）。"""
    for name in (
        "publish_requirement",
        "publish_requirements",
        "validate_requirements",
        "update_requirement_relations",
        "update_requirements",
        "update_node",
        "bind_requirement_to_project",
        "submit_dev_artifacts",
    ):
        assert name not in ADMIN_TOOLS, name


def test_admin_keeps_management_and_read():
    """admin 仍能审批、管密钥/system/画像、审计与查看需求。"""
    for name in (
        "review_approve",
        "review_submission",
        "list_access_keys",
        "manage_system",
        "manage_project_profile",
        "query_audit_log",
        "list_knowledge",
        "list_requirements",
        "get_requirement_context",
        "search_code_snippets",
        "get_skill",
    ):
        assert has_tool_access("admin", name) is True, name


def test_reindex_tools_admin_only():
    """reindex_project_vectors / get_reindex_status 仅 admin 可调用（不落入 pm/dev）。"""
    assert "reindex_project_vectors" in ADMIN_ONLY_TOOLS
    assert "get_reindex_status" in ADMIN_ONLY_TOOLS
    assert "reindex_project_vectors" not in PM_TOOLS
    assert "reindex_project_vectors" not in DEV_TOOLS


def test_graph_tools_admin_only():
    """ENH-01 三个图能力工具仅 admin 可调用（不落入 pm/dev）。"""
    graph_tools = {"get_graph_stats", "get_graph_quality_report", "generate_rule_edges"}
    assert graph_tools <= ADMIN_ONLY_TOOLS
    assert not (graph_tools & PM_TOOLS)
    assert not (graph_tools & DEV_TOOLS)


def test_pm_tools_exact():
    """PM_TOOLS 精确匹配工具名。

    批次三调整（工具面治理）：-get_role_skills（GitHub 分发）-get_project_profile
    （get_project_info 吸收）+list_requirements（清单枚举）。
    反馈跟进：+get_node_info（归属反查，ISSUE-14）。
    """
    expected = {
        "publish_requirement",
        "publish_requirements",
        "validate_requirements",
        "search_similar_requirements",
        "analyze_impact_scope",
        "check_requirement_conflicts",
        "update_requirement_relations",
        "update_node",
        "update_requirements",
        "list_requirements",
        "get_requirement_context",
        "get_node_info",
        "bind_requirement_to_project",
        "get_project_info",
        "get_submission",
        "get_requirement_graph",
        "get_skill",
    }
    assert PM_TOOLS == expected


def test_dev_tools_exact():
    """DEV_TOOLS 精确匹配工具名（批次三：-skills/-profile +list_requirements；
    反馈跟进：+get_node_info 归属反查）。
    角色×节点类型分权：-update_requirements（收为 PM 专属）
    +bind_requirement_to_project（提权给 Dev，仅可绑定到自己项目）。"""
    expected = {
        "list_requirements",
        "get_requirement_context",
        "get_node_info",
        "search_code_snippets",
        "submit_dev_artifacts",
        "update_node",
        "bind_requirement_to_project",
        "search_similar_requirements",
        "analyze_impact_scope",
        "get_project_info",
        "get_skill",
    }
    assert DEV_TOOLS == expected


def test_removed_tools_not_anywhere():
    """批次三删除的工具不得出现在任何角色工具集（防回流）。"""
    for name in ("get_role_skills", "get_project_profile"):
        assert name not in PM_TOOLS
        assert name not in DEV_TOOLS
        assert name not in ADMIN_ONLY_TOOLS


def test_list_requirements_shared():
    """list_requirements 为 pm/dev 共享（admin 经并集继承）。"""
    assert "list_requirements" in PM_TOOLS
    assert "list_requirements" in DEV_TOOLS


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


def test_update_node_only_for_pm_and_dev():
    """update_node 是 PM/Dev 共享写，admin 不持有。"""
    assert has_tool_access("pm", "update_node") is True
    assert has_tool_access("dev", "update_node") is True
    assert has_tool_access("admin", "update_node") is False


def test_rbac_tool_sets_after_role_split():
    """update_requirements 收为 PM 专属；bind_requirement_to_project 提权给 Dev。"""
    from mem_lake.auth.rbac import (
        ADMIN_TOOLS,
        DEV_TOOLS,
        PM_TOOLS,
        has_tool_access,
    )

    # update_requirements：PM 有、Dev 无
    assert has_tool_access("pm", "update_requirements") is True
    assert has_tool_access("dev", "update_requirements") is False

    # bind_requirement_to_project：PM 与 Dev 都有
    assert has_tool_access("pm", "bind_requirement_to_project") is True
    assert has_tool_access("dev", "bind_requirement_to_project") is True

    # admin 仍不持有任何写入工具
    assert has_tool_access("admin", "update_node") is False
    assert has_tool_access("admin", "update_requirements") is False
    assert has_tool_access("admin", "bind_requirement_to_project") is False
    assert has_tool_access("admin", "publish_requirement") is False

    # 工具数不漂移
    assert len(PM_TOOLS) == 17
    assert len(DEV_TOOLS) == 11
    assert len(ADMIN_TOOLS) == 32


def test_validate_role():
    """admin/pm/dev 返回 True，其他返回 False。"""
    assert validate_role("admin") is True
    assert validate_role("pm") is True
    assert validate_role("dev") is True
    assert validate_role("guest") is False
    assert VALID_ROLES == frozenset({"admin", "pm", "dev"})
