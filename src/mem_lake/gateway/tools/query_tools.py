"""查询类工具：读取知识图谱内容与系统元信息（只读，不产生审批批次）。

工具职责：转发 knowledge/repository 与 audit/service 的只读查询，不写业务逻辑。

包含工具（PDD 6.1）：
- get_role_skills（三角色共享）：获取角色 Skills 指导文档
- get_project_info（PM/Dev/Admin）：枚举/查询项目画像 + scope 自证
- get_project_profile（PM/Dev/Admin）：查询项目画像（ProjectProfile 节点）
- get_requirement_context（PM/Dev/Admin）：查询需求上下文（关联节点+关系链）
- query_audit_log（Admin）：查询审计日志

设计要点：
- 全部为只读工具（READ_TOOL_ANNOTATIONS）
- 角色 RBAC 由中间件层控制，本文件不区分角色
- get_role_skills 为元工具，指导 Agent 如何使用工具集
- get_project_profile 直接调 repository.list_nodes_by_project 过滤 ProjectProfile
- get_requirement_context 调 graph.traverse 获取需求关联节点
- query_audit_log 调 audit.service.query_audit_logs 多条件过滤
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from pydantic import BaseModel, Field

from mem_lake.audit.service import query_audit_logs
from mem_lake.gateway.dependencies import (
    get_current_project_scope,
    get_current_role,
    get_readonly_session,
    validate_project_access,
    validate_system_access,
)
from mem_lake.gateway.tools._shared import (
    INSTALLATION_GUIDE,
    READ_TOOL_ANNOTATIONS,
    ROLE_SKILLS_MD,
    ROLE_SKILLS_VERSION,
    to_tool_error,
)
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    get_node,
    list_nodes_by_project,
    list_project_profiles,
)

logger = logging.getLogger("mem_lake.gateway.tools.query")


# ============================================================================
# 输出模型
# ============================================================================


class GetRoleSkillsOutput(BaseModel):
    """get_role_skills 工具出参。"""

    role: str = Field(description="角色：admin/pm/dev")
    skills_markdown: str = Field(
        description="角色 Skills 指导文档（Markdown 格式，可直接保存为 SKILL.md）"
    )
    version: str = Field(description="Skills 文档版本")
    installation_guide: str = Field(
        description="常见 Agent 的 skills 文件放置目录格式（Claude Code/Cursor/Codex CLI/Gemini CLI）"
    )


class ProjectProfileOutput(BaseModel):
    """项目画像详情。"""

    node_id: uuid.UUID = Field(description="节点 ID")
    title: str = Field(description="项目名称标题")
    content: str = Field(description="项目描述")
    properties: dict[str, Any] = Field(default={}, description="项目属性")
    tags: list[str] = Field(default=[], description="标签数组")
    version: int = Field(description="版本号")
    created_at: Any = Field(description="创建时间（ISO 8601）")
    created_by: str = Field(description="创建者")


class GetProjectProfileOutput(BaseModel):
    """get_project_profile 工具出参。"""

    project_id: uuid.UUID = Field(description="项目 ID")
    profile: ProjectProfileOutput | None = Field(
        default=None, description="项目画像（None 表示尚未创建）"
    )


class ProjectInfo(BaseModel):
    """单个项目摘要信息（get_project_info 返回单元）。"""

    project_id: uuid.UUID = Field(description="项目 ID")
    name: str = Field(description="项目名称（ProjectProfile.title）")
    work_dir: str | None = Field(default=None, description="项目本地工作目录")
    repo: str | None = Field(default=None, description="代码仓库标识/名称")
    description: str = Field(description="项目描述（ProjectProfile.content）")
    tags: list[str] = Field(default=[], description="标签数组")
    updated_at: Any = Field(description="更新时间（ISO 8601，取画像节点 created_at）")
    profile: dict[str, Any] | None = Field(
        default=None,
        description="完整画像属性（仅 include_profile=true 时返回，否则 null）",
    )


class ScopeMeta(BaseModel):
    """key 可见范围自证信息（include_scope_meta=true 时返回）。"""

    scope_type: str = Field(description="范围类型：all（admin 不受限）/ scoped（受限）")
    visible_count: int = Field(description="可见项目数量")
    visible_uuids: list[str] = Field(
        default=[], description="可见项目 UUID 列表（scope_type=all 时为空）"
    )


class GetProjectInfoOutput(BaseModel):
    """get_project_info 工具出参。"""

    action: str = Field(description="list / get")
    scope: ScopeMeta | None = Field(
        default=None, description="仅 include_scope_meta=true 时返回"
    )
    projects: list[ProjectInfo] = Field(default=[], description="list 结果数组")
    project: ProjectInfo | None = Field(
        default=None, description="get 结果（无画像时为 null）"
    )


class RelatedNodeOutput(BaseModel):
    """关联节点项。

    edge_type 为从需求节点到该关联节点的路径边类型列表（每一跳一个），
    depth 为路径跳数（1=直接关联）。图遍历为无向，direction 无第一语义，故不提供。
    """
    node_id: uuid.UUID = Field(description="节点 ID")
    title: str = Field(description="节点标题")
    content: str = Field(description="节点摘要（前 200 字符）")
    node_type: str = Field(description="节点类型")
    edge_type: list[str] = Field(
        description="关联路径上的边类型列表（从需求出发每一跳一个；无向图无方向概念）"
    )
    depth: int = Field(description="与起点的跳数距离（1=直接关联）")


class RequirementContextOutput(BaseModel):
    """get_requirement_context 工具出参。"""

    requirement_id: uuid.UUID = Field(description="需求节点 ID")
    requirement: dict[str, Any] | None = Field(
        default=None, description="需求节点详情（None 表示不存在）"
    )
    related_nodes: list[RelatedNodeOutput] = Field(
        default=[], description="关联节点列表（按深度排序）"
    )
    total: int = Field(description="关联节点数量")


class AuditLogItemOutput(BaseModel):
    """审计日志项。"""

    log_id: uuid.UUID = Field(description="日志 ID")
    actor: str = Field(description="操作者")
    action: str = Field(description="操作类型：write/update/archive")
    target_type: str = Field(description="目标类型：node/edge")
    target_id: uuid.UUID | None = Field(default=None, description="目标 ID")
    detail: dict[str, Any] = Field(default={}, description="操作详情")
    created_at: Any = Field(description="操作时间（ISO 8601）")


class QueryAuditLogOutput(BaseModel):
    """query_audit_log 工具出参。"""

    logs: list[AuditLogItemOutput] = Field(description="审计日志列表")
    total: int = Field(description="返回数量（非总数）")
    limit: int = Field(description="当前分页上限")
    offset: int = Field(description="当前分页偏移")


# ============================================================================
# 工具注册
# ============================================================================


def register_query_tools(mcp: FastMCP) -> None:
    """注册查询类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_role_skills(
        role: str | None = Field(
            default=None,
            description="指定角色（admin/pm/dev），None 表示返回当前调用者角色",
        ),
    ) -> GetRoleSkillsOutput:
        """获取角色 Skills 指导文档（Markdown 格式，可直接保存为 SKILL.md）。

        共享工具（三角色均可调用）。返回当前角色或指定角色的 Skills 文档，
        指导 Agent 如何使用 Mem Lake 工具集。返回值含 installation_guide 字段，
        指导如何将 Skills 文件放置到对应 Agent 目录（首推跨客户端项目级
        `.agents/skills/mem-lake-{role}/SKILL.md`，并列出 Claude Code/Cursor/Codex CLI/Gemini CLI）。
        首次接入 MemLake 时请先调用本工具，取 skills_markdown 按其指引安装技能，
        安装后刷新/重启会话即可生效。
        """
        target_role = role or get_current_role()
        if target_role not in ROLE_SKILLS_MD:
            raise to_tool_error(
                ValueError(f"未知角色: {target_role}，合法角色: admin/pm/dev")
            )
        return GetRoleSkillsOutput(
            role=target_role,
            skills_markdown=ROLE_SKILLS_MD[target_role],
            version=ROLE_SKILLS_VERSION,
            installation_guide=INSTALLATION_GUIDE,
        )

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_project_profile(
        project_id: uuid.UUID = Field(description="项目 ID"),
    ) -> GetProjectProfileOutput:
        """查询项目画像（技术栈/架构/约定/团队）。

        PM/Dev/Admin 共享工具。返回项目最新的 ProjectProfile 节点。
        项目尚未创建画像时 profile=None，可调用 manage_project_profile（admin）创建。
        """
        try:
            validate_project_access(project_id)
            session = await get_readonly_session()
            try:
                nodes = await list_nodes_by_project(
                    session,
                    project_id=project_id,
                    node_type="ProjectProfile",
                    status="approved",
                    limit=1,
                    offset=0,
                )
                profile = None
                if nodes:
                    n = nodes[0]
                    profile = ProjectProfileOutput(
                        node_id=n.id,
                        title=n.title,
                        content=n.content,
                        properties=n.properties or {},
                        tags=n.tags or [],
                        version=n.version,
                        created_at=n.created_at.isoformat()
                        if n.created_at
                        else None,
                        created_by=n.created_by,
                    )
                return GetProjectProfileOutput(
                    project_id=project_id, profile=profile
                )
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_project_info(
        action: Literal["list", "get"] = Field(
            description="操作类型：list 枚举当前 key 可见项目 / get 查询单个项目"
        ),
        project_id: uuid.UUID | None = Field(
            default=None, description="get 时必填的项目 ID"
        ),
        include_profile: bool = Field(
            default=False,
            description="为 true 时在每个项目结果附完整画像属性（properties）",
        ),
        include_scope_meta: bool = Field(
            default=False,
            description="为 true 时附 scope 自证信息（scope_type/visible_count/visible_uuids）",
        ),
    ) -> GetProjectInfoOutput:
        """枚举/查询项目画像（PM/Dev/Admin 共享，只读）。

        list：枚举当前 key 可见的项目（admin 全量；pm/dev 仅 scope 内），
        每项含 name/work_dir/repo/description/tags/updated_at。
        get：按 project_id 查询单个项目；越权（pm/dev 访问 scope 外）返回权限拒绝错误。
        include_scope_meta=true 回显 key 的可见范围，用于自证项目隔离边界。
        同一项目存在多个画像节点时取最新一条。
        """
        try:
            role = get_current_role()
            scope = get_current_project_scope()
            session = await get_readonly_session()
            try:
                return await _get_project_info_core(
                    action=action,
                    project_id=project_id,
                    include_profile=include_profile,
                    include_scope_meta=include_scope_meta,
                    role=role,
                    scope=scope,
                    list_fn=lambda **kw: list_project_profiles(session, **kw),
                    validate_fn=validate_project_access,
                )
            finally:
                await session.close()
        except (ValueError, ToolError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_requirement_context(
        requirement_id: uuid.UUID = Field(description="需求节点 ID"),
        depth: int = Field(
            default=2,
            description="关系链遍历深度（1=直接关联，2=间接关联，最大 5）",
        ),
    ) -> RequirementContextOutput:
        """查询需求上下文（关联的代码/方案/意图/踩坑节点）。

        PM/Dev/Admin 共享工具。基于图遍历获取需求节点的关联节点列表（按关联距离大致排序）。
        每个关联节点返回真实的 edge_type（从需求到该节点的路径边类型列表）与 depth（跳数）；
        图遍历为无向，故不提供 direction。需求节点不存在时 requirement=None，related_nodes 为空。
        """
        try:
            # 深度校验（1~5）
            if not 1 <= depth <= 5:
                raise ValueError("depth 必须在 1~5 之间")

            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context

            session = await get_readonly_session()
            try:
                # 1. 获取需求节点详情
                requirement_dict = None
                try:
                    req_node = await get_node(session, requirement_id)
                    requirement_dict = {
                        "node_id": str(req_node.id),
                        "title": req_node.title,
                        "content": req_node.content,
                        "type": req_node.type,
                        "project_id": str(req_node.project_id) if req_node.project_id else None,
                        "system_id": str(req_node.system_id) if req_node.system_id else None,
                        "status": req_node.status,
                        "version": req_node.version,
                        "tags": req_node.tags or [],
                    }
                    # 需求存在则校验权限：有项目按项目；悬浮需求按 system
                    if req_node.project_id is not None:
                        validate_project_access(req_node.project_id)
                    elif req_node.system_id is not None:
                        validate_system_access(req_node.system_id)
                except NodeNotFoundError:
                    return RequirementContextOutput(
                        requirement_id=requirement_id,
                        requirement=None,
                        related_nodes=[],
                        total=0,
                    )

                # 2. 图遍历获取关联节点
                from mem_lake.search.graph import GraphSearcher
                graph_searcher = GraphSearcher(lifespan_ctx.graph_store)
                from mem_lake.search.filters import FilterSpec
                filters = FilterSpec(
                    project_id=req_node.project_id,
                )
                results = await graph_searcher.context_traverse(
                    session,
                    requirement_id,
                    depth=depth,
                    filters=filters,
                )

                # 3. 转换为 RelatedNodeOutput（context_traverse 已透出真实 edge_type/depth）
                related = [
                    RelatedNodeOutput(
                        node_id=r.node_id,
                        title=r.title,
                        content=r.content,
                        node_type=r.node_type,
                        edge_type=r.edge_types or [],
                        depth=r.graph_depth or 1,
                    )
                    for r in results
                ]

                return RequirementContextOutput(
                    requirement_id=requirement_id,
                    requirement=requirement_dict,
                    related_nodes=related,
                    total=len(related),
                )
            finally:
                await session.close()
        except (NodeNotFoundError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def query_audit_log(
        project_id: uuid.UUID | None = Field(
            default=None, description="项目 ID 过滤（None 表示所有项目）"
        ),
        actor: str | None = Field(
            default=None, description="操作者 Access Key ID 过滤"
        ),
        action: str | None = Field(
            default=None,
            description="操作类型过滤：write/update/archive",
        ),
        target_type: str | None = Field(
            default=None, description="目标类型过滤：node/edge"
        ),
        target_id: uuid.UUID | None = Field(
            default=None, description="目标 ID 过滤"
        ),
        start_time: datetime | None = Field(
            default=None, description="起始时间（ISO 8601）"
        ),
        end_time: datetime | None = Field(
            default=None, description="结束时间（ISO 8601）"
        ),
        limit: int = Field(default=100, description="返回数量上限"),
        offset: int = Field(default=0, description="分页偏移"),
    ) -> QueryAuditLogOutput:
        """查询审计日志（多条件过滤 + 分页）。

        Admin 工具。审计日志为 append-only，记录所有知识图谱写操作。
        支持按项目/操作者/操作类型/目标类型/目标 ID/时间范围过滤。
        """
        try:
            # 项目权限校验（admin 不受限于 project_id）
            if project_id is not None:
                validate_project_access(project_id)

            session = await get_readonly_session()
            try:
                logs = await query_audit_logs(
                    session,
                    actor=actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    project_id=project_id,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                    offset=offset,
                )

                return QueryAuditLogOutput(
                    logs=[_to_audit_log_item_output(log) for log in logs],
                    total=len(logs),
                    limit=limit,
                    offset=offset,
                )
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e)


# ============================================================================
# 辅助函数
# ============================================================================


def _to_audit_log_item_output(log) -> AuditLogItemOutput:
    """从 AuditLog ORM 对象构造 AuditLogItemOutput。"""
    return AuditLogItemOutput(
        log_id=log.id,
        actor=log.actor,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        detail=log.detail or {},
        created_at=log.created_at.isoformat() if log.created_at else None,
    )


def _to_project_info(node, include_profile: bool = False) -> ProjectInfo:
    """从 ProjectProfile 节点构造 ProjectInfo。

    name 优先取 properties.name（业务项目名），缺省回退 node.title，
    避免列表中的 name 与画像内部 name 语义割裂。
    """
    props = node.properties or {}
    return ProjectInfo(
        project_id=node.project_id,
        name=props.get("name", node.title),
        work_dir=props.get("work_dir"),
        repo=props.get("repo"),
        description=node.content,
        tags=node.tags or [],
        updated_at=node.created_at.isoformat() if node.created_at else None,
        profile=props if include_profile else None,
    )


def _build_scope_meta(is_admin: bool, scope: list[str], projects: list) -> ScopeMeta:
    """构造 scope 自证信息。

    admin（不受限）：scope_type="all"，visible_uuids 置空，visible_count 取实际可见项目数。
    非 admin：scope_type="scoped"，visible_uuids 为 scope 列表，visible_count 为 scope 长度。
    """
    if is_admin:
        return ScopeMeta(
            scope_type="all", visible_count=len(projects), visible_uuids=[]
        )
    return ScopeMeta(
        scope_type="scoped", visible_count=len(scope), visible_uuids=list(scope)
    )


async def _get_project_info_core(
    *,
    action: str,
    project_id: uuid.UUID | None,
    include_profile: bool,
    include_scope_meta: bool,
    role: str,
    scope: list[str],
    list_fn,
    validate_fn,
) -> GetProjectInfoOutput:
    """get_project_info 的核心逻辑（与 FastMCP 上下文解耦，便于单测）。

    list_fn(session 无关)：list_project_profiles 的封装（接收 project_ids/limit/offset）。
    validate_fn：validate_project_access 的封装（越权抛 ToolError）。
    """
    is_admin = role == "admin"

    if action == "list":
        visible_ids = None if is_admin else [uuid.UUID(s) for s in scope]
        nodes = await list_fn(project_ids=visible_ids)
        # 同 project_id 去重（created_at desc 已排序，取首条）
        seen: dict[uuid.UUID, ProjectInfo] = {}
        for n in nodes:
            if n.project_id in seen:
                continue
            seen[n.project_id] = _to_project_info(n, include_profile)
        projects = list(seen.values())
        scope_meta = (
            _build_scope_meta(is_admin, scope, projects) if include_scope_meta else None
        )
        return GetProjectInfoOutput(action="list", scope=scope_meta, projects=projects)

    elif action == "get":
        if project_id is None:
            raise ValueError("get 操作必须指定 project_id")
        validate_fn(project_id)  # 越权抛 ToolError
        nodes = await list_fn(project_ids=[project_id], limit=1)
        project = _to_project_info(nodes[0], include_profile) if nodes else None
        scope_meta = (
            _build_scope_meta(is_admin, scope, [project] if project else [])
            if include_scope_meta
            else None
        )
        return GetProjectInfoOutput(action="get", scope=scope_meta, project=project)

    raise ValueError(f"未知 action: {action}")
