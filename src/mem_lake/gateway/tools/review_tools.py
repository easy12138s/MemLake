"""审批类工具：admin 对 pending_review 批次的查询与决策。

工具职责：转发 approval/service 的查询与审批操作，不在工具层写业务逻辑。
review_approve 是唯一触发知识图谱写入的入口（原子性写入由 service 层完成）。

包含工具（PDD 6.1 Admin 工具表）：
- review_pending_list：查询待审批批次队列（含超期标记）
- review_batch_detail：查看批次内所有审批项详情
- review_approve：审批通过批次（原子性写入图谱 + 冲突检测）
- review_reject：审批退回批次（附原因，不写入图谱）

设计要点：
- 角色 RBAC 由中间件层控制（admin 专属），本文件不区分角色
- review_approve/review_reject 控制事务边界，service 层不 commit
- conflict_hint 仅作提示不阻断审批（PDD 3.4）
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import BaseModel, Field

from mem_lake.approval.service import (
    BatchNotFoundError,
    BatchStatusError,
    auto_process_batch,
    get_batch_detail,
    list_pending_batches,
)
from mem_lake.approval.service import (
    review_approve as approval_review_approve,
)
from mem_lake.approval.service import (
    review_reject as approval_review_reject,
)
from mem_lake.config import get_settings
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    get_readonly_session,
    transactional_session,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    WRITE_TOOL_ANNOTATIONS,
    ApprovalResultOutput,
    _safe_enqueue_embed,
    to_tool_error,
)
from mem_lake.knowledge.repository import NodeNotFoundError
from mem_lake.knowledge.schema import SchemaValidationError

logger = logging.getLogger("mem_lake.gateway.tools.review")


# ============================================================================
# 输出模型
# ============================================================================


class PendingBatchItem(BaseModel):
    """待审批批次列表项。"""

    batch_id: uuid.UUID = Field(description="批次 ID")
    project_id: uuid.UUID | None = Field(
        default=None, description="归属项目 ID（悬浮 system 需求批次为 None）"
    )
    batch_type: str = Field(description="批次类型")
    submitted_by: str = Field(description="提交者 Access Key ID")
    submitted_at: datetime = Field(description="提交时间")
    summary: str = Field(description="批次摘要")
    is_warning: bool = Field(description="即将超期（>7 天）")
    is_timeout: bool = Field(description="已超期（>30 天）")


class ReviewPendingListOutput(BaseModel):
    """review_pending_list 工具出参。"""

    batches: list[PendingBatchItem] = Field(description="批次列表")
    total: int = Field(description="返回数量（非总数）")


class ApprovalItemOutput(BaseModel):
    """审批项详情。"""

    seq: int = Field(description="序号")
    item_type: str = Field(description="item 类型：node/edge")
    action: str = Field(description="操作：create/update/delete")
    entity_type: str = Field(description="实体类型（节点类型或边类型）")
    payload: dict[str, Any] = Field(description="审批项 payload")
    target_id: uuid.UUID | None = Field(
        default=None, description="审批通过后回填的实际节点 ID"
    )


class ReviewBatchDetailOutput(BaseModel):
    """review_batch_detail 工具出参。"""

    batch_id: uuid.UUID = Field(description="批次 ID")
    project_id: uuid.UUID | None = Field(
        default=None, description="归属项目 ID（悬浮 system 需求批次为 None）"
    )
    batch_type: str = Field(description="批次类型")
    submitted_by: str = Field(description="提交者 Access Key ID")
    submitter_role: str = Field(description="提交者角色")
    summary: str = Field(description="批次摘要")
    status: str = Field(description="批次状态")
    submitted_at: datetime = Field(description="提交时间")
    reviewed_by: str | None = Field(default=None, description="审批者")
    reviewed_at: datetime | None = Field(default=None, description="审批时间")
    review_comment: str | None = Field(default=None, description="审批意见")
    conflict_hint: dict[str, Any] | None = Field(
        default=None, description="冲突检测结果（仅 approve 后有值）"
    )
    operation_id: str | None = Field(default=None, description="幂等键")
    items: list[ApprovalItemOutput] = Field(description="审批项列表")


class AutoProcessOutput(BaseModel):
    """review_auto_process 工具出参。

    为 Admin Agent 设计：无冲突时 decision=auto_approved 且批次已写入图谱，
    有冲突时 decision=needs_human_review 且批次保持 pending_review 等待人工决策。
    Agent 据 decision 决定是否需向人类 admin 描述冲突并等待决策。
    """

    batch_id: uuid.UUID = Field(description="批次 ID")
    decision: str = Field(
        description="决策结果：auto_approved（自动通过）/ needs_human_review（升级人工审查）"
    )
    status: str = Field(
        description="批次最终状态：approved（auto_approved）/ pending_review（needs_human_review）"
    )
    conflict_hint: dict[str, Any] = Field(
        description=(
            "冲突检测详情。auto_approved 时含 has_conflict=False/checked_nodes/candidates_examined；"
            "needs_human_review 时含 has_conflict=True/conflicting_nodes/suggestion"
        )
    )
    summary: str = Field(description="批次摘要（供 Agent 向人类 admin 描述时使用）")
    batch_type: str = Field(description="批次类型")
    submitted_by: str = Field(description="提交者 Access Key ID")
    item_count: int = Field(description="审批项数量")


# ============================================================================
# 工具注册
# ============================================================================


def register_review_tools(mcp: FastMCP) -> None:
    """注册审批类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def review_pending_list(
        project_id: uuid.UUID | None = Field(
            default=None, description="项目 ID 过滤，None 表示所有项目"
        ),
        limit: int = Field(default=50, description="返回数量上限"),
        offset: int = Field(default=0, description="分页偏移"),
    ) -> ReviewPendingListOutput:
        """查询待审批批次队列（含摘要、提交者、时间、超期标记）。

        Admin 工具。返回 pending_review 状态的批次列表，含超期预警标记。
        is_warning=true 表示提交超过 7 天即将超期，is_timeout=true 表示已超期（>30 天）。
        """
        try:
            session = await get_readonly_session()
            try:
                batches = await list_pending_batches(
                    session,
                    project_id=project_id,
                    limit=limit,
                    offset=offset,
                )
                items = [_to_pending_batch_item(b) for b in batches]
                return ReviewPendingListOutput(batches=items, total=len(items))
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)
    async def review_batch_detail(
        batch_id: uuid.UUID = Field(description="审批批次 ID"),
    ) -> ReviewBatchDetailOutput:
        """查看审批批次内所有审批项的完整内容（节点/边 payload 详情）。

        Admin 工具。审批前查看批次详情，了解提交内容后再决定 approve/reject。
        target_id 为审批通过后回填的实际节点 ID（pending_review 状态下为 None）。
        """
        try:
            session = await get_readonly_session()
            try:
                batch = await get_batch_detail(session, batch_id)
                return _to_batch_detail_output(batch)
            finally:
                await session.close()
        except Exception as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def review_approve(
        batch_id: uuid.UUID = Field(description="审批批次 ID"),
        review_comment: str | None = Field(
            default=None, description="审批意见（可选）"
        ),
    ) -> ApprovalResultOutput:
        """审批通过批次，原子性写入知识图谱（节点+边+审计日志同一事务）。

        Admin 工具。审批通过后：节点写入 knowledge_node 表 + AGE 图节点 + 生成向量；
        边写入 AGE 图；conflict_hint 返回冲突检测结果（不阻断审批，仅提示）。
        临时引用（from_ref/to_ref）在此时解析为实际节点 ID。
        """
        try:
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context

            async with transactional_session() as session:
                batch = await approval_review_approve(
                    session,
                    batch_id=batch_id,
                    reviewed_by=get_current_key_id(),
                    graph_store=lifespan_ctx.graph_store,
                    embedding_client=lifespan_ctx.embedding_client,
                    vector_searcher=lifespan_ctx.vector_searcher,
                    review_comment=review_comment,
                )
                # 新建节点 id 取自审批后 batch.items 中 node+create 项的 target_id
                created_node_ids = [
                    it.target_id
                    for it in batch.items
                    if it.item_type == "node"
                    and it.action == "create"
                    and it.target_id is not None
                ]
            # 审批已提交（事务已 commit）：将新建节点（content_vector 暂为 NULL）
            # 异步入队补向量，复用 reindex worker，避免大批次审批阻塞 MCP 调用超时。
            # 入队失败不阻断审批结果——审批已生效，向量缺失由后续 reindex 兜底
            #（AUDIT §2.11：避免"审批成功但工具报错"的 Agent 误判重试窗口）。
            if created_node_ids:
                await _safe_enqueue_embed(batch.project_id, created_node_ids)
            return ApprovalResultOutput(
                batch_id=batch.id,
                status=batch.status,
                reviewed_at=batch.reviewed_at,
                conflict_hint=batch.conflict_hint,
            )
        except (
            BatchNotFoundError,
            BatchStatusError,
            NodeNotFoundError,
            SchemaValidationError,
        ) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def review_reject(
        batch_id: uuid.UUID = Field(description="审批批次 ID"),
        review_comment: str = Field(description="拒绝原因（必填）"),
    ) -> ApprovalResultOutput:
        """审批退回批次（附原因），不写入知识图谱。

        Admin 工具。批次状态转为 rejected（终态），不执行任何图谱写入。
        review_comment 为必填，需说明退回原因供提交者参考。
        """
        if not review_comment or not review_comment.strip():
            raise to_tool_error(ValueError("review_comment 不能为空"))
        try:
            async with transactional_session() as session:
                batch = await approval_review_reject(
                    session,
                    batch_id=batch_id,
                    reviewed_by=get_current_key_id(),
                    review_comment=review_comment,
                )
            return ApprovalResultOutput(
                batch_id=batch.id,
                status=batch.status,
                reviewed_at=batch.reviewed_at,
                conflict_hint=None,
            )
        except (BatchNotFoundError, BatchStatusError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def review_auto_process(
        batch_id: uuid.UUID = Field(description="审批批次 ID"),
    ) -> AutoProcessOutput:
        """自动处理审批批次：检测冲突，无冲突自动通过，有冲突返回详情供人工决策。

        Admin 工具，为 Agent 设计（非人类直接使用）。调用后：
        - decision="auto_approved"：批次已自动审批通过（无冲突），节点写入图谱，
          无需人工介入
        - decision="needs_human_review"：检测到冲突，批次保持 pending_review，
          Agent 需向人类 admin 描述冲突详情（节点标题/相似度/匹配属性）并等待决策，
          人类确认通过后调用 review_approve，拒绝则调用 review_reject

        冲突检测使用三层架构：同项目同类型（L1）→ 关键属性比对（L2）→ 内容语义
        相似度 ≥ CONFLICT_SIMILARITY_THRESHOLD（L3，默认 0.85，用标题+正文做向量对比，
        换 embedding 模型后需重新标定）。三层全部通过才视为冲突。
        """
        try:
            ctx = get_context()
            lifespan_ctx = ctx.lifespan_context

            async with transactional_session() as session:
                result = await auto_process_batch(
                    session,
                    batch_id=batch_id,
                    reviewed_by=get_current_key_id(),
                    graph_store=lifespan_ctx.graph_store,
                    embedding_client=lifespan_ctx.embedding_client,
                    vector_searcher=lifespan_ctx.vector_searcher,
                )
            batch = result["batch"]
            # 审批已提交：将新建节点（content_vector 暂为 NULL）异步入队补向量，
            # 复用 reindex worker；入队失败不阻断（AUDIT §2.11，见 review_approve）。
            created_node_ids = result.get("created_node_ids") or []
            if created_node_ids:
                await _safe_enqueue_embed(batch.project_id, created_node_ids)
            return AutoProcessOutput(
                batch_id=batch.id,
                decision=result["decision"],
                status=batch.status,
                conflict_hint=result["conflict_hint"],
                summary=batch.summary,
                batch_type=batch.batch_type,
                submitted_by=batch.submitted_by,
                item_count=len(batch.items) if batch.items else 0,
            )
        except (
            BatchNotFoundError,
            BatchStatusError,
            NodeNotFoundError,
            SchemaValidationError,
        ) as e:
            raise to_tool_error(e)


# ============================================================================
# 转换辅助函数
# ============================================================================


def _to_pending_batch_item(batch) -> PendingBatchItem:
    """从 ApprovalBatch ORM 对象构造 PendingBatchItem。"""
    from datetime import timezone

    settings = get_settings()
    now = datetime.now(timezone.utc)
    # 确保 submitted_at 是 timezone-aware
    submitted_at = batch.submitted_at
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    age_days = (now - submitted_at).days

    warning_days = settings.APPROVAL_WARNING_DAYS
    timeout_days = settings.APPROVAL_TIMEOUT_DAYS

    return PendingBatchItem(
        batch_id=batch.id,
        project_id=batch.project_id,
        batch_type=batch.batch_type,
        submitted_by=batch.submitted_by,
        submitted_at=submitted_at,
        summary=batch.summary,
        is_warning=warning_days <= age_days < timeout_days,
        is_timeout=age_days >= timeout_days,
    )


def _to_batch_detail_output(batch) -> ReviewBatchDetailOutput:
    """从 ApprovalBatch ORM 对象构造 ReviewBatchDetailOutput。"""
    return ReviewBatchDetailOutput(
        batch_id=batch.id,
        project_id=batch.project_id,
        batch_type=batch.batch_type,
        submitted_by=batch.submitted_by,
        submitter_role=batch.submitter_role,
        summary=batch.summary,
        status=batch.status,
        submitted_at=batch.submitted_at,
        reviewed_by=batch.reviewed_by,
        reviewed_at=batch.reviewed_at,
        review_comment=batch.review_comment,
        conflict_hint=batch.conflict_hint,
        operation_id=batch.operation_id,
        items=[
            ApprovalItemOutput(
                seq=item.seq,
                item_type=item.item_type,
                action=item.action,
                entity_type=item.entity_type,
                payload=item.payload,
                target_id=item.target_id,
            )
            for item in batch.items
        ],
    )
