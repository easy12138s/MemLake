"""角色-工具集映射：admin/pm/dev 三角色的固定工具集配置。

纯 Python 常量字典，无 DB 查询，无 Casbin。工具名严格对齐 PDD 3.5 / 6.1。
M6 gateway 拦截层调用 has_tool_access 校验角色对工具的访问权限。
"""

PM_TOOLS: frozenset[str] = frozenset({
    "publish_requirement",
    "search_similar_requirements",
    "analyze_impact_scope",
    "check_requirement_conflicts",
    "update_requirement_relations",
    "get_project_profile",
    "get_requirement_context",
    "get_role_skills",
})

DEV_TOOLS: frozenset[str] = frozenset({
    "get_project_profile",
    "get_requirement_context",
    "search_code_snippets",
    "submit_dev_artifacts",
    "search_similar_requirements",
    "analyze_impact_scope",
    "get_role_skills",
})

ADMIN_ONLY_TOOLS: frozenset[str] = frozenset({
    "review_pending_list",
    "review_batch_detail",
    "review_approve",
    "review_reject",
    "list_knowledge",
    "query_audit_log",
    "manage_access_key",
})

# admin 拥有全部工具（含 pm 与 dev 工具集）
ADMIN_TOOLS: frozenset[str] = PM_TOOLS | DEV_TOOLS | ADMIN_ONLY_TOOLS

ROLE_TOOLSET: dict[str, frozenset[str]] = {
    "admin": ADMIN_TOOLS,
    "pm": PM_TOOLS,
    "dev": DEV_TOOLS,
}

VALID_ROLES: frozenset[str] = frozenset(ROLE_TOOLSET.keys())


def get_tools_for_role(role: str) -> frozenset[str]:
    """返回指定角色绑定的工具集。非法角色抛 ValueError。"""
    if role not in ROLE_TOOLSET:
        raise ValueError(f"非法角色: {role}，合法角色: {VALID_ROLES}")
    return ROLE_TOOLSET[role]


def has_tool_access(role: str, tool_name: str) -> bool:
    """校验角色是否有权访问指定工具。"""
    return tool_name in get_tools_for_role(role)


def validate_role(role: str) -> bool:
    """校验角色是否合法。"""
    return role in VALID_ROLES
