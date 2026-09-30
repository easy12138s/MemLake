"""工具共享辅助：ToolAnnotations 常量 + 输出模型 + 异常转换 + items 构造 + scope 兜底。

本模块是 gateway/tools/ 下所有工具模块的共享基础设施，避免重复代码。
对齐 PDD 6.1 工具表 + 8.3/8.4/8.5 Skills 文档。
"""

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from mem_lake.approval.service import (
    BatchNotFoundError,
    BatchStatusError,
    IdempotencyConflictError,
    PayloadValidationError,
    submit_batch_with_mode,
)
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    get_current_lax_mode,
    transactional_session,
)
from mem_lake.knowledge.repository import NodeNotFoundError
from mem_lake.knowledge.schema import SchemaValidationError, validate_attribution

logger = logging.getLogger("mem_lake.gateway.tools.shared")

if TYPE_CHECKING:
    # 仅用于类型标注：lifespan 共享资源的真实类型在 gateway/server.py 定义，
    # 运行期不 import（避免工具↔server 的模块级循环依赖）。
    from mem_lake.approval.models import ApprovalBatch
    from mem_lake.gateway.server import LifespanContext


# ============================================================================
# 严格输入基类
# ============================================================================


class StrictInputModel(BaseModel):
    """工具输入模型基类：拒绝未知字段。

    网关是 LLM Agent 的 API 边界，Agent 常见错误是把顶层参数误嵌套进子对象
    （如把 relations 放进 artifacts）。Pydantic 默认静默忽略未知字段，
    会导致整段数据无声丢失且调用方毫无感知——比报错更危险。
    输入模型统一继承本基类，未知字段直接校验失败，让调用方立即纠正。
    （输出模型仍用普通 BaseModel，服务端自控无此风险。）
    """

    model_config = ConfigDict(extra="forbid")


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

    所有写工具产生审批批次。严格模式（默认）返回 status=pending_review，Agent 据此知道
    批次已提交、需等待 admin 审批；宽松模式（提交方 Access Key 为 lax 且全局开关开启）
    提交即自动处理：无冲突 status=approved + decision=auto_approved（已入库），有冲突
    status=pending_review + decision=needs_human_review（仍在审批队列）。
    """

    batch_id: uuid.UUID = Field(description="审批批次 ID")
    status: str = Field(
        description="批次状态：pending_review（严格模式/待审批）/ approved（宽松模式已入库）"
    )
    submitted_at: datetime = Field(description="提交时间（ISO 8601）")
    item_count: int = Field(description="审批项数量")
    decision: str | None = Field(
        default=None,
        description=(
            "宽松模式自动处理决策：auto_approved（已直接入库）/ needs_human_review"
            "（有冲突，批次停在 pending 需 admin 处理）；严格模式为 None"
        ),
    )
    created: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "已入库节点清单（宽松模式 auto_approved 时返回）："
            "[{ref, node_id, node_type, title}]——写入回执的确定性依据"
            "（09-29 报告 P1-2：此前不返回 node_id，写入最后一公里靠 Agent 反查）"
        ),
    )
    edges_created: int | None = Field(
        default=None,
        description="已建立的边数量（宽松模式 auto_approved 时返回）",
    )

    @classmethod
    def from_batch(
        cls, batch: "ApprovalBatch", decision: str | None = None
    ) -> "WriteToolOutput":
        """从 ApprovalBatch ORM 对象构造输出。

        宽松模式 auto_approved：附 created（node 项 target_id 已回填）与
        edges_created（批次内 edge 项数；建边失败即整批回滚，无部分失败态）。
        """
        created = None
        edges_created = None
        if decision == "auto_approved" and batch.items:
            created = [
                {
                    "ref": (it.payload or {}).get("ref"),
                    "node_id": str(it.target_id),
                    "node_type": it.entity_type,
                    "title": (it.payload or {}).get("title"),
                }
                for it in batch.items
                if it.item_type == "node"
                and it.action == "create"
                and it.target_id is not None
            ]
            edges_created = sum(
                1
                for it in batch.items
                if it.item_type == "edge"
            )
        return cls(
            batch_id=batch.id,
            status=batch.status,
            submitted_at=batch.submitted_at,
            item_count=len(batch.items) if batch.items else 0,
            decision=decision,
            created=created,
            edges_created=edges_created,
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
    system_id: uuid.UUID | None = None,
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
        project_id: 归属项目 ID（Requirement 可为空=悬浮；其余类型必填）
        system_id: 归属 system 域（仅 Requirement 必填；跨项目需求建模）
        created_by: 创建者 Access Key ID（必填，写入 payload 供 _execute_node_create 读取）

    归属约束（与 create_node 一致）：
        - Requirement：system_id 必填，project_id 可空（悬浮）
        - 其余类型：project_id 必填

    返回：审批项 dict（submit_batch 接收的完整 item 结构）
        {
            "item_type": "node",
            "action": "create",
            "entity_type": node_type,
            "payload": {
                "ref": str, "node_type": str, "title": str, "content": str,
                "properties": dict, "tags": list[str], "source": dict,
                "project_id": str|None, "system_id": str|None, "created_by": str,
            }
        }
    """
    if not properties:
        raise PayloadValidationError(f"节点 {ref} 缺少 properties 字段")
    if not created_by:
        raise PayloadValidationError(f"节点 {ref} 缺少 created_by")
    # 归属约束统一走 schema.validate_attribution（FIX-17 单一实现）；
    # 工具层对外仍包装为 PayloadValidationError（含 ref 便于定位批次内项）。
    try:
        validate_attribution(node_type, system_id=system_id, project_id=project_id)
    except SchemaValidationError as e:
        raise PayloadValidationError(f"节点 {ref} 归属校验失败: {e}") from e
    payload: dict[str, Any] = {
        "ref": ref,
        "node_type": node_type,
        "title": title,
        "content": content,
        "properties": properties,
        "tags": tags or [],
        "source": source or {},
        "project_id": str(project_id) if project_id is not None else None,
        "system_id": str(system_id) if system_id is not None else None,
        "created_by": created_by,
    }
    return {
        "item_type": "node",
        "action": "create",
        "entity_type": node_type,
        "payload": payload,
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
                "properties": dict,
            }
        }

    注意：from_ref/to_ref 在审批通过时由 approval/service._resolve_ref 解析：
        - 优先匹配同批次已创建节点的 ref（通过 approval_item.payload.ref 反查 target_id）
        - 其次匹配 UUID 字符串（已有节点）
        - 解析失败抛 PayloadValidationError
        边类型只存于 entity_type（审批执行侧 _execute_edge_create 从此读取），
        payload 不重复携带。
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
            "properties": properties or {},
        },
    }


