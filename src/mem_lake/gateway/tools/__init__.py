"""MCP 工具定义（按操作性质分组）。

工具注册入口：register_all_tools(mcp) 按操作性质注册 5 组工具。
每个分组模块实现 register_xxx_tools(mcp) 函数，注册该类型的所有工具。

工具只在一个文件中定义一次，角色访问权由 RBAC 中间件层控制（auth/rbac.py），
与工具文件归属无关。同一工具可被多个角色访问（如 get_role_skills 三角色共享）。

PDD 6.1 工具表分组：
- write_tools：写入类（产生审批批次）
  - publish_requirement, update_requirement_relations（PM）
  - submit_dev_artifacts（Dev）
- review_tools：审批类（Admin 专属）
  - review_pending_list, review_batch_detail, review_approve, review_reject
- manage_tools：管理类（Admin 专属）
  - manage_access_key, manage_project_profile
- search_tools：检索类（M6b 待实现）
  - search_similar_requirements, search_code_snippets, analyze_impact_scope,
    check_requirement_conflicts, list_knowledge
- query_tools：查询类（只读）
  - get_role_skills（已实现，三角色共享）
  - get_project_profile, get_requirement_context, query_audit_log（M6b 待实现）
"""

from fastmcp import FastMCP


def register_all_tools(mcp: FastMCP) -> None:
    """注册所有工具到 FastMCP 实例（按操作性质分组）。

    注册顺序无要求（FastMCP 内部按 name 索引），为可读性按类型分组注册。
    """
    from mem_lake.gateway.tools.manage_tools import register_manage_tools
    from mem_lake.gateway.tools.query_tools import register_query_tools
    from mem_lake.gateway.tools.review_tools import register_review_tools
    from mem_lake.gateway.tools.search_tools import register_search_tools
    from mem_lake.gateway.tools.write_tools import register_write_tools

    register_write_tools(mcp)
    register_review_tools(mcp)
    register_manage_tools(mcp)
    register_search_tools(mcp)
    register_query_tools(mcp)
