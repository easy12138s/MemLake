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
    get_batch_detail,
    list_pending_batches,
    review_approve as approval_review_approve,
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
    project_id: uuid.UUID = Field(description="归属项目 ID")
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
    project_id: uuid.UUID = Field(description="归属项目 ID")
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
        except (BatchNotFoundError, Exception) as e:
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

    warning_days = getattr(settings, "APPROVAL_WARNING_DAYS", 7)
    timeout_days = getattr(settings, "APPROVAL_TIMEOUT_DAYS", 30)

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
