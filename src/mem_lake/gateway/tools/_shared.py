"""工具共享辅助：ToolAnnotations 常量 + 输出模型 + 异常转换 + items 构造 + 角色文档。

本模块是 gateway/tools/ 下所有工具模块的共享基础设施，避免重复代码。
对齐 PDD 6.1 工具表 + 8.3/8.4/8.5 Skills 文档。
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastmcp.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from mem_lake.approval.service import (
    BatchNotFoundError,
    BatchStatusError,
    IdempotencyConflictError,
    PayloadValidationError,
)
from mem_lake.knowledge.repository import NodeNotFoundError
from mem_lake.knowledge.schema import SchemaValidationError

logger = logging.getLogger("mem_lake.gateway.tools.shared")


# ============================================================================
# ToolAnnotations 常量（snake_case，alias_generator 自动转 camelCase wire format）
# ============================================================================

READ_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

WRITE_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


# ============================================================================
# 输出模型
# ============================================================================


class WriteToolOutput(BaseModel):
    """写工具统一出参（publish_requirement/update_requirement_relations/submit_dev_artifacts）。

    所有写工具产生审批批次，返回 batch_id + status + submitted_at + item_count。
    Agent 据此知道批次已提交，需等待 admin 审批。
    """

    batch_id: uuid.UUID = Field(description="审批批次 ID")
    status: str = Field(description="批次状态：pending_review")
    submitted_at: datetime = Field(description="提交时间（ISO 8601）")
    item_count: int = Field(description="审批项数量")

    @classmethod
    def from_batch(cls, batch) -> "WriteToolOutput":
        """从 ApprovalBatch ORM 对象构造输出。"""
        return cls(
            batch_id=batch.id,
            status=batch.status,
            submitted_at=batch.submitted_at,
            item_count=len(batch.items) if batch.items else 0,
        )


class ApprovalResultOutput(BaseModel):
    """审批结果出参（review_approve/review_reject）。"""

    batch_id: uuid.UUID = Field(description="审批批次 ID")
    status: str = Field(description="批次最终状态：approved/rejected")
    reviewed_at: datetime = Field(description="审批时间（ISO 8601）")
    conflict_hint: dict[str, Any] | None = Field(
        default=None,
        description="冲突检测结果（仅 approve 时返回，含 has_conflict/nodes_with_conflict/details/suggestion）",
    )


# ============================================================================
# 异常转换
# ============================================================================


def to_tool_error(exc: Exception) -> ToolError:
    """将 service 层异常转换为 ToolError（FastMCP 规范，工具层统一抛 ToolError）。

    PDD 硬约束：gateway 工具层不吞掉异常，转换后向上抛出由 FastMCP 处理。
    """
    if isinstance(exc, ToolError):
        return exc

    if isinstance(exc, PayloadValidationError):
        return ToolError(f"参数校验失败: {exc}")
    if isinstance(exc, SchemaValidationError):
        return ToolError(f"Schema 校验失败: {exc}")
    if isinstance(exc, BatchNotFoundError):
        return ToolError(f"批次不存在: {exc}")
    if isinstance(exc, BatchStatusError):
        return ToolError(f"批次状态错误: {exc}")
    if isinstance(exc, IdempotencyConflictError):
        return ToolError(f"幂等冲突: {exc}")
    if isinstance(exc, NodeNotFoundError):
        return ToolError(f"节点不存在: {exc}")

    # 未识别的异常，记录详细日志后返回通用错误信息（不暴露内部细节给 Agent）
    logger.exception("未处理的工具调用异常")
    return ToolError(f"工具调用失败: {type(exc).__name__}")


# ============================================================================
# Items 构造辅助（统一审批项格式，避免每个工具重复构造）
# ============================================================================


def build_node_item(
    *,
    ref: str,
    node_type: str,
    title: str,
    content: str,
    properties: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source: dict[str, Any] | None = None,
    project_id: uuid.UUID | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """构造 node + create 审批项（含 item_type/action/entity_type/payload 完整结构）。

    参数：
        ref: 批次内引用名（如 "requirement" / "LoginService"），供 edge item 引用
        node_type: 节点类型（Requirement/CodeSnippet/Solution/DesignIntent/Pitfall/ProjectProfile）
        title: 节点标题
        content: 节点内容
        properties: 节点属性（必填，含类型特有字段）
        tags: 标签数组（可选）
        source: 来源信息（可选，如 {"doc": "...", "url": "..."}）
        project_id: 归属项目 ID（必填，写入 payload 供 _execute_node_create 读取）
        created_by: 创建者 Access Key ID（必填，写入 payload 供 _execute_node_create 读取）

    返回：审批项 dict（submit_batch 接收的完整 item 结构）
        {
            "item_type": "node",
            "action": "create",
            "entity_type": node_type,
            "payload": {
                "ref": str,
                "node_type": str,
                "title": str,
                "content": str,
                "properties": dict,
                "tags": list[str],
                "source": dict,
                "project_id": str,
                "created_by": str,
            }
        }
    """
    if not properties:
        raise PayloadValidationError(f"节点 {ref} 缺少 properties 字段")
    if project_id is None:
        raise PayloadValidationError(f"节点 {ref} 缺少 project_id")
    if not created_by:
        raise PayloadValidationError(f"节点 {ref} 缺少 created_by")
    return {
        "item_type": "node",
        "action": "create",
        "entity_type": node_type,
        "payload": {
            "ref": ref,
            "node_type": node_type,
            "title": title,
            "content": content,
            "properties": properties,
            "tags": tags or [],
            "source": source or {},
            "project_id": str(project_id),
            "created_by": created_by,
        },
    }


def build_update_node_item(
    *,
    node_id: uuid.UUID,
    node_type: str,
    title: str | None = None,
    content: str | None = None,
    properties: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 node + update 审批项（走审批流修改已通过节点的内容）。

    与 build_node_item（create）生成 action="update" 的 item。payload 契约对齐
    approval.models.ApprovalItem：{"node_id", "title", "content", "properties",
    "tags", "source"}，审批通过时由 approval/service._execute_node_update 调用
    repository.update_node 落地（版本递增 + title 同步 AGE + 向量重算 + 审计）。

    字段语义与 repository.update_node 一致：None 表示不更新，properties 整体替换
    （不深度合并，调用方负责合并）。至少须提供一个变更字段；节点 type 不可变更，
    不由本工具提供（不写入 payload）。
    """
    if all(v is None for v in (title, content, properties, tags, source)):
        raise PayloadValidationError(
            "update 节点至少提供一个要变更的字段: title/content/properties/tags/source"
        )
    payload: dict[str, Any] = {"node_id": str(node_id)}
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["content"] = content
    if properties is not None:
        payload["properties"] = properties
    if tags is not None:
        payload["tags"] = tags
    if source is not None:
        payload["source"] = source
    return {
        "item_type": "node",
        "action": "update",
        "entity_type": node_type,
        "payload": payload,
    }


