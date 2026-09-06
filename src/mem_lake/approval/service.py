"""审批服务门面（FIX-24 拆包后：对外 API 门面聚合导出）。

FIX-24：原本 999 行 / 9 类职责的 service.py 已拆分为包内四模块：
    repository.py  批次 CRUD 与提交（submit_batch/_create_batch/list_pending_batches/
                   get_batch_detail/_find_by_idempotency_key）+ 状态机常量与异常
    validation.py  payload/结构校验（_validate_item_*）+ PayloadValidationError
    executor.py    审批执行写入（review_approve/review_reject/_execute_*/
                   _build_conflict_query_vectors/_merge_conflict_hints/_to_uuid）
    policy.py      冲突策略与自动审批（auto_process_batch/submit_batch_with_mode）

本文件保留为对外门面：仅聚合 import 上述符号并重新导出，外部代码沿用
`from mem_lake.approval.service import X` 路径不变（含下划线私有符号），
不承载任何实现逻辑，也不 commit（事务边界仍由调用方 gateway 工具控制）。
"""

from mem_lake.approval.conflict import detect_conflicts
from mem_lake.approval.executor import (
    _build_conflict_query_vectors,
    _execute_edge_create,
    _execute_node_create,
    _execute_node_update,
    _merge_conflict_hints,
    _resolve_ref,
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
    IdempotencyConflictError,
    _find_by_idempotency_key,
    get_batch_detail,
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
    "IdempotencyConflictError",
    "PayloadValidationError",
    "STATUS_APPROVED",
    "STATUS_PENDING_REVIEW",
    "STATUS_REJECTED",
    "TERMINAL_STATUSES",
    "_build_conflict_query_vectors",
    "_execute_edge_create",
    "_execute_node_create",
    "_execute_node_update",
    "_find_by_idempotency_key",
    "_merge_conflict_hints",
    "_resolve_ref",
    "_to_uuid",
    "_validate_item_payload",
    "_validate_item_structure",
    "auto_process_batch",
    "detect_conflicts",
    "get_batch_detail",
    "get_settings",
    "list_pending_batches",
    "review_approve",
    "review_reject",
    "submit_batch",
    "submit_batch_with_mode",
]
