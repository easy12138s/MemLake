"""工具共享辅助：ToolAnnotations 常量 + 输出模型 + 异常转换 + items 构造 + 角色文档。

本模块是 gateway/tools/ 下所有工具模块的共享基础设施，避免重复代码。
对齐 PDD 6.1 工具表 + 8.3/8.4/8.5 Skills 文档。
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from mem_lake.approval.service import (
    BatchNotFoundError,
    BatchStatusError,
    IdempotencyConflictError,
    PayloadValidationError,
)
from fastmcp.exceptions import ToolError
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
# 角色 Skills 文档（M6a 骨架，M6b 完善）
# 基于PDD 8.3/8.4/8.5 表格生成
# ============================================================================

ROLE_SKILLS_MD: dict[str, str] = {
    "pm": """# PM Skills（产品经理）

## 你的角色
你是项目的 PM，负责需求管理。通过 Mem Lake MCP 工具发布和维护需求节点。

## 可用工具
- `publish_requirement`：发布需求节点（含版本关系与关联关系），产生审批批次
- `update_requirement_relations`：更新需求间关系（冲突/关联/替代）
- `get_role_skills`：获取角色 Skills 指导文档

## 工作流
1. 调用 `publish_requirement` 提交新需求，获得 batch_id
2. 等待 admin 审批通过后，需求节点写入知识图谱
3. 需求变更时调用 `update_requirement_relations` 更新关系

## 关键约束
- 需求节点的 properties 必须包含：requirement_id, priority, module, acceptance_criteria
- related.supersedes/relates_to 中的 requirement_id 必须为已有 Requirement 节点
- 所有写操作产生审批批次，需 admin 审批通过后才生效

（M6b 将完善详细 Skills 指导）
""",
    "dev": """# Dev Skills（开发者）

## 你的角色
你是项目的开发者，负责提交开发产物（代码片段/方案/意图/踩坑）。通过 Mem Lake MCP 工具批量提交开发产物。

## 可用工具
- `submit_dev_artifacts`：批量提交开发产物，产生审批批次
- `get_role_skills`：获取角色 Skills 指导文档

## 工作流
1. 调用 `submit_dev_artifacts` 批量提交代码片段/方案/意图/踩坑
2. 使用 ref 机制在批次内引用未创建的节点
3. relations 中用 from_ref/to_ref 引用 ref 名或已有节点 UUID
4. 等待 admin 审批通过后，产物节点写入知识图谱

## 关键约束
- 每个产物必须声明 ref 名（如 "LoginService"），供 relations 引用
- code_snippets 的 properties 必须包含：name, type, responsibility, file_path
- solutions 的 properties 必须包含：approach, alternatives
- design_intents 的 properties 必须包含：rationale, trade_offs
- pitfalls 的 properties 必须包含：symptom, root_cause, solution, severity
- 自动构造 Requirement--implements-->CodeSnippet 关系
- 临时引用在审批通过时解析为实际节点 ID

（M6b 将完善详细 Skills 指导）
""",
    "admin": """# Admin Skills（管理员）

## 你的角色
你是项目的管理员，负责审批管理、Access Key 管理、项目画像维护。通过 Mem Lake MCP 工具管理知识图谱的写入。

## 可用工具
- `review_pending_list`：查询待审批批次队列
- `review_batch_detail`：查看批次内所有审批项的完整内容
- `review_approve`：审批通过批次（原子性写入图谱）
- `review_reject`：审批退回批次（附原因）
- `manage_access_key`：创建/吊销/查看 Access Key
- `manage_project_profile`：直接写入 ProjectProfile 节点（不走审批流）
- `get_role_skills`：获取角色 Skills 指导文档

## 工作流
1. 调用 `review_pending_list` 查看待审批批次
2. 调用 `review_batch_detail` 查看批次详情
3. 调用 `review_approve` 或 `review_reject` 审批
4. 审批通过时自动检测冲突（conflict_hint），但不阻断审批
5. 通过 `manage_access_key` 为 PM/Dev 创建 Access Key
6. 通过 `manage_project_profile` 维护项目画像

## 关键约束
- 审批通过是原子操作（节点+边+审计日志同一事务）
- conflict_hint 仅作提示，不阻断审批
- manage_project_profile 直接写入，不走审批流
- Access Key 明文仅创建时返回一次，需安全保存

（M6b 将完善详细 Skills 指导）
""",
}

ROLE_SKILLS_VERSION = "0.1.0"