def resolve_search_scope_fallback(
    role: str,
    system_scope: list[str],
    *,
    project_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """检索 scope 兜底：调用方未传 scope 时按 Key 的 system_scope 回收。

    规则（局促性收敛——绝不隐式跨 system 检索）：
    - 任传一个 ID → 原样返回（调用方的显式选择，不做放大）
    - admin → 维持「至少提供一个」的硬错误（admin 无 system scope 概念，
      避免 accidental 全库检索）
    - 非 admin + claims 恰好绑定 1 个 system → 默认按该 system 检索
    - 非 admin + claims 0 个 system → 报错「未绑定 system，请显式传入」
    - 非 admin + claims >1 个 system → 报错列出候选（id），不聚合检索防混叠
    """
    if project_id is not None or system_id is not None:
        return project_id, system_id

    if role == "admin":
        raise ValueError("project_id 与 system_id 至少提供一个")

    if not system_scope:
        raise ValueError(
            "当前 Access Key 未绑定任何 system，请显式传入 system_id（或 project_id）"
        )
    if len(system_scope) == 1:
        return project_id, uuid.UUID(system_scope[0])
    raise ValueError(
        f"当前 Access Key 绑定多个 system（{len(system_scope)} 个），"
        f"请显式传入 system_id 二选一；候选: {sorted(system_scope)}"
    )




# ============================================================================
# 写入/审批后向量补全入队（宽松直接入库与审批通用，共享实现避免循环导入）
# ============================================================================


async def _safe_enqueue_embed(
    project_id: uuid.UUID | None, node_ids: list[uuid.UUID]
) -> None:
    """写入/审批提交后安全入队向量补全任务。

    事务已 commit，入队仅是后台优化（新建节点暂无 node_embedding 向量记录，搜索可安全
    跳过）。入队失败（如 DB 短暂不可用）只记录告警，不阻断结果返回——已生效，向量
    缺失由后续 reindex 兜底（AUDIT §2.11）。
    """
    # 惰性导入避免与 background_tasks 的循环依赖
    from mem_lake.gateway.background_tasks import start_embed_nodes_task

    try:
        await start_embed_nodes_task(project_id, node_ids, get_current_key_id())
    except Exception:  # noqa: BLE001 - 入队失败仅告警，不阻断主流程
        logger.exception(
            "写入成功但向量补全入队失败，请后续手动 reindex 补全: "
            "project=%s node_count=%d",
            project_id,
            len(node_ids),
        )


# ============================================================================
# 写工具提交批次公共流程（4 个写工具共享：宽松模式资源解析 + 提交 + 出参收尾）
# ============================================================================


def get_lifespan_context() -> "LifespanContext":
    """返回类型化的 lifespan 共享资源容器。

    FastMCP 的 ``get_context().lifespan_context`` 类型标注为 ``dict``（fastmcp
    泛化 type alias），但运行时是 ``gateway.server.LifespanContext`` 实例（@lifespan
    yield 的对象，含 graph_store/embedding_client/vector_searcher 属性）。工具层
    统一经本 helper 取回类型化上下文，避免各工具各自 cast / type: ignore。
    """
    ctx = get_context()
    return cast("LifespanContext", ctx.lifespan_context)


def _lax_lifespan_resources() -> tuple[Any, Any, Any]:
    """宽松模式下从 lifespan context 取图谱/嵌入/检索依赖。

    返回 (graph_store, embedding_client, vector_searcher)；strict 模式无需调用。
    """
    lifespan_ctx = get_lifespan_context()
    return (
        lifespan_ctx.graph_store,
        lifespan_ctx.embedding_client,
        lifespan_ctx.vector_searcher,
    )


async def submit_write_batch(
    *,
    project_id: uuid.UUID | None,
    batch_type: str,
    submitter_role: str,
    items: list[dict[str, Any]],
    operation_id: str | None,
) -> WriteToolOutput:
    """写工具统一提交流程：开事务 → 按模式提交批次 → 宽松模式收尾入队补向量。

    收敛 4 个写工具（publish_requirement / update_requirement_relations /
    submit_dev_artifacts / update_node）的重复提交块；get_current_lax_mode
    单次调用内只取一次。
    """
    lax = get_current_lax_mode()
    async with transactional_session() as session:
        graph_store = embedding_client = vector_searcher = None
        if lax:
            graph_store, embedding_client, vector_searcher = (
                _lax_lifespan_resources()
            )
        batch, decision = await submit_batch_with_mode(
            session,
            project_id=project_id,
            batch_type=batch_type,
            submitted_by=get_current_key_id(),
            submitter_role=submitter_role,
            items=items,
            operation_id=operation_id,
            lax_mode=lax,
            graph_store=graph_store,
            embedding_client=embedding_client,
            vector_searcher=vector_searcher,
        )

    # 宽松模式提交后收尾：已自动审批时异步入队补向量，并构造出参。
    # strict 模式（lax=False）不触发入队，仅构造包含 decision=None 的出参。
    created = [
        it.target_id
        for it in (batch.items or [])
        if it.item_type == "node"
        and it.action == "create"
        and it.target_id is not None
    ]
    if lax and decision == "auto_approved" and created and project_id is not None:
        await _safe_enqueue_embed(project_id, created)
    return WriteToolOutput.from_batch(batch, decision=decision)
