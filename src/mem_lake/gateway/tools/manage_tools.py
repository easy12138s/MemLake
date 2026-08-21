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

import json
import logging
import re
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
    rotate_access_key,
    update_access_key_scope,
)
from mem_lake.gateway.background_tasks import (
    find_running_task,
    get_task_record,
    start_reindex_task,
)
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    transactional_session,
    validate_project_access,
)
from mem_lake.config import get_settings
from mem_lake.gateway.tools._shared import (
    WRITE_TOOL_ANNOTATIONS,
    to_tool_error,
)
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    create_node,
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
    """create / rotate 操作出参（含明文，仅返回一次）。"""

    key_id: uuid.UUID = Field(description="Access Key ID")
    plaintext: str = Field(
        description="Access Key 明文（仅此一次返回，调用方负责安全保存）"
    )
    role: str = Field(description="角色")
    project_scope: list[str] = Field(description="项目范围")
    mcp_config: str | None = Field(
        default=None,
        description=(
            "拼装好的 MCP 客户端配置（JSON 字符串），交由用户粘贴到自己的 "
            "MCP 客户端（Claude Desktop / Cursor / Codex 等），目标 Agent 不自行安装 MCP"
        ),
    )
    onboarding_prompt: str | None = Field(
        default=None,
        description=(
            "给目标 Agent 的技能安装提示词（不含 Key，MCP 接通后复制给 Agent 执行一次性安装）"
        ),
    )


class ManageAccessKeyOutput(BaseModel):
    """manage_access_key 工具出参。"""

    action: str = Field(description="操作类型：create/revoke/list/update_scope/rotate")
    created: CreateAccessKeyOutput | None = Field(default=None, description="create 结果")
    revoked_key_id: uuid.UUID | None = Field(default=None, description="revoke 结果")
    listed: list[AccessKeyOutput] | None = Field(default=None, description="list 结果")
    scoped: list[AccessKeyOutput] | None = Field(
        default=None, description="update_scope 结果（受影响 Key 列表）"
    )
    rotated: CreateAccessKeyOutput | None = Field(
        default=None, description="rotate 结果（含新明文，仅返回一次）"
    )


# ============================================================================
# Access Key 初始化产物构造（onboarding）
# ============================================================================


def _build_mcp_config(mcp_url: str, plaintext: str) -> str:
    """拼装给用户粘贴的 MCP 客户端配置（JSON 字符串）。

    目标 Agent 通常无法自行给自己安装 MCP，因此这部分交由用户处理：
    用户把返回的 JSON 写入自己的 MCP 客户端（Claude Desktop / Cursor / Codex 等），
    Agent 侧只需在 MCP 接通后执行技能安装（见 _build_onboarding_prompt）。
    """
    import json

    cfg = {
        "mcpServers": {
            "mem-lake": {
                "url": mcp_url,
                "headers": {"X-MCP-Key": plaintext},
            }
        }
    }
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def _build_onboarding_prompt(role: str) -> str:
    """给目标 Agent 的技能安装提示词（不含 Key，MCP 已接通后执行）。

    仅指导 Agent 调用 get_role_skills 取回 SKILL.md 并写入其客户端约定目录，
    安全相关提醒不在 Agent 侧（Key 仅出现在 mcp_config 的 JSON 里，由用户保管）。
    """
    return (
        f"你是当前项目的 {role}。MemLake MCP 已接通，现在执行一次性操作："
        "安装团队知识共享技能。\n\n"
        "## 1. 获取技能\n"
        f'调用 get_role_skills(role="{role}")，读取返回的 skills_markdown'
        "（完整 SKILL.md，含你的角色工具用法、最佳实践与字段规范）和 installation_guide。\n\n"
        "## 2. 安装技能\n"
        "将 skills_markdown 写入 installation_guide 中对应你当前客户端的目录；"
        "不确定时用通用路径：\n"
        f".agents/skills/mem-lake-{role}/SKILL.md\n"
        "若无文件写入权限或写入失败，将 skills_markdown 完整输出给用户手动保存。\n\n"
        "## 3. 完成\n"
        "写入成功后告知用户：技能已安装，重启或刷新当前会话后生效。生效后该技能会指导你在"
        "日常工作中使用 MemLake 工具检索和沉淀团队知识。"
    )


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

    project_id: uuid.UUID = Field(
        description="归属项目 ID（create 时未传则服务端自动生成并返回）"
    )
    node_id: uuid.UUID = Field(description="节点 ID")
    action: str = Field(description="操作类型：create/update")
    status: str = Field(description="节点状态：approved（直接审批豁免）")
    version: int = Field(description="版本号")


def _resolve_profile_id(project_id: uuid.UUID | None) -> uuid.UUID:
    """解析 ProjectProfile 归属项目 ID。

    未传 project_id 时服务端自动生成新的项目 ID，便于「先建项目画像、再围绕该项目
    沉淀需求/产物」的入门流程，避免调用方先自行生成 UUID。
    """
    return project_id or uuid.uuid4()


