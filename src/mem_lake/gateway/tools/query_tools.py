"""查询类工具：读取知识图谱内容与系统元信息（只读，不产生审批批次）。

工具职责：转发 knowledge/repository 与 audit/service 的只读查询，不写业务逻辑。

包含工具：
- list_requirements（PM/Dev/Admin）：清单式枚举需求（分页+module/source_doc 过滤）
- get_project_info（PM/Dev/Admin）：枚举/查询项目画像 + scope 自证
- get_requirement_context（PM/Dev/Admin）：查询需求上下文（关联节点+关系链）
- query_audit_log（Admin）：查询审计日志

设计要点：
- 全部为只读工具（READ_TOOL_ANNOTATIONS）
- 角色 RBAC 由中间件层控制，本文件不区分角色
- list_requirements 直查 repository.list_requirements（JSONB 属性过滤 + 分页 total）
- get_requirement_context 调 graph.traverse 获取需求关联节点
- query_audit_log 调 audit.service.query_audit_logs 多条件过滤

批次三工具面治理：get_role_skills 已删除（skills 改 GitHub 分发，见
manage_tools._build_user_hint）；get_project_profile 已删除（get_project_info
的 action=get + include_profile=true 完整覆盖）。
"""

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from mem_lake.audit.service import query_audit_logs
from mem_lake.gateway.dependencies import (
    get_current_project_scope,
    get_current_role,
    get_current_system_scope,
    get_readonly_session,
    validate_project_access,
    validate_system_access,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    get_lifespan_context,
    resolve_search_scope_fallback,
    to_tool_error,
)
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    get_node,
    list_project_profiles,
    list_systems,
)
from mem_lake.knowledge.repository import (
    list_requirements as list_requirements_repo,
)
from mem_lake.search.graph import GraphSearcher

if TYPE_CHECKING:
    # 仅类型标注用：_to_audit_log_item_output 接收 AuditLog ORM 对象
    from mem_lake.audit.models import AuditLog

logger = logging.getLogger("mem_lake.gateway.tools.query")


# ============================================================================
# 输出模型
# ============================================================================


class ProjectInfo(BaseModel):
    """项目摘要（name/description/updated_at 为 None=scope 内但尚未建画像）。"""

    project_id: uuid.UUID = Field(description="项目 ID")
    name: str | None = Field(default=None, description="项目名（None=未建画像）")
    work_dir: str | None = Field(default=None, description="工作目录")
    repo: str | None = Field(default=None, description="仓库标识")
    description: str | None = Field(default=None, description="描述")
    tags: list[str] = Field(default=[], description="标签")
    updated_at: Any = Field(default=None, description="更新时间")
    profile: dict[str, Any] | None = Field(
        default=None, description="完整画像（include_profile=true 时）"
    )
    has_profile: bool = Field(
        default=True,
        description="false=scope 内但尚未创建画像（占位条目）——admin 可用 "
        "manage_project_profile 补建以暴露 name/work_dir/repo",
    )


class VisibleSystemInfo(BaseModel):
    """可见 system 摘要。"""

    system_id: str = Field(description="ID")
    name: str = Field(description="名称")
    code: str | None = Field(default=None, description="code（可空）")


class RequirementListItem(BaseModel):
    """list_requirements 返回单元（条目不含正文，详情走 get_requirement_context）。"""

    node_id: uuid.UUID = Field(description="节点 ID")
    title: str = Field(description="需求标题")
    module: str | None = Field(default=None, description="模块（properties.module）")
    priority: str | None = Field(default=None, description="优先级")
    requirement_key: str | None = Field(
        default=None, description="需求主键（如 SYS-0001；悬浮未分配为 None）"
    )
    source_doc: str | None = Field(
        default=None, description="来源文档路径（存量导入的原始相对路径）"
    )


class ListRequirementsOutput(BaseModel):
    """list_requirements 工具出参。"""

    total: int = Field(description="命中总数（跨页不变，非本页条数）")
    limit: int = Field(description="页大小")
    offset: int = Field(description="分页偏移")
    items: list[RequirementListItem] = Field(description="需求条目列表")


