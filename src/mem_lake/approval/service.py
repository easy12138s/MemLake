"""审批服务门面：对外 API 聚合导出。

包内四模块分工：
    repository.py  批次 CRUD 与提交 + 状态机常量与异常
    validation.py  payload/结构校验 + PayloadValidationError
    executor.py    审批执行写入（review_approve/review_reject/_execute_*）
    policy.py      冲突策略与自动审批

本文件仅聚合 re-export（外部沿用 `from mem_lake.approval.service import X`），
不承载实现逻辑，也不 commit（事务边界由调用方 gateway 工具控制）。
"""

from mem_lake.approval.conflict import detect_conflicts
from mem_lake.approval.executor import (
    _merge_conflict_hints,
    _to_uuid,
    review_approve,
    review_reject,
)
from mem_lake.approval.policy import auto_process_batch, submit_batch_with_mode
from mem_lake.approval.repository import (
    BATCH_TYPES,
    STATUS_APPROVED,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    TERMINAL_STATUSES,
    BatchNotFoundError,
    BatchStatusError,
    _find_by_idempotency_key,
    get_batch_detail,
    get_pending_batch,
    list_pending_batches,
    submit_batch,
)
from mem_lake.approval.validation import (
    PayloadValidationError,
    _validate_item_payload,
    _validate_item_structure,
)
from mem_lake.config import get_settings

__all__ = [
    "BATCH_TYPES",
    "BatchNotFoundError",
    "BatchStatusError",
    "PayloadValidationError",
    "STATUS_APPROVED",
    "STATUS_PENDING_REVIEW",
    "STATUS_REJECTED",
    "TERMINAL_STATUSES",
    "_find_by_idempotency_key",
    "_merge_conflict_hints",
    "_to_uuid",
    "_validate_item_payload",
    "_validate_item_structure",
    "auto_process_batch",
    "detect_conflicts",
    "get_batch_detail",
    "get_pending_batch",
    "get_settings",
    "list_pending_batches",
    "review_approve",
    "review_reject",
    "submit_batch",
    "submit_batch_with_mode",
]
