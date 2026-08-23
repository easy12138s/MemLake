"""Requirement 对调用者可见性的唯一判定函数。

统一口径，供以下四处调用，避免口径漂移：
1. write_tools._validate_dev_artifacts（dev 引用悬浮/跨项目需求）
2. write_tools._validate_requirement_refs（PM related / update_requirement_relations 引用）
3. Retrieve 侧 dev 可见过滤的组成依据（业务层组合）
4. approval._resolve_ref 落边前的目标可见性复核

可见规则（决策定稿）：
- admin：恒可见
- pm：需求.system_id ∈ 调用者 system_scope，或需求.project_id ∈ 调用者 project_scope
- dev：需求.project_id ∈ project_scope；或需求.system 含调用者任一 project（system_project 反查）；
  或需求.system_id ∈ system_scope
"""


from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.knowledge.models import SystemProject


async def is_requirement_visible(
    session: AsyncSession,
    *,
    req_node,
    role: str,
    project_scope: list[str],
    system_scope: list[str],
) -> bool:
    """判定 Requirement 节点对调用者（角色 + 两级 scope）是否可见。"""
    if role == "admin":
        return True

    req_project = req_node.project_id
    req_system = req_node.system_id

    if req_project is not None and str(req_project) in project_scope:
        return True

    if req_system is not None and str(req_system) in system_scope:
        return True

    # dev：需求 system 含调用者任一 project → 可见
    if role == "dev" and req_system is not None and project_scope:
        result = await session.execute(
            select(SystemProject.project_id).where(
                SystemProject.system_id == req_system
            )
        )
        sys_projects = {str(row[0]) for row in result}
        if sys_projects & set(project_scope):
            return True

    return False