class ScopeMeta(BaseModel):
    """key 可见范围自证信息（include_scope_meta=true 时返回）。"""

    scope_type: str = Field(description="范围类型：all（admin 不受限）/ scoped（受限）")
    visible_count: int = Field(description="可见项目数量")
    visible_uuids: list[str] = Field(
        default=[], description="可见项目 UUID 列表（scope_type=all 时为空）"
    )
    system_scope_type: str = Field(
        default="all", description="system 维度范围类型：all（admin）/ scoped"
    )
    visible_system_count: int = Field(default=0, description="可见 system 数量")
    visible_systems: list[VisibleSystemInfo] = Field(
        default=[], description="可见 system 列表（id+name+code）"
    )


class GetProjectInfoOutput(BaseModel):
    """get_project_info 工具出参。"""

    hint: str | None = Field(
        default=None,
        description="输出级提示（如存在无画像的占位项目时引导补建）",
    )
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
    async def list_requirements(
        project_id: uuid.UUID | None = Field(
            default=None, description="归属项目 ID（与 system_id 均不传时按 Key 绑定 system 兜底）"
        ),
        system_id: uuid.UUID | None = Field(
            default=None, description="归属 system 域（枚举该系统全部需求，含悬浮）"
        ),
        module: str | None = Field(
            default=None, description="module 精确过滤（properties.module）"
        ),
        source_doc_prefix: str | None = Field(
            default=None,
            description="source_doc 前缀过滤，如 '云HIS-二期-v2.15优化-' 圈定一个批次",
        ),
        tags: list[str] | None = Field(default=None, description="标签过滤"),
        tags_op: str = Field(
            default="all", description="标签语义：all=AND（默认）/any=OR"
        ),
        requirement_key: str | None = Field(
            default=None,
            description="需求主键精确直查（如 SYS-1416），已知 key 时免翻分页",
        ),
        fields: list[str] | None = Field(
            default=None,
            description="出参字段白名单裁剪（可裁：module/priority/requirement_key/"
            "source_doc；node_id/title 恒回）——大结果集只回必要字段省 token",
        ),
        limit: int = Field(default=50, description="页大小，默认 50，上限 200"),
        offset: int = Field(default=0, description="分页偏移"),
    ) -> ListRequirementsOutput:
        """清单式枚举 Requirement（分页 + 属性过滤），返回命中总数 total。

        「列出某项目/系统的全部需求」「按 module 或 source_doc 前缀圈定批次」
        「按 requirement_key 直查」用本工具，不要用检索工具反复试探再人工取
        并集。条目不含正文；要看某条详情用 get_requirement_context。
        """
        try:
            project_id, system_id = resolve_search_scope_fallback(
                get_current_role(),
                get_current_system_scope(),
                project_id=project_id,
                system_id=system_id,
            )
            if project_id is not None:
                validate_project_access(project_id)
            if system_id is not None:
                validate_system_access(system_id)
            # fields 白名单校验（防注入：非出参字段直接拒绝）
            allowed_fields = set(RequirementListItem.model_fields)
            if fields is not None:
                unknown = set(fields) - allowed_fields
                if unknown:
                    raise ValueError(
                        f"非法 fields: {sorted(unknown)}，可选: {sorted(allowed_fields)}"
                    )

            session = await get_readonly_session()
            try:
                rows, total = await list_requirements_repo(
                    session,
                    project_id=project_id,
                    system_id=system_id,
                    module=module,
                    source_doc_prefix=source_doc_prefix,
                    requirement_key=requirement_key,
                    tags=tuple(tags) if tags else None,
                    tags_op=tags_op,
                    limit=min(max(limit, 1), 200),
                    offset=max(offset, 0),
                )
                # node_id/title 为恒回字段（引用键+可读名，报告 P1-4 曾批评
                # "只回 UUID 没名字"），fields 只裁可选维度
                keep = (set(fields) | {"node_id", "title"}) if fields is not None else None

                def _item(r: Any) -> RequirementListItem:
                    payload = {
                        "node_id": r.id,
                        "title": r.title,
                        "module": (r.properties or {}).get("module"),
                        "priority": (r.properties or {}).get("priority"),
                        "requirement_key": r.requirement_key,
                        "source_doc": (r.properties or {}).get("source_doc"),
                    }
                    if keep is not None:
                        payload = {k: v for k, v in payload.items() if k in keep}
                    return RequirementListItem(**payload)

                return ListRequirementsOutput(
                    total=total,
                    limit=min(max(limit, 1), 200),
                    offset=offset,
                    items=[_item(r) for r in rows],
                )
            finally:
                await session.close()
        except (ValueError, ToolError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def get_project_info(
        action: Literal["list", "get"] = Field(
            description="操作类型：list 枚举当前 key 可见项目 / get 查询单个项目"
        ),
        project_id: uuid.UUID | None = Field(
            default=None, description="get 时必填的项目 ID"
        ),
        include_profile: bool = Field(
            default=False, description="为 true 时附完整画像属性（properties）"
        ),
        include_scope_meta: bool = Field(
            default=False,
            description="为 true 时附 scope 自证（project 维度 + system 维度 visible_systems）",
        ),
    ) -> GetProjectInfoOutput:
        """枚举/查询项目画像（admin 全量；pm/dev 仅 scope 内，只读）。

        get 越权返回权限拒绝；scope 内无画像的项目返回占位条目（name=None）。
        同一项目多画像时取最新一条。
        """
        try:
            role = get_current_role()
            scope = get_current_project_scope()
            system_scope = get_current_system_scope()
            session = await get_readonly_session()
            try:
                return await _get_project_info_core(
                    action=action,
                    project_id=project_id,
                    include_profile=include_profile,
                    include_scope_meta=include_scope_meta,
                    role=role,
                    scope=scope,
                    system_scope=system_scope,
                    list_fn=lambda **kw: list_project_profiles(session, **kw),
                    validate_fn=validate_project_access,
                    list_systems_fn=lambda: list_systems(session),
                )
            finally:
                await session.close()
        except (ValueError, ToolError) as e:
            raise to_tool_error(e) from e

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

            lifespan_ctx = get_lifespan_context()

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
                        "requirement_key": req_node.requirement_key,
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
            raise to_tool_error(e) from e

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
            raise to_tool_error(e) from e


# ============================================================================
# 辅助函数
# ============================================================================


def _to_audit_log_item_output(log: "AuditLog") -> AuditLogItemOutput:
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


def _to_project_info(node: KnowledgeNode, include_profile: bool = False) -> ProjectInfo:
    """从 ProjectProfile 节点构造 ProjectInfo。

    name 优先取 properties.name（业务项目名），缺省回退 node.title，
    避免列表中的 name 与画像内部 name 语义割裂。
    """
    props = node.properties or {}
    # ProjectProfile 节点必归属项目（画像链路 project_id 恒有值），供 out 模型非空字段
    assert node.project_id is not None
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


def _build_scope_meta(
    is_admin: bool,
    scope: list[str],
    projects: list[ProjectInfo],
    *,
    system_scope: list[str],
    systems: list[Any],
) -> ScopeMeta:
    """构造 scope 自证信息（含 project 与 system 双维度）。

    - admin（不受限）：scope_type="all"，visible_uuids 置空，visible_count 取项目数；
      system 维度 system_scope_type="all"，可见 system = 全量 systems（不按 claims 过滤）
    - 非 admin：scope_type="scoped"，project 维度回显 claims；system 维度按 claims
      的 system_scope 过滤出对应 System（dev/pm 可查可见 system 自证边界）
    """
    visible_systems = (
        systems
        if is_admin
        else [s for s in systems if str(s.id) in set(system_scope)]
    )
    base_fields: dict[str, Any] = {
        "system_scope_type": "all" if is_admin else "scoped",
        "visible_system_count": len(visible_systems),
        "visible_systems": [
            VisibleSystemInfo(
                system_id=str(s.id), name=s.name, code=s.code
            )
            for s in visible_systems
        ],
    }
    if is_admin:
        return ScopeMeta(
            scope_type="all", visible_count=len(projects), visible_uuids=[],
            **base_fields,
        )
    return ScopeMeta(
        scope_type="scoped", visible_count=len(scope), visible_uuids=list(scope),
        **base_fields,
    )


async def _get_project_info_core(
    *,
    action: str,
    project_id: uuid.UUID | None,
    include_profile: bool,
    include_scope_meta: bool,
    role: str,
    scope: list[str],
    list_fn: Callable[..., Awaitable[list[KnowledgeNode]]],
    validate_fn: Callable[[uuid.UUID], None],
    system_scope: list[str] | None = None,
    list_systems_fn: Callable[[], Awaitable[list[Any]]] | None = None,
) -> GetProjectInfoOutput:
    """get_project_info 的核心逻辑（与 FastMCP 上下文解耦，便于单测）。

    list_fn(session 无关)：list_project_profiles 的封装（接收 project_ids/limit/offset）。
    validate_fn：validate_project_access 的封装（越权抛 ToolError）。
    include_scope_meta 且 list_systems_fn 提供时，scope_meta 还会回显 system 维度
    （可见 system id+name+code 列表），供 dev/pm 自查能否检索哪些 system。
    """
    is_admin = role == "admin"
    system_scope = system_scope or []

    if action == "list":
        visible_ids = None if is_admin else [uuid.UUID(s) for s in scope]
        nodes = await list_fn(project_ids=visible_ids)
        # 同 project_id 去重（created_at desc 已排序，取首条）
        seen: dict[uuid.UUID, ProjectInfo] = {}
        for n in nodes:
            pid = n.project_id
            if pid is None or pid in seen:
                continue  # ProjectProfile 必归属项目，None 仅防御性跳过
            seen[pid] = _to_project_info(n, include_profile)
        projects = list(seen.values())
        # ISSUE-07：scope 内但尚未创建 ProjectProfile 的项目补占位条目
        # （name=None），避免「visible_uuids 有 id 但 projects 查不到任何项目名」
        # 批次六（报告 P1-4）：占位条目标记 has_profile=False，输出级 hint
        # 引导补建——Agent 不再需要靠猜区分「无数据」与「未建画像」
        hint = None
        placeholder_count = 0
        if not is_admin:
            for pid in visible_ids or []:
                if pid not in seen:
                    seen[pid] = ProjectInfo(project_id=pid, has_profile=False)
                    projects.append(seen[pid])
                    placeholder_count += 1
            if placeholder_count:
                hint = (
                    f"{placeholder_count} 个可见项目尚未创建画像（has_profile=false，"
                    "name/工作目录等暂不可见）：请 admin 用 manage_project_profile 补建，"
                    "补建后所有 Agent 可自证项目与代码仓库的对应关系"
                )
        scope_meta = (
            _build_scope_meta(
                is_admin,
                scope,
                projects,
                system_scope=system_scope,
                systems=await list_systems_fn() if list_systems_fn else [],
            )
            if include_scope_meta
            else None
        )
        return GetProjectInfoOutput(
            action="list", scope=scope_meta, projects=projects, hint=hint
        )

    elif action == "get":
        if project_id is None:
            raise ValueError("get 操作必须指定 project_id")
        validate_fn(project_id)  # 越权抛 ToolError
        nodes = await list_fn(project_ids=[project_id], limit=1)
        project = _to_project_info(nodes[0], include_profile) if nodes else None
        scope_meta = (
            _build_scope_meta(
                is_admin,
                scope,
                [project] if project else [],
                system_scope=system_scope,
                systems=await list_systems_fn() if list_systems_fn else [],
            )
            if include_scope_meta
            else None
        )
        return GetProjectInfoOutput(action="get", scope=scope_meta, project=project)

    raise ValueError(f"未知 action: {action}")
