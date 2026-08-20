"""管理类工具：admin 对系统资源（Access Key / 项目画像）的直接管理。

工具职责：转发 auth/service 与 knowledge/repository 的管理操作，控制事务边界。
manage_project_profile 为 PDD 3.4 + 8.5 要求的审批豁免入口（admin 直接写入 ProjectProfile 节点）。

包含工具（PDD 6.1 Admin 工具表）：
- manage_access_key：创建/吊销/查看 Access Key（指定角色与项目范围）
- manage_project_profile：直接写入/更新 ProjectProfile 节点（不走审批流，状态 approved）

设计要点：
- 角色 RBAC 由中间件层控制（admin 专属），本文件不区分角色
- manage_access_key create 返回明文仅一次，调用方负责安全保存
- manage_project_profile 绕过审批流直接调用 repository.create_node/update_node
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import BaseModel, Field

from mem_lake.auth.service import (
    AccessKeyNotFoundError,
    create_access_key,
    list_access_keys,
    revoke_access_key,
)
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    transactional_session,
    validate_project_access,
)
from mem_lake.gateway.tools._shared import (
    WRITE_TOOL_ANNOTATIONS,
    to_tool_error,
)
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    create_node,
    list_nodes_by_project,
    regenerate_vector,
    update_node,
)
from mem_lake.knowledge.schema import SchemaValidationError

logger = logging.getLogger("mem_lake.gateway.tools.manage")


# ============================================================================
# Access Key 输出模型
# ============================================================================


class AccessKeyOutput(BaseModel):
    """Access Key 信息（不含 hash）。"""

    key_id: uuid.UUID = Field(description="Access Key ID")
    role: str = Field(description="角色")
    project_scope: list[str] = Field(description="项目范围（项目 ID 字符串列表）")
    status: str = Field(description="状态：active/revoked")
    created_at: datetime = Field(description="创建时间")
    revoked_at: datetime | None = Field(default=None, description="吊销时间")


class CreateAccessKeyOutput(BaseModel):
    """create 操作出参（含明文，仅返回一次）。"""

    key_id: uuid.UUID = Field(description="Access Key ID")
    plaintext: str = Field(
        description="Access Key 明文（仅此一次返回，调用方负责安全保存）"
    )
    role: str = Field(description="角色")
    project_scope: list[str] = Field(description="项目范围")


class ManageAccessKeyOutput(BaseModel):
    """manage_access_key 工具出参。"""

    action: str = Field(description="操作类型：create/revoke/list")
    created: CreateAccessKeyOutput | None = Field(default=None, description="create 结果")
    revoked_key_id: uuid.UUID | None = Field(default=None, description="revoke 结果")
    listed: list[AccessKeyOutput] | None = Field(default=None, description="list 结果")


# ============================================================================
# ProjectProfile 输入/输出模型
# ============================================================================


class ProjectProfileInput(BaseModel):
    """项目画像内容。"""

    title: str = Field(description="项目名称标题")
    content: str = Field(description="项目描述")
    properties: dict[str, Any] = Field(
        description=(
            "ProjectProfile 属性，必填：name（项目名）、description（描述）、"
            "tech_stack（技术栈数组）、architecture（架构）；可选：conventions、team、"
            "work_dir（本地工作目录）、repo（代码仓库标识）"
        )
    )
    tags: list[str] = Field(default=[], description="标签数组")
    work_dir: str | None = Field(
        default=None, description="项目本地工作目录绝对路径（可选，登记便于自证隔离/定位）"
    )
    repo: str | None = Field(
        default=None, description="代码仓库标识/名称（可选，登记便于检索与核对）"
    )


class ManageProjectProfileOutput(BaseModel):
    """manage_project_profile 工具出参。"""

    node_id: uuid.UUID = Field(description="节点 ID")
    action: str = Field(description="操作类型：create/update")
    status: str = Field(description="节点状态：approved（直接审批豁免）")
    version: int = Field(description="版本号")


class ReindexOutput(BaseModel):
    """reindex_project_vectors 工具出参。"""

    project_id: uuid.UUID = Field(description="项目 ID")
    reindexed: int = Field(description="已重建向量的节点数")
    status: str = Field(description="执行状态：done")


# ============================================================================
# 工具注册
# ============================================================================


def register_manage_tools(mcp: FastMCP) -> None:
    """注册管理类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def manage_access_key(
        action: Literal["create", "revoke", "list"] = Field(
            description="操作类型"
        ),
        role: str | None = Field(
            default=None, description="create 时指定角色：admin/pm/dev"
        ),
        project_scope: list[uuid.UUID] | None = Field(
            default=None,
            description="create 时指定项目范围（admin 为空列表表示不受限）",
        ),
        key_id: uuid.UUID | None = Field(
            default=None, description="revoke 时指定 Access Key ID"
        ),
        status_filter: str | None = Field(
            default=None, description="list 时按状态过滤：active/revoked"
        ),
    ) -> ManageAccessKeyOutput:
        """管理 Access Key（创建/吊销/查看），指定角色与项目范围。

        Admin 工具。create 返回明文 Access Key（仅此一次，调用方负责安全保存）。
        role 决定可调用的工具集（admin 全部/pm 需求相关/dev 产物相关）。
        project_scope 限定 pm/dev 可访问的项目（admin 为空列表表示不受限）。
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                if action == "create":
                    if not role:
                        raise ValueError("create 操作必须指定 role")
                    if project_scope is None:
                        raise ValueError(
                            "create 操作必须指定 project_scope（admin 为空列表）"
                        )
                    new_key_id, plaintext = await create_access_key(
                        session,
                        role=role,
                        project_scope=project_scope,
                        created_by=key_id_actor,
                    )
                    return ManageAccessKeyOutput(
                        action="create",
                        created=CreateAccessKeyOutput(
                            key_id=new_key_id,
                            plaintext=plaintext,
                            role=role,
                            project_scope=[str(pid) for pid in project_scope],
                        ),
                    )
                elif action == "revoke":
                    if not key_id:
                        raise ValueError("revoke 操作必须指定 key_id")
                    await revoke_access_key(
                        session, key_id=key_id, actor=key_id_actor
                    )
                    return ManageAccessKeyOutput(
                        action="revoke", revoked_key_id=key_id
                    )
                elif action == "list":
                    keys = await list_access_keys(
                        session, role=role, status=status_filter
                    )
                    return ManageAccessKeyOutput(
                        action="list",
                        listed=[_to_access_key_output(k) for k in keys],
                    )
                else:
                    raise ValueError(f"未知 action: {action}")
        except (AccessKeyNotFoundError, ValueError, Exception) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def manage_project_profile(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        action: Literal["create", "update"] = Field(description="操作类型"),
        profile: ProjectProfileInput = Field(description="项目画像内容"),
        node_id: uuid.UUID | None = Field(
            default=None, description="update 时指定现有 ProjectProfile 节点 ID"
        ),
    ) -> ManageProjectProfileOutput:
        """直接写入/更新 ProjectProfile 节点（不走审批流，状态直接 approved）。

        Admin 工具。ProjectProfile 为项目级元信息（技术栈/架构/约定/团队），
        admin 直接维护无需审批。create 新建节点，update 更新已有节点（需传 node_id）。
        """
        try:
            validate_project_access(project_id)
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context
            key_id = get_current_key_id()

            async with transactional_session() as session:
                if action == "create":
                    props = _profile_properties(profile)
                    node = await create_node(
                        session,
                        graph_store=lifespan_ctx.graph_store,
                        embedding_client=lifespan_ctx.embedding_client,
                        project_id=project_id,
                        node_type="ProjectProfile",
                        title=profile.title,
                        content=profile.content,
                        properties=props,
                        tags=profile.tags,
                        created_by=key_id,
                        generate_vector=True,
                    )
                    return ManageProjectProfileOutput(
                        node_id=node.id,
                        action="create",
                        status=node.status,
                        version=node.version,
                    )
                elif action == "update":
                    if not node_id:
                        raise ValueError("update 操作必须指定 node_id")
                    props = _profile_properties(profile)
                    node = await update_node(
                        session,
                        graph_store=lifespan_ctx.graph_store,
                        embedding_client=lifespan_ctx.embedding_client,
                        node_id=node_id,
                        title=profile.title,
                        content=profile.content,
                        properties=props,
                        tags=profile.tags,
                        actor=key_id,
                        regenerate_vector=True,
                    )
                    return ManageProjectProfileOutput(
                        node_id=node.id,
                        action="update",
                        status=node.status,
                        version=node.version,
                    )
                else:
                    raise ValueError(f"未知 action: {action}")
        except (NodeNotFoundError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def reindex_project_vectors(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        limit: int = Field(
            default=500, description="单次最多重建向量的节点数（避免超大项目单次超时）"
        ),
    ) -> ReindexOutput:
        """重建项目内全部知识节点的向量（admin 运维工具）。

        当嵌入文本构造逻辑变更（如纳入更多属性）后，存量节点的 content_vector 已过期，
        需调用本工具刷新以恢复检索召回质量。逐节点重新生成向量并随事务提交。
        仅重建 approved 状态节点。嵌入逻辑见 mem_lake.knowledge.embed。
        """
        try:
            validate_project_access(project_id)
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context
            actor = get_current_key_id()

            async with transactional_session() as session:
                nodes = await list_nodes_by_project(
                    session,
                    project_id=project_id,
                    status="approved",
                    limit=limit,
                )
                count = 0
                for node in nodes:
                    await regenerate_vector(
                        session,
                        embedding_client=lifespan_ctx.embedding_client,
                        node_id=node.id,
                        actor=actor,
                    )
                    count += 1
                return ReindexOutput(
                    project_id=project_id, reindexed=count, status="done"
                )
        except (NodeNotFoundError, ValueError) as e:
            raise to_tool_error(e)


# ============================================================================
# 转换辅助函数
# ============================================================================


def _profile_properties(profile: "ProjectProfileInput") -> dict:
    """合并 work_dir/repo 到 properties 副本，避免修改入参。

    work_dir/repo 为可选元数据字段，仅当非空时写入，便于 get_project_info 回显。
    """
    props: dict = dict(profile.properties or {})
    if profile.work_dir is not None:
        props["work_dir"] = profile.work_dir
    if profile.repo is not None:
        props["repo"] = profile.repo
    return props


def _to_access_key_output(access_key) -> AccessKeyOutput:
    """从 AccessKey ORM 对象构造 AccessKeyOutput。

    access_key.created_at/revoked_at 在 ORM 中为 naive datetime（列类型未带时区），
    序列化为 ISO 串时缺少偏移，不满足 MCP 输出 schema 的 date-time（RFC 3339）校验。
    此处统一补 UTC 时区，使其输出带偏移，通过 schema 校验。
    """

    def _as_utc_aware(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    return AccessKeyOutput(
        key_id=access_key.id,
        role=access_key.role,
        project_scope=access_key.project_scope or [],
        status=access_key.status,
        created_at=_as_utc_aware(access_key.created_at),
        revoked_at=_as_utc_aware(access_key.revoked_at),
    )
