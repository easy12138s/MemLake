"""检索类工具：基于三引擎融合（向量/全文/图）的智能检索（只读）。

工具职责：转发 search 模块的三引擎融合检索，不在工具层写业务逻辑。

包含工具（PDD 6.1，M6b 待实现）：
- search_similar_requirements（PM/Dev）：向量+全文检索相似需求
- search_code_snippets（Dev）：向量+全文检索代码片段
- analyze_impact_scope（PM/Dev）：图检索分析变更影响范围
- check_requirement_conflicts（PM）：向量检索检测需求冲突
- list_knowledge（Admin）：分页列出项目知识节点

设计要点：
- 全部为只读工具（READ_TOOL_ANNOTATIONS）
- 角色 RBAC 由中间件层控制，本文件不区分角色
- 三引擎融合检索委托给 search/fusion.py（RRF 算法）
- M6b 阶段实现，当前为占位
"""

import logging

from fastmcp import FastMCP

logger = logging.getLogger("mem_lake.gateway.tools.search")


def register_search_tools(mcp: FastMCP) -> None:
    """注册检索类工具到 FastMCP 实例。

    M6b 阶段实现，当前为占位。实现时在此函数内注册 5 个检索工具：
    search_similar_requirements / search_code_snippets / analyze_impact_scope /
    check_requirement_conflicts / list_knowledge。
    """
    # M6b 待实现，暂无注册内容
    return None
