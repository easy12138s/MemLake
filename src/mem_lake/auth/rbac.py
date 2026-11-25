"""角色-工具集映射：admin/pm/dev 三角色的固定工具集配置。

纯 Python 常量字典，无 DB 查询，无 Casbin。工具名严格对齐 PDD 3.5 / 6.1。
M6 gateway 拦截层调用 has_tool_access 校验角色对工具的访问权限。

分层设计（读 / 写 / 管理）：

- COMMON_READ_TOOLS：三角色共享的查询工具
- PM_READ_TOOLS / DEV_READ_TOOLS：各自额外的查询面
- SHARED_WRITE_TOOLS：PM/Dev 共享的写入工具（admin 不持有）
- ADMIN_ONLY_TOOLS：admin 专属管理工具

admin 是管理与查询角色：ADMIN_TOOLS = ADMIN_ONLY_TOOLS | ADMIN_READ_TOOLS，
不再等于 PM ∪ Dev ∪ admin_only。PM/Dev 的写入工具（发布、更新、绑定、提交产物）
一律不给 admin——审批人不应同时是提交人，治理通过 review_* / list_knowledge /
query_audit_log 等完成。该收敛是 RBAC 行为变更：存量 admin Key 部署后立即
失去写工具，需要写入时请使用对应角色的 Key。

list_requirements 为清单式枚举工具（避免清单类任务靠检索试探取并集）。

manage_project_profile 为 PDD 3.4 + 8.5 要求的补充工具（admin 专属，直接写入
ProjectProfile 节点不走审批流），PDD 6.1 admin 工具表未列出但 PDD 3.4/8.5 明确要求。
"""

# 三角色共享的查询工具（只读）
COMMON_READ_TOOLS: frozenset[str] = frozenset({
    "list_requirements",
    "get_requirement_context",
    "get_node_info",
    "get_project_info",
    "search_similar_requirements",
    "analyze_impact_scope",
    # 技能文档按需读取（MCP Resource 的兜底工具）
    "get_skill",
})

# PM 额外查询面：冲突排查、提交时间线、需求关系图
PM_READ_TOOLS: frozenset[str] = COMMON_READ_TOOLS | {
    "check_requirement_conflicts",
    "get_submission",
    "get_requirement_graph",
}

# Dev 额外查询面：研发资产检索
DEV_READ_TOOLS: frozenset[str] = COMMON_READ_TOOLS | {"search_code_snippets"}

# admin 的查询面覆盖 PM 与 Dev（治理需要看得到），但不含任何写入
ADMIN_READ_TOOLS: frozenset[str] = PM_READ_TOOLS | DEV_READ_TOOLS

# PM/Dev 共享的写入工具（admin 不持有）
SHARED_WRITE_TOOLS: frozenset[str] = frozenset({
    "update_node",
    "bind_requirement_to_project",
})

# Dev 可通过 update_node 修改的研发资产节点类型（需求内容归 PM）
DEV_UPDATABLE_NODE_TYPES: frozenset[str] = frozenset({
    "CodeSnippet",
    "Solution",
    "DesignIntent",
    "Decision",
    "Pitfall",
})

# PM 专属写入工具（admin 不持有）
PM_WRITE_TOOLS: frozenset[str] = SHARED_WRITE_TOOLS | {
    "publish_requirement",
    "publish_requirements",
    "validate_requirements",
    "update_requirement_relations",
    "update_requirements",
}

PM_TOOLS: frozenset[str] = PM_READ_TOOLS | PM_WRITE_TOOLS

DEV_TOOLS: frozenset[str] = (
    DEV_READ_TOOLS | SHARED_WRITE_TOOLS | {"submit_dev_artifacts"}
)

ADMIN_ONLY_TOOLS: frozenset[str] = frozenset({
    "review_pending_list",
    "review_batch_detail",
    "review_approve",
    "review_reject",
    "review_auto_process",
    "review_submission",
    "list_knowledge",
    "query_audit_log",
    "create_access_key",
    "revoke_access_key",
    "list_access_keys",
    "update_access_key_scope",
    "rotate_access_key",
    "set_access_key_mode",
    "manage_system",
    "manage_project_profile",
    "reindex_project_vectors",
    "get_reindex_status",
    "get_graph_stats",
    "get_graph_quality_report",
    "generate_rule_edges",
})

# admin = 管理类 + 查询类（不含 PM/Dev 的写入工具）
ADMIN_TOOLS: frozenset[str] = ADMIN_ONLY_TOOLS | ADMIN_READ_TOOLS

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