def _normalize_uuid_list(
    value: str | list[uuid.UUID] | list[str] | None,
) -> list[uuid.UUID] | None:
    """将 key_ids 入参规范为 UUID 列表（或 None）。

    兼容两种客户端传参：
    - 列表：[UUID | str, ...]
    - 字符串：逗号/空格/分号分隔（或 JSON 数组字符串），如 "id1,id2" / "[id1,id2]"
    部分客户端会将数组序列化成字符串，这里在工具层归一化，避免 pydantic 因
    "收到字符串而非列表" 直接报错。非法片段会被跳过（保持容错）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # JSON 数组字符串（如 "[\"id1\",\"id2\"]"）
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                items = [str(x) for x in parsed]
            except (json.JSONDecodeError, TypeError):
                items = []
        else:
            items = [p for p in re.split(r"[,\s;]+", s) if p]
    else:
        items = [str(x) for x in value]
    result: list[uuid.UUID] = []
    for item in items:
        try:
            result.append(uuid.UUID(str(item).strip()))
        except (ValueError, AttributeError):
            continue
    return result


class ReindexOutput(BaseModel):
    """reindex_project_vectors 工具出参。"""

    project_id: uuid.UUID = Field(description="项目 ID")
    task_id: uuid.UUID = Field(description="异步重嵌任务 ID，用于 get_reindex_status 轮询")
    reindexed: int = Field(description="已重建向量的节点数（任务未结束时为 0）")
    status: str = Field(description="任务状态：pending/running/done/failed")


class ReindexStatusOutput(BaseModel):
    """get_reindex_status 工具出参。"""

    task_id: uuid.UUID = Field(description="任务 ID")
    project_id: uuid.UUID = Field(description="项目 ID")
    status: str = Field(description="任务状态：pending/running/done/failed")
    total: int | None = Field(default=None, description="总量（已开始执行后为节点总数）")
    processed: int = Field(default=0, description="已处理节点数")
    reindexed: int = Field(default=0, description="已重建向量节点数")
    error: str | None = Field(default=None, description="失败原因（status=failed 时）")
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="结束时间")
    created_at: datetime | None = Field(default=None, description="任务创建时间")


# ============================================================================
# 工具注册
# ============================================================================


def register_manage_tools(mcp: FastMCP) -> None:
    """注册管理类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def manage_access_key(
        action: Literal["create", "revoke", "list", "update_scope", "rotate"] = Field(
            description="操作类型"
        ),
        role: str | None = Field(
            default=None, description="create 时指定角色：admin/pm/dev"
        ),
        project_scope: list[uuid.UUID] | None = Field(
            default=None,
            description=(
                "create 时指定初始项目范围（admin 为空列表表示不受限）；"
                "update_scope 时指定新的项目范围"
            ),
        ),
        key_id: uuid.UUID | None = Field(
            default=None, description="revoke / rotate 时指定 Access Key ID"
        ),
        status_filter: str | None = Field(
            default=None, description="list 时按状态过滤：active/revoked"
        ),
        key_ids: str | list[uuid.UUID] | None = Field(
            default=None,
            description=(
                "update_scope 时显式指定一个或多个目标 Key ID。"
                "接受 UUID 列表，或逗号/空格/分号分隔的字符串（兼容客户端将数组序列化为字符串的场景）"
            ),
        ),
        role_filter: str | None = Field(
            default=None,
            description="update_scope 时按角色批量授权（如 'dev' → 所有 dev Key）",
        ),
        grant_all_projects: bool = Field(
            default=False,
            description="update_scope 时一键将全部 Key 授权为不受限（project_scope=[]）",
        ),
    ) -> ManageAccessKeyOutput:
        """管理 Access Key（创建/吊销/查看/改范围/轮换），指定角色与项目范围。

        Admin 工具。
        - create：创建 Key，返回明文（仅此一次，调用方负责安全保存）。role 决定工具集，
          project_scope 限定 pm/dev 可访问项目（admin 为空列表表示不受限）。
          同时返回两部分初始化产物，请按需分发给对应接收方：
            · created.mcp_config：拼装好的 MCP 客户端配置 JSON，交【用户】粘贴到其
              MCP 客户端（Claude Desktop / Cursor / Codex 等），Agent 不自行安装 MCP；
            · created.onboarding_prompt：给【目标 Agent】的技能安装提示词（不含 Key），
              MCP 接通后复制给 Agent 执行一次性技能安装。
        - revoke：吊销指定 key_id 的 Key（status=revoked）。
        - list：按角色/状态查看 Key 列表。
        - update_scope：动态修改 Key 的项目范围，支持三种定位（优先级
          key_ids > role_filter > grant_all_projects）：显式指定多个 Key / 按角色批量 /
          一键全项目（grant_all_projects 时 project_scope 留空表示不受限）。
          三者均省略时为空操作（返回空列表，不改任何 Key）。
        - rotate：轮换指定 key_id 的 Key 密钥（保留 Key ID，旧明文立即失效），
          返回新明文（仅此一次），同样附带 mcp_config 与 onboarding_prompt。
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
                    mcp_url = get_settings().MCP_PUBLIC_URL
                    return ManageAccessKeyOutput(
                        action="create",
                        created=CreateAccessKeyOutput(
                            key_id=new_key_id,
                            plaintext=plaintext,
                            role=role,
                            project_scope=[str(pid) for pid in project_scope],
                            mcp_config=_build_mcp_config(mcp_url, plaintext),
                            onboarding_prompt=_build_onboarding_prompt(role),
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
                elif action == "update_scope":
                    if project_scope is None:
                        raise ValueError(
                            "update_scope 操作必须指定 project_scope（新的项目范围）"
                        )
                    updated = await update_access_key_scope(
                        session,
                        project_scope=project_scope,
                        key_ids=_normalize_uuid_list(key_ids),
                        role_filter=role_filter,
                        grant_all_projects=grant_all_projects,
                        actor=key_id_actor,
                    )
                    return ManageAccessKeyOutput(
                        action="update_scope",
                        scoped=[_to_access_key_output(k) for k in updated],
                    )
                elif action == "rotate":
                    if not key_id:
                        raise ValueError("rotate 操作必须指定 key_id")
                    ak, plaintext = await rotate_access_key(
                        session, key_id=key_id, actor=key_id_actor
                    )
                    mcp_url = get_settings().MCP_PUBLIC_URL
                    return ManageAccessKeyOutput(
                        action="rotate",
                        rotated=CreateAccessKeyOutput(
                            key_id=ak.id,
                            plaintext=plaintext,
                            role=ak.role,
                            project_scope=ak.project_scope or [],
                            mcp_config=_build_mcp_config(mcp_url, plaintext),
                            onboarding_prompt=_build_onboarding_prompt(ak.role),
                        ),
                    )
                else:
                    raise ValueError(f"未知 action: {action}")
        except (AccessKeyNotFoundError, ValueError, Exception) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def manage_project_profile(
        action: Literal["create", "update"] = Field(description="操作类型"),
        profile: ProjectProfileInput = Field(description="项目画像内容"),
        project_id: uuid.UUID | None = Field(
            default=None,
            description=(
                "归属项目 ID。create 时可省略，省略时服务端自动生成并返回"
                "（project_id 出参）；update 时必填（与既有 ProjectProfile 同项目）"
            ),
        ),
        node_id: uuid.UUID | None = Field(
            default=None, description="update 时指定现有 ProjectProfile 节点 ID"
        ),
    ) -> ManageProjectProfileOutput:
        """直接写入/更新 ProjectProfile 节点（不走审批流，状态直接 approved）。

        Admin 工具。ProjectProfile 为项目级元信息（技术栈/架构/约定/团队），
        admin 直接维护无需审批。create 新建节点，update 更新已有节点（需传 node_id）。

        入门体验优化：create 时若未传 project_id，服务端自动生成新项目 ID
        并通过出参 project_id 返回，调用方无需预先自行生成 UUID。
        """
        try:
            profile_id = _resolve_profile_id(project_id)
            if project_id is not None:
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
                        project_id=profile_id,
                        node_type="ProjectProfile",
                        title=profile.title,
                        content=profile.content,
                        properties=props,
                        tags=profile.tags,
                        created_by=key_id,
                        generate_vector=True,
                    )
                    return ManageProjectProfileOutput(
                        project_id=profile_id,
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
                        project_id=profile_id,
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
        batch_size: int = Field(
            default=50, description="后台批量向量化每批节点数（默认 50）"
        ),
    ) -> ReindexOutput:
        """异步重建项目内全部知识节点的向量（admin 运维工具）。

        本工具立即返回任务 ID（task_id），真正的向量重嵌在后台分批执行，
        避免大项目同步执行导致的 MCP 调用超时。提交后通过 get_reindex_status 轮询进度。
        若同一项目已有进行中（pending/running）任务，直接返回该任务（防重入，避免重复全量重嵌）。
        仅重建 approved 状态节点。
        """
        try:
            validate_project_access(project_id)
            actor = get_current_key_id()

            existing = await find_running_task(project_id)
            if existing is not None:
                return ReindexOutput(
                    project_id=project_id,
                    task_id=existing.id,
                    reindexed=existing.reindexed or 0,
                    status=existing.status,
                )

            task_id = await start_reindex_task(
                project_id, actor, batch_size=batch_size
            )
            return ReindexOutput(
                project_id=project_id, task_id=task_id, reindexed=0, status="pending"
            )
        except (NodeNotFoundError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def get_reindex_status(
        task_id: uuid.UUID = Field(description="reindex_project_vectors 返回的任务 ID"),
    ) -> ReindexStatusOutput:
        """查询异步重嵌任务的进度与状态（admin 运维工具）。"""
        try:
            task = await get_task_record(task_id)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            return ReindexStatusOutput(
                task_id=task.id,
                project_id=task.project_id,
                status=task.status,
                total=task.total,
                processed=task.processed,
                reindexed=task.reindexed,
                error=task.error,
                started_at=task.started_at,
                finished_at=task.finished_at,
                created_at=task.created_at,
            )
        except ValueError as e:
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
