"""审批工作流模块：批次提交、审批通过（原子写入图谱）、退回。

对齐 PDD 3.4。状态机自建（pending_review → approved/rejected），单审批人（admin），
批次为审批单元，未审批内容暂存于 approval_item.payload 不写入正式存储。
审批通过时复用 knowledge.repository 写接口原子写入 knowledge_node 表与 AGE 图，
并基于 VectorSearcher 检测冲突生成 conflict_hint 供管理员决策。
"""

from mem_lake.approval.conflict import detect_conflicts
from mem_lake.approval.models import ApprovalBatch, ApprovalItem
from mem_lake.approval.service import (
    BATCH_TYPES,
    STATUS_APPROVED,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    TERMINAL_STATUSES,
    BatchNotFoundError,
    BatchStatusError,
    IdempotencyConflictError,
    PayloadValidationError,
    get_batch_detail,
    list_pending_batches,
    review_approve,
    review_reject,
    submit_batch,
)

__all__ = [
    "ApprovalBatch",
    "ApprovalItem",
    "BATCH_TYPES",
    "BatchNotFoundError",
    "BatchStatusError",
    "IdempotencyConflictError",
    "PayloadValidationError",
    "STATUS_APPROVED",
    "STATUS_PENDING_REVIEW",
    "STATUS_REJECTED",
    "TERMINAL_STATUSES",
    "detect_conflicts",
    "get_batch_detail",
    "list_pending_batches",
    "review_approve",
    "review_reject",
    "submit_batch",
]