def build_edge_item(
    *,
    from_ref: str,
    to_ref: str,
    edge_type: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 edge + create 审批项（含 item_type/action/entity_type/payload 完整结构）。

    参数：
        from_ref: 源引用（ref 名 / UUID 字符串）
        to_ref: 目标引用（ref 名 / UUID 字符串）
        edge_type: 边类型（implements/depends_on/supersedes/relates_to/...）
        properties: 边属性（可选）

    返回：审批项 dict（submit_batch 接收的完整 item 结构）
        {
            "item_type": "edge",
            "action": "create",
            "entity_type": edge_type,
            "payload": {
                "from_ref": str,
                "to_ref": str,
                "edge_type": str,
                "properties": dict,
            }
        }

    注意：from_ref/to_ref 在审批通过时由 approval/service._resolve_ref 解析：
        - 优先匹配同批次已创建节点的 ref（通过 approval_item.payload.ref 反查 target_id）
        - 其次匹配 UUID 字符串（已有节点）
        - 解析失败抛 PayloadValidationError
    """
    if not from_ref or not to_ref:
        raise PayloadValidationError(
            f"边缺少 from_ref 或 to_ref: from_ref={from_ref}, to_ref={to_ref}"
        )
    return {
        "item_type": "edge",
        "action": "create",
        "entity_type": edge_type,
        "payload": {
            "from_ref": str(from_ref),
            "to_ref": str(to_ref),
            "edge_type": edge_type,
            "properties": properties or {},
        },
    }


# ============================================================================
# 角色 Skills 文档（从 src/mem_lake/skills/{role}/SKILL.md 加载）
# 符合 Agent Skills 标准（agentskills.io）：YAML frontmatter + Markdown body
# 基于PDD 8.3/8.4/8.5 表格生成
# ============================================================================

_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


def _load_skill_file(role: str) -> tuple[str, str]:
    """从文件系统加载角色 SKILL.md，返回 (markdown_body, version)。

    解析 YAML frontmatter 提取 version，返回 frontmatter 之后的 body。
    文件格式符合 Agent Skills 标准（agentskills.io）：
        ---
        name: mem-lake-{role}
        description: "..."
        version: 1.0.0
        ---
        # {Role} Skills
        ...

    加载时机：模块级（首次 import _shared.py 时执行），启动后缓存在内存，
    修改 skills 文件需重启服务。
    """
    skill_file = _SKILLS_DIR / role / "SKILL.md"
    content = skill_file.read_text(encoding="utf-8")
    # 解析 YAML frontmatter（首行 --- 开始，第二个 --- 结束）
    parts = content.split("---", 2)
    if len(parts) >= 3:
        frontmatter = yaml.safe_load(parts[1])
        body = parts[2].strip()
        version = str(frontmatter.get("version", "0.0.0"))
        return body, version
    # 无 frontmatter 的降级处理（不应发生，保持健壮性）
    return content, "0.0.0"


# 模块级加载（首次导入时执行，启动后缓存）
_ROLE_SKILLS_DATA: dict[str, tuple[str, str]] = {
    role: _load_skill_file(role) for role in ("pm", "dev", "admin")
}
ROLE_SKILLS_MD: dict[str, str] = {
    role: data[0] for role, data in _ROLE_SKILLS_DATA.items()
}
ROLE_SKILLS_VERSION = max(data[1] for data in _ROLE_SKILLS_DATA.values())


# ============================================================================
# Skills 安装指南（告知上游 agent 如何将 skills 放置到对应目录）
# 基于互联网可搜索到的常见 agent 放置格式，Mem Lake 不保证覆盖所有 agent
# ============================================================================

INSTALLATION_GUIDE = """## Skills 文件放置指南

将返回的 skills_markdown 内容保存为 SKILL.md 文件，根据你使用的 Agent 放置到对应目录。
首选跨客户端项目级路径（Claude Code / Cursor / Codex / Gemini 等主流 Agent 均识别）：

### 跨客户端项目级（推荐，首选）
- `.agents/skills/mem-lake-{role}/SKILL.md`
  （符合 Agent Skills 标准 agentskills.io，所有主流 Agent 通用；不确定就用这个）

### Claude Code
- 用户级（全局）：`~/.claude/skills/mem-lake-{role}/SKILL.md`
- 项目级（仅当前项目）：`.claude/skills/mem-lake-{role}/SKILL.md`

### Cursor
- 项目规则目录：`.cursor/rules/mem-lake-{role}.mdc`（将内容包装为 .mdc 格式）
- 或旧格式：`.cursorrules`（追加到现有文件）

### Codex CLI (OpenAI)
- 项目级技能目录：`.agents/skills/mem-lake-{role}/SKILL.md`（同首选）
- 或项目根目录：`AGENTS.md`（追加到现有文件，或创建新文件）
- 或 `.codex/rules/mem-lake-{role}.md`

### Gemini CLI
- 项目级：`.gemini/rules/mem-lake-{role}.md`

### 通用说明
- `{role}` 替换为你的实际角色（admin/pm/dev）
- 放置后重启 Agent 会话即可生效
- 如不确定你的 Agent 使用的目录格式，请查阅其官方文档或互联网搜索
"""
