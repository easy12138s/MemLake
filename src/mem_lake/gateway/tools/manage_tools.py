"""管理类工具：admin 对系统资源（Access Key / 项目画像）的直接管理。

工具职责：转发 auth/service 与 knowledge/repository 的管理操作，控制事务边界。
manage_project_profile 为 PDD 3.4 + 8.5 要求的审批豁免入口（admin 直接写入 ProjectProfile 节点）。

包含工具（PDD 6.1 Admin 工具表）：
- create_access_key / revoke_access_key / list_access_keys / update_access_key_scope /
  rotate_access_key / set_access_key_mode：Access Key 的创建/吊销/查看/改范围/轮换/改审核模式（指定角色与项目范围）
- manage_project_profile：直接写入/更新 ProjectProfile 节点（不走审批流，状态 approved）

设计要点：
- 角色 RBAC 由中间件层控制（admin 专属），本文件不区分角色
- create_access_key / rotate_access_key 返回明文仅一次，调用方负责安全保存
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
from sqlalchemy import delete, select

from mem_lake.auth.service import (
    create_access_key as svc_create_access_key,
)
from mem_lake.auth.service import (
    list_access_keys as svc_list_access_keys,
)
from mem_lake.auth.service import (
    revoke_access_key as svc_revoke_access_key,
)
from mem_lake.auth.service import (
    rotate_access_key as svc_rotate_access_key,
)
from mem_lake.auth.service import (
    update_access_key_mode as svc_update_access_key_mode,
)
from mem_lake.auth.service import (
    update_access_key_scope as svc_update_access_key_scope,
)
from mem_lake.auth.service import (
    update_access_key_systems,
)
from mem_lake.config import get_settings
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
from mem_lake.gateway.tools._shared import (
    WRITE_TOOL_ANNOTATIONS,
    StrictInputModel,
    to_tool_error,
)
from mem_lake.knowledge.models import System, SystemProject
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
    lax_mode: bool = Field(
        description="审核模式：false=严格(需审批) true=宽松(免审批直接入库)"
    )
    created_at: datetime = Field(description="创建时间")
    revoked_at: datetime | None = Field(default=None, description="吊销时间")


class RevokeAccessKeyOutput(BaseModel):
    """revoke_access_key 工具出参。"""

    key_id: uuid.UUID = Field(description="被吊销的 Access Key ID")
    status: str = Field(description="状态：revoked")


class AccessKeyListOutput(BaseModel):
    """列表型 Access Key 工具的具名出参（统一出参形态，便于分页/元信息扩展）。"""

    items: list[AccessKeyOutput] = Field(description="Access Key 列表")
    total: int = Field(description="列表项总数（= len(items)）")


class CreateAccessKeyOutput(BaseModel):
    """create / rotate 操作出参（含明文，仅返回一次）。"""

    key_id: uuid.UUID = Field(description="Access Key ID")
    plaintext: str = Field(
        description="Access Key 明文（仅此一次返回，调用方负责安全保存）"
    )
    role: str = Field(description="角色")
    project_scope: list[str] = Field(description="项目范围")
    lax_mode: bool = Field(
        default=False,
        description="审核模式：false=严格(需审批) true=宽松(免审批直接入库)",
    )
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


class ProjectProfileInput(StrictInputModel):
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
    warning: str | None = Field(
        default=None,
        description="操作提示（如 create 时项目已有画像节点，供调用方知情）",
    )


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
    "收到字符串而非列表" 直接报错（AUDIT §2.16/P2#10）。
    输入非空但全片段非法时抛 ValueError——此前静默跳过会导致 update_scope
    变成无提示的空操作。
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
    if items and not result:
        raise ValueError("key_id 列表全部为非法 UUID（请输入合法 UUID 列表）")
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


class ManageSystemOutput(BaseModel):
    """manage_system 工具出参。"""

    action: str = Field(description="操作类型：create/list/set_projects/bind_keys")
    system_id: str | None = Field(default=None, description="system 域 ID")
    name: str | None = Field(default=None, description="系统域名（create 时）")
    description: str | None = Field(default=None, description="系统域描述（create 时）")
    project_count: int | None = Field(default=None, description="归属项目数（set_projects/list 时）")
    systems: list[dict] | None = Field(default=None, description="系统域列表（list 时，含 project_count）")
    affected_key_ids: list[str] | None = Field(default=None, description="受影响 Key ID 列表（bind_keys 时）")


def register_manage_tools(mcp: FastMCP) -> None:
    """注册管理类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def create_access_key(
        role: str = Field(description="角色：admin/pm/dev"),
        project_scope: list[uuid.UUID] | None = Field(
            default=None,
            description="初始项目范围（admin 为空列表表示不受限）",
        ),
        lax_mode: bool = Field(
            default=False,
            description="初始审核模式：false=严格(需审批) true=宽松(免审批直接入库)",
        ),
    ) -> CreateAccessKeyOutput:
        """创建 Access Key，返回明文（仅此一次，调用方负责安全保存）。

        Admin 工具。role 决定工具集，project_scope 限定 pm/dev 可访问项目
        （admin 为空列表表示不受限）。同时返回两部分初始化产物，请按需分发给对应接收方：
          · mcp_config：拼装好的 MCP 客户端配置 JSON，交【用户】粘贴到其
            MCP 客户端（Claude Desktop / Cursor / Codex 等），Agent 不自行安装 MCP；
          · onboarding_prompt：给【目标 Agent】的技能安装提示词（不含 Key），
            MCP 接通后复制给 Agent 执行一次性技能安装。
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                if project_scope is None:
                    raise ValueError(
                        "create_access_key 必须指定 project_scope（admin 为空列表）"
                    )
                new_key_id, plaintext = await svc_create_access_key(
                    session,
                    role=role,
                    project_scope={
                        "systems": [],
                        "projects": [str(pid) for pid in (project_scope or [])],
                    },
                    created_by=key_id_actor,
                    lax_mode=bool(lax_mode),
                )
                mcp_url = get_settings().MCP_PUBLIC_URL
                return CreateAccessKeyOutput(
                    key_id=new_key_id,
                    plaintext=plaintext,
                    role=role,
                    project_scope=[str(pid) for pid in project_scope],
                    lax_mode=bool(lax_mode),
                    mcp_config=_build_mcp_config(mcp_url, plaintext),
                    onboarding_prompt=_build_onboarding_prompt(role),
                )
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def revoke_access_key(
        key_id: uuid.UUID = Field(description="要吊销的 Access Key ID"),
    ) -> RevokeAccessKeyOutput:
        """吊销指定 key_id 的 Access Key（status=revoked）。

        Admin 工具。吊销后该 Key 立即失效，不可恢复；如需再用需重新创建。
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                await svc_revoke_access_key(
                    session, key_id=key_id, actor=key_id_actor
                )
                return RevokeAccessKeyOutput(key_id=key_id, status="revoked")
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def list_access_keys(
        role: str | None = Field(default=None, description="按角色过滤：admin/pm/dev"),
        status_filter: str | None = Field(
            default=None, description="按状态过滤：active/revoked"
        ),
        lax_mode: bool | None = Field(
            default=None, description="按审核模式过滤：true=宽松/false=严格"
        ),
    ) -> AccessKeyListOutput:
        """按角色/状态/审核模式查看 Access Key 列表。Admin 工具。"""
        try:
            async with transactional_session() as session:
                keys = await svc_list_access_keys(
                    session, role=role, status=status_filter, lax_mode=lax_mode
                )
                items = [_to_access_key_output(k) for k in keys]
                return AccessKeyListOutput(items=items, total=len(items))
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def update_access_key_scope(
        project_scope: list[uuid.UUID] | None = Field(
            default=None,
            description="新的项目范围（grant_all_projects=true 时留空表示不受限）",
        ),
        key_ids: str | list[uuid.UUID] | None = Field(
            default=None,
            description=(
                "显式指定一个或多个目标 Key ID。"
                "接受 UUID 列表，或逗号/空格/分号分隔的字符串（兼容客户端将数组序列化为字符串的场景）"
            ),
        ),
        role_filter: str | None = Field(
            default=None,
            description="按角色批量授权（如 'dev' → 所有 dev Key）",
        ),
        grant_all_projects: bool = Field(
            default=False,
            description="一键将全部 Key 授权为不受限（project_scope=[]）",
        ),
    ) -> AccessKeyListOutput:
        """动态修改 Key 的项目范围，支持三种定位（优先级 key_ids > role_filter > grant_all_projects）。

        Admin 工具。三者均省略时为空操作（返回空列表，不改任何 Key）。
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                if project_scope is None:
                    raise ValueError(
                        "update_access_key_scope 必须指定 project_scope（新的项目范围）"
                    )
                updated = await svc_update_access_key_scope(
                    session,
                    project_scope=project_scope,
                    key_ids=_normalize_uuid_list(key_ids),
                    role_filter=role_filter,
                    grant_all_projects=grant_all_projects,
                    actor=key_id_actor,
                )
                items = [_to_access_key_output(k) for k in updated]
                return AccessKeyListOutput(items=items, total=len(items))
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def rotate_access_key(
        key_id: uuid.UUID = Field(description="要轮换的 Access Key ID"),
    ) -> CreateAccessKeyOutput:
        """轮换指定 key_id 的 Key 密钥（保留 Key ID，旧明文立即失效），返回新明文（仅此一次）。

        Admin 工具。用于密钥疑似泄露时主动作废，无需吊销重建（Key ID 不变）。
        同样附带 mcp_config 与 onboarding_prompt。
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                ak, plaintext = await svc_rotate_access_key(
                    session, key_id=key_id, actor=key_id_actor
                )
                _ak_scope = ak.project_scope or {}
                _proj_list = (
                    [str(p) for p in _ak_scope.get("projects", [])]
                    if isinstance(_ak_scope, dict)
                    else [str(x) for x in _ak_scope]
                )
                mcp_url = get_settings().MCP_PUBLIC_URL
                return CreateAccessKeyOutput(
                    key_id=ak.id,
                    plaintext=plaintext,
                    role=ak.role,
                    project_scope=_proj_list,
                    lax_mode=bool(ak.lax_mode),
                    mcp_config=_build_mcp_config(mcp_url, plaintext),
                    onboarding_prompt=_build_onboarding_prompt(ak.role),
                )
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def set_access_key_mode(
        lax_mode: bool = Field(
            description="新的审核模式：true=宽松(免审批直接入库) false=严格"
        ),
        key_ids: str | list[uuid.UUID] | None = Field(
            default=None,
            description=(
                "显式指定一个或多个目标 Key ID。"
                "接受 UUID 列表，或逗号/空格/分号分隔的字符串（兼容客户端将数组序列化为字符串的场景）"
            ),
        ),
        role_filter: str | None = Field(
            default=None, description="按角色批量设置（如 'dev' → 所有 dev Key）"
        ),
        grant_all_projects: bool = Field(
            default=False, description="一键将全部 Key 设置为指定模式"
        ),
    ) -> AccessKeyListOutput:
        """设置/批量设置 Key 的审核模式（lax_mode）。定位方式同
        update_access_key_scope（key_ids > role_filter > grant_all_projects）。

        Admin 工具。lax_mode=true 表示宽松（免审批直接入库）。全局开关
        LAX_MODE_ENABLED=false 时即使标记宽松也强制走审批。
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                updated = await svc_update_access_key_mode(
                    session,
                    lax_mode=lax_mode,
                    key_ids=_normalize_uuid_list(key_ids),
                    role_filter=role_filter,
                    grant_all_projects=grant_all_projects,
                    actor=key_id_actor,
                )
                items = [_to_access_key_output(k) for k in updated]
                return AccessKeyListOutput(items=items, total=len(items))
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def manage_system(
        action: Literal["create", "list", "set_projects", "bind_keys"] = Field(
            description="操作类型"
        ),
        name: str | None = Field(
            default=None, description="create 时指定的系统域名（唯一）"
        ),
        description: str | None = Field(
            default=None, description="create 时指定的系统域描述"
        ),
        system_id: uuid.UUID | None = Field(
            default=None,
            description="set_projects / bind_keys 时指定的 system 域 ID",
        ),
        project_ids: list[uuid.UUID] | None = Field(
            default=None,
            description="set_projects 时指定该系统下归属的项目 ID 列表",
        ),
        key_ids: str | list[uuid.UUID] | None = Field(
            default=None,
            description=(
                "bind_keys 时显式指定一个或多个目标 Key ID"
                "（接受 UUID 列表，或逗号/空格分隔的字符串）"
            ),
        ),
        role_filter: str | None = Field(
            default=None, description="bind_keys 时按角色批量（如 'dev' → 所有 dev Key）"
        ),
        grant_all: bool = Field(
            default=False, description="bind_keys 时作用于全部 Key"
        ),
    ) -> ManageSystemOutput:
        """管理 system 域（admin 专属）：建立/枚举系统域、维护系统↔项目归属、绑定 Key 的 system 授权。

        PM 需求按 system 隔离；System 由 admin 统一建并签发。行为：
        - create：建一个 System 域，返回 system_id
        - list：枚举所有 System（含其下项目数）
        - set_projects：定义该系统下挂哪些 project（决定 dev 对悬浮需求的可见性与影响评估聚合）
        - bind_keys：把该系统授权给目标 Key（进入其 scope.systems；定位方式 key_ids > role_filter > grant_all）
        """
        try:
            key_id_actor = get_current_key_id()
            async with transactional_session() as session:
                if action == "create":
                    if not name:
                        raise ValueError("create 操作必须指定 name")
                    sys_obj = System(name=name, description=description or "")
                    session.add(sys_obj)
                    await session.flush()
                    return ManageSystemOutput(
                        action="create",
                        system_id=str(sys_obj.id),
                        name=sys_obj.name,
                        description=description,
                    )
                if action == "list":
                    rows = (
                        await session.execute(select(System).order_by(System.name))
                    ).scalars().all()
                    result = []
                    for row in rows:
                        cnt = (
                            await session.execute(
                                select(SystemProject.project_id).where(
                                    SystemProject.system_id == row.id
                                )
                            )
                        ).scalars().all()
                        result.append(
                            {
                                "system_id": str(row.id),
                                "name": row.name,
                                "description": row.description,
                                "project_count": len(cnt),
                            }
                        )
                    return ManageSystemOutput(action="list", systems=result)

                if not system_id:
                    raise ValueError("set_projects / bind_keys 必须指定 system_id")
                exists = (
                    await session.execute(
                        select(System).where(System.id == system_id)
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise ValueError(f"system 不存在: {system_id}")

                if action == "set_projects":
                    pids = [str(p) for p in (project_ids or [])]
                    # 先清空该系统归属，再批量写入（幂等：删+插）
                    await session.execute(
                        delete(SystemProject).where(SystemProject.system_id == system_id)
                    )
                    for pid in pids:
                        session.add(
                            SystemProject(system_id=system_id, project_id=uuid.UUID(pid))
                        )
                    await session.flush()
                    return ManageSystemOutput(
                        action="set_projects",
                        system_id=str(system_id),
                        project_count=len(pids),
                    )

                if action == "bind_keys":
                    updated = await update_access_key_systems(
                        session,
                        system_ids=[system_id],
                        key_ids=_normalize_uuid_list(key_ids),
                        role_filter=role_filter,
                        grant_all=grant_all,
                        actor=key_id_actor,
                    )
                    return ManageSystemOutput(
                        action="bind_keys",
                        system_id=str(system_id),
                        affected_key_ids=[str(k.id) for k in updated],
                    )

                raise ValueError(f"未知 action: {action}")
        except Exception as e:
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
                    # create 时若项目已存在画像节点，附提示供调用方知情
                    # （AUDIT §2.16/§2.4：多处"取最新画像"策略下重复画像会静默漂移）
                    existing = await _has_approved_profile(
                        session, profile_id, node.id
                    )
                    return ManageProjectProfileOutput(
                        project_id=profile_id,
                        node_id=node.id,
                        action="create",
                        status=node.status,
                        version=node.version,
                        warning=(
                            "该项目已存在其他 approved 画像节点，get_project_profile"
                            " 将取最新一条" if existing else None
                        ),
                    )
                elif action == "update":
                    if not node_id:
                        raise ValueError("update 操作必须指定 node_id")
                    # 校验目标节点确实是 ProjectProfile（AUDIT §2.16：此前
                    # 可修改任意类型节点，语义漂移）
                    from mem_lake.knowledge.repository import get_node

                    target = await get_node(session, node_id)
                    if target.type != "ProjectProfile":
                        raise ValueError(
                            f"node_id 非 ProjectProfile 类型: {target.type}，"
                            "manage_project_profile 仅维护项目画像节点"
                        )
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
                        # 出参 project_id 取实际节点归属（AUDIT §2.16：此前
                        # update 未传 project_id 时误用随机新生成的 profile_id）
                        project_id=node.project_id,
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


async def _has_approved_profile(
    session, project_id, exclude_node_id
) -> bool:
    """判断项目下是否存在其他 approved 画像节点（供 create 提示）。

    exclude_node_id 排除本次新建的画像（避免自检）。
    """
    from sqlalchemy import select

    from mem_lake.knowledge.models import KnowledgeNode

    stmt = (
        select(KnowledgeNode.id)
        .where(KnowledgeNode.project_id == project_id)
        .where(KnowledgeNode.type == "ProjectProfile")
        .where(KnowledgeNode.status == "approved")
        .where(KnowledgeNode.is_deleted.is_(False))
        .where(KnowledgeNode.id != exclude_node_id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


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

    scope = (
        access_key.project_scope
        if isinstance(access_key.project_scope, dict)
        else {"systems": [], "projects": [str(x) for x in (access_key.project_scope or [])]}
    )
    return AccessKeyOutput(
        key_id=access_key.id,
        role=access_key.role,
        project_scope=[str(p) for p in scope.get("projects", [])],
        status=access_key.status,
        lax_mode=bool(access_key.lax_mode),
        created_at=_as_utc_aware(access_key.created_at),
        revoked_at=_as_utc_aware(access_key.revoked_at),
    )
