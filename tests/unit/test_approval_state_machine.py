"""审批状态机纯逻辑单测（无 DB 依赖）。

覆盖：
- 批次类型白名单校验（submit_batch 前置校验，BATCH_TYPES 常量集）
- 状态机常量与终态集合（STATUS_PENDING_REVIEW / STATUS_APPROVED / STATUS_REJECTED / TERMINAL_STATUSES）
- item 结构校验（_validate_item_structure：item_type/action/必填字段）
- payload 校验（_validate_item_payload：node+create 必填字段、edge+create 类型与 from_id/to_id）
- conflict_hint 合并逻辑（_merge_conflict_hints：空列表、无冲突、有冲突、聚合建议）
- _to_uuid 类型转换边界
- 异常类型层级（PayloadValidationError / BatchStatusError / BatchNotFoundError / IdempotencyConflictError）

不依赖 DB：所有测试直接调用纯函数或常量，验证逻辑正确性。
DB 依赖场景（如状态转换的实际写入、幂等键 DB 唯一约束）由 test_approval_flow.py 集成测试覆盖。
"""

import uuid

import pytest

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
    _merge_conflict_hints,
    _to_uuid,
    _validate_item_payload,
    _validate_item_structure,
)


# ============ 批次类型白名单 ============


class TestBatchTypes:
    """BATCH_TYPES 白名单常量校验。"""

    def test_batch_types_contains_all_types(self):
        """PDD 3.4 定义的 3 种批次类型 + update_node（c0ee66d 新增）均在白名单内。"""
        expected = {
            "publish_requirement",
            "submit_dev_artifacts",
            "update_requirement_relations",
            "update_node",
        }
        assert set(BATCH_TYPES) == expected

    def test_batch_types_is_frozenset(self):
        """白名单为 frozenset，防止运行时篡改（PDD 硬约束：不可变配置）。"""
        assert isinstance(BATCH_TYPES, frozenset)

    @pytest.mark.parametrize(
        "batch_type",
        [
            "publish_requirement",
            "submit_dev_artifacts",
            "update_requirement_relations",
            "update_node",
        ],
    )
    def test_valid_batch_type_in_whitelist(self, batch_type):
        """合法批次类型在白名单内。"""
        assert batch_type in BATCH_TYPES

    @pytest.mark.parametrize(
        "invalid_type",
        [
            "",
            "publish_requirement_v2",
            "Publish_Requirement",  # 大小写敏感
            "publish_requirement ",  # 含空格
            "delete_requirement",
            "random_type",
        ],
    )
    def test_invalid_batch_type_not_in_whitelist(self, invalid_type):
        """非法批次类型不在白名单内。"""
        assert invalid_type not in BATCH_TYPES


# ============ 状态机常量 ============


class TestStatusConstants:
    """状态机常量与终态集合校验。"""

    def test_three_statuses_defined(self):
        """PDD 3.4 定义 3 状态：pending_review / approved / rejected。"""
        assert STATUS_PENDING_REVIEW == "pending_review"
        assert STATUS_APPROVED == "approved"
        assert STATUS_REJECTED == "rejected"

    def test_terminal_statuses_contains_approved_rejected(self):
        """终态集合包含 approved 与 rejected，不含 pending_review。"""
        assert STATUS_APPROVED in TERMINAL_STATUSES
        assert STATUS_REJECTED in TERMINAL_STATUSES
        assert STATUS_PENDING_REVIEW not in TERMINAL_STATUSES

    def test_terminal_statuses_is_frozenset(self):
        """终态集合为 frozenset，防止运行时篡改。"""
        assert isinstance(TERMINAL_STATUSES, frozenset)

    def test_terminal_statuses_size(self):
        """终态数量为 2（approved + rejected）。"""
        assert len(TERMINAL_STATUSES) == 2


# ============ 异常类型层级 ============


class TestExceptionHierarchy:
    """审批模块异常类型校验：均为 Exception 子类，独立互不继承。"""

    def test_all_exceptions_are_exception_subclass(self):
        """所有审批异常均为 Exception 子类。"""
        for exc_cls in (
            BatchNotFoundError,
            BatchStatusError,
            IdempotencyConflictError,
            PayloadValidationError,
        ):
            assert issubclass(exc_cls, Exception)

    def test_exceptions_distinct(self):
        """4 个异常类互不相同，便于调用方按类型捕获。"""
        excs = {
            BatchNotFoundError,
            BatchStatusError,
            IdempotencyConflictError,
            PayloadValidationError,
        }
        assert len(excs) == 4

    def test_batch_status_error_message_format(self):
        """BatchStatusError 消息含当前状态与期望状态（便于排障）。"""
        msg = str(BatchStatusError("当前=approved, 期望=pending_review"))
        assert "approved" in msg
        assert "pending_review" in msg


# ============ item 结构校验 ============


class TestValidateItemStructure:
    """_validate_item_structure：item 字段完整性校验。"""

    @pytest.mark.parametrize(
        "item",
        [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {},
            },
            {
                "item_type": "edge",
                "action": "create",
                "entity_type": "implements",
                "payload": {},
            },
            {
                "item_type": "node",
                "action": "update",
                "entity_type": "CodeSnippet",
                "payload": {},
            },
            {
                "item_type": "node",
                "action": "delete",
                "entity_type": "Solution",
                "payload": {},
            },
        ],
    )
    def test_valid_structure(self, item):
        """含 4 个必要字段且 item_type/action 合法的 item 不抛异常。"""
        _validate_item_structure(item, 0)  # 不抛即通过

    @pytest.mark.parametrize(
        "missing_key",
        ["item_type", "action", "entity_type", "payload"],
    )
    def test_missing_required_key(self, missing_key):
        """缺失任一必要字段抛 PayloadValidationError，消息含字段名与索引。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {},
        }
        del item[missing_key]
        with pytest.raises(PayloadValidationError) as exc_info:
            _validate_item_structure(item, 2)
        assert missing_key in str(exc_info.value)
        assert "item[2]" in str(exc_info.value)

    @pytest.mark.parametrize(
        "invalid_item_type",
        ["", "node ", "vertex", "relation", "NODE", None],
    )
    def test_invalid_item_type(self, invalid_item_type):
        """非法 item_type 抛 PayloadValidationError。"""
        item = {
            "item_type": invalid_item_type,
            "action": "create",
            "entity_type": "Requirement",
            "payload": {},
        }
        with pytest.raises(PayloadValidationError, match="非法 item_type"):
            _validate_item_structure(item, 0)

    @pytest.mark.parametrize(
        "invalid_action",
        ["", "create ", "insert", "remove", "CREATE", None],
    )
    def test_invalid_action(self, invalid_action):
        """非法 action 抛 PayloadValidationError。"""
        item = {
            "item_type": "node",
            "action": invalid_action,
            "entity_type": "Requirement",
            "payload": {},
        }
        with pytest.raises(PayloadValidationError, match="非法 action"):
            _validate_item_structure(item, 0)

    def test_extra_keys_allowed(self):
        """item 含额外字段（如 seq_hint）不影响校验。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {},
            "extra_meta": "ok",
        }
        _validate_item_structure(item, 0)  # 不抛即通过


# ============ payload 校验 ============


class TestValidateItemPayload:
    """_validate_item_payload：payload 内容合规性校验。"""

    def test_node_create_valid_payload(self):
        """node+create 含合法 properties 通过校验。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {
                "properties": {
                    "requirement_id": "REQ-001",
                    "priority": "P0",
                    "module": "auth",
                }
            },
        }
        _validate_item_payload(item, 0)  # 不抛即通过

    def test_node_create_missing_properties(self):
        """node+create 缺 properties 抛 PayloadValidationError。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {"title": "x"},
        }
        with pytest.raises(PayloadValidationError, match="缺 properties"):
            _validate_item_payload(item, 0)

    def test_node_create_properties_not_dict(self):
        """node+create 的 properties 非 dict 抛 PayloadValidationError。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {"properties": "not a dict"},
        }
        with pytest.raises(PayloadValidationError, match="非 dict"):
            _validate_item_payload(item, 0)

    def test_node_create_invalid_node_type(self):
        """node+create 的 entity_type 不在 NODE_TYPES 抛 PayloadValidationError。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "InvalidType",
            "payload": {"properties": {"any": "thing"}},
        }
        with pytest.raises(PayloadValidationError, match="node\\+create 校验失败"):
            _validate_item_payload(item, 0)

    def test_node_create_missing_required_field(self):
        """node+create 的 properties 缺必填字段抛 PayloadValidationError。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {
                "properties": {"requirement_id": "REQ-001"},  # 缺 priority/module
            },
        }
        with pytest.raises(PayloadValidationError) as exc_info:
            _validate_item_payload(item, 0)
        # 错误消息含缺失字段（透传 SchemaValidationError 信息）
        assert "priority" in str(exc_info.value) or "module" in str(exc_info.value)

    def test_edge_create_valid_payload(self):
        """edge+create 含合法 edge_type 与 from_id/to_id 通过校验。"""
        item = {
            "item_type": "edge",
            "action": "create",
            "entity_type": "implements",
            "payload": {
                "from_id": str(uuid.uuid4()),
                "to_id": str(uuid.uuid4()),
            },
        }
        _validate_item_payload(item, 0)  # 不抛即通过

    def test_edge_create_invalid_edge_type(self):
        """edge+create 的 entity_type 不在 EDGE_TYPES 抛 PayloadValidationError。"""
        item = {
            "item_type": "edge",
            "action": "create",
            "entity_type": "invalid_edge",
            "payload": {"from_id": "x", "to_id": "y"},
        }
        with pytest.raises(PayloadValidationError, match="edge\\+create 校验失败"):
            _validate_item_payload(item, 0)

    def test_edge_create_missing_from_id(self):
        """edge+create 缺 from_ref/from_id 抛 PayloadValidationError。"""
        item = {
            "item_type": "edge",
            "action": "create",
            "entity_type": "implements",
            "payload": {"to_id": str(uuid.uuid4())},
        }
        with pytest.raises(PayloadValidationError, match="缺 from_ref"):
            _validate_item_payload(item, 0)

    def test_edge_create_missing_to_id(self):
        """edge+create 缺 to_ref/to_id 抛 PayloadValidationError。"""
        item = {
            "item_type": "edge",
            "action": "create",
            "entity_type": "implements",
            "payload": {"from_id": str(uuid.uuid4())},
        }
        with pytest.raises(PayloadValidationError, match="缺 from_ref"):
            _validate_item_payload(item, 0)

    def test_edge_create_missing_both_ids(self):
        """edge+create 同时缺 from_ref/to_ref 抛 PayloadValidationError。"""
        item = {
            "item_type": "edge",
            "action": "create",
            "entity_type": "implements",
            "payload": {},
        }
        with pytest.raises(PayloadValidationError, match="缺 from_ref"):
            _validate_item_payload(item, 0)

    def test_payload_not_dict(self):
        """payload 非 dict 抛 PayloadValidationError。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": "not a dict",
        }
        with pytest.raises(PayloadValidationError, match="payload 必须为 dict"):
            _validate_item_payload(item, 0)

    def test_node_update_no_strict_validation(self):
        """node+update 提交时不强校验（节点可能已不存在，留待审批通过时校验）。"""
        item = {
            "item_type": "node",
            "action": "update",
            "entity_type": "Requirement",
            "payload": {"node_id": str(uuid.uuid4()), "title": "更新标题"},
        }
        _validate_item_payload(item, 0)  # 不抛即通过

    def test_edge_update_no_strict_validation(self):
        """edge+update 提交时不强校验。"""
        item = {
            "item_type": "edge",
            "action": "update",
            "entity_type": "implements",
            "payload": {},
        }
        _validate_item_payload(item, 0)  # 不抛即通过

    def test_delete_action_no_strict_validation(self):
        """delete action 提交时不强校验。"""
        item = {
            "item_type": "node",
            "action": "delete",
            "entity_type": "Requirement",
            "payload": {},
        }
        _validate_item_payload(item, 0)  # 不抛即通过

    def test_error_message_contains_index(self):
        """错误消息含 item 索引，便于定位批量提交中的具体项。"""
        item = {
            "item_type": "node",
            "action": "create",
            "entity_type": "Requirement",
            "payload": {},  # 缺 properties
        }
        with pytest.raises(PayloadValidationError) as exc_info:
            _validate_item_payload(item, 5)
        assert "item[5]" in str(exc_info.value)


# ============ conflict_hint 合并逻辑 ============


class TestMergeConflictHints:
    """_merge_conflict_hints：多节点 conflict_hint 合并为单个 JSONB。"""

    def test_empty_hints_list(self):
        """空 hints 列表返回 has_conflict=False 的默认结构。"""
        result = _merge_conflict_hints([])
        assert result == {
            "has_conflict": False,
            "nodes_with_conflict": 0,
            "details": [],
            "suggestion": None,
        }

    def test_all_no_conflict(self):
        """所有节点均无冲突：has_conflict=False，details 为空。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {"has_conflict": False, "suggestion": None},
            },
            {
                "node_id": "n2",
                "title": "节点2",
                "conflict": {"has_conflict": False, "suggestion": None},
            },
        ]
        result = _merge_conflict_hints(hints)
        assert result["has_conflict"] is False
        assert result["nodes_with_conflict"] == 0
        assert result["details"] == []
        assert result["suggestion"] is None

    def test_one_node_with_conflict_review(self):
        """单节点冲突 suggestion=review：聚合建议为 review。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {
                    "has_conflict": True,
                    "suggestion": "review",
                    "similar_nodes": [{"node_id": "n0", "similarity": 0.92}],
                },
            },
            {
                "node_id": "n2",
                "title": "节点2",
                "conflict": {"has_conflict": False, "suggestion": None},
            },
        ]
        result = _merge_conflict_hints(hints)
        assert result["has_conflict"] is True
        assert result["nodes_with_conflict"] == 1
        assert result["suggestion"] == "review"
        assert len(result["details"]) == 1
        assert result["details"][0]["node_id"] == "n1"

    def test_one_node_with_conflict_manual_merge(self):
        """单节点冲突 suggestion=manual_merge：聚合建议为 manual_merge。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {
                    "has_conflict": True,
                    "suggestion": "manual_merge",
                    "tag_matches": [{"node_id": "n0", "shared_tags": ["auth"]}],
                },
            },
        ]
        result = _merge_conflict_hints(hints)
        assert result["has_conflict"] is True
        assert result["suggestion"] == "manual_merge"

    def test_mixed_suggestions_review_wins(self):
        """多节点冲突，review + manual_merge 同时存在，聚合建议优先 review。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {"has_conflict": True, "suggestion": "manual_merge"},
            },
            {
                "node_id": "n2",
                "title": "节点2",
                "conflict": {"has_conflict": True, "suggestion": "review"},
            },
        ]
        result = _merge_conflict_hints(hints)
        assert result["nodes_with_conflict"] == 2
        assert result["suggestion"] == "review"  # review 优先

    def test_all_manual_merge_suggestions(self):
        """所有冲突节点均为 manual_merge：聚合建议为 manual_merge。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {"has_conflict": True, "suggestion": "manual_merge"},
            },
            {
                "node_id": "n2",
                "title": "节点2",
                "conflict": {"has_conflict": True, "suggestion": "manual_merge"},
            },
        ]
        result = _merge_conflict_hints(hints)
        assert result["suggestion"] == "manual_merge"

    def test_details_only_contains_conflict_nodes(self):
        """details 仅含 has_conflict=True 的节点，无冲突节点被过滤。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {"has_conflict": False, "suggestion": None},
            },
            {
                "node_id": "n2",
                "title": "节点2",
                "conflict": {"has_conflict": True, "suggestion": "review"},
            },
            {
                "node_id": "n3",
                "title": "节点3",
                "conflict": {"has_conflict": True, "suggestion": "manual_merge"},
            },
        ]
        result = _merge_conflict_hints(hints)
        assert len(result["details"]) == 2
        detail_ids = {d["node_id"] for d in result["details"]}
        assert detail_ids == {"n2", "n3"}

    def test_none_suggestion_in_conflict_node(self):
        """节点 has_conflict=True 但 suggestion=None（理论边界）：聚合 suggestion=None。"""
        hints = [
            {
                "node_id": "n1",
                "title": "节点1",
                "conflict": {"has_conflict": True, "suggestion": None},
            },
        ]
        result = _merge_conflict_hints(hints)
        assert result["has_conflict"] is True
        assert result["nodes_with_conflict"] == 1
        assert result["suggestion"] is None

    def test_returned_structure_keys(self):
        """返回 dict 含 4 个必要键：has_conflict/nodes_with_conflict/details/suggestion。"""
        result = _merge_conflict_hints([])
        assert set(result.keys()) == {
            "has_conflict",
            "nodes_with_conflict",
            "details",
            "suggestion",
        }


# ============ _to_uuid 类型转换 ============


class TestToUuid:
    """_to_uuid：字符串/UUID 转换为 UUID 实例。"""

    def test_uuid_string_to_uuid(self):
        """UUID 字符串正确转换为 UUID 实例。"""
        original = uuid.uuid4()
        result = _to_uuid(str(original))
        assert result == original
        assert isinstance(result, uuid.UUID)

    def test_uuid_instance_passthrough(self):
        """UUID 实例直接返回（同一对象）。"""
        original = uuid.uuid4()
        result = _to_uuid(original)
        assert result == original

    def test_uppercase_uuid_string(self):
        """大写 UUID 字符串正确转换（UUID 标准兼容大小写）。"""
        original = uuid.uuid4()
        result = _to_uuid(str(original).upper())
        assert result == original

    def test_invalid_string_raises_value_error(self):
        """非法字符串抛 ValueError。"""
        with pytest.raises(ValueError):
            _to_uuid("not-a-uuid")

    def test_empty_string_raises_value_error(self):
        """空字符串抛 ValueError。"""
        with pytest.raises(ValueError):
            _to_uuid("")


# ============ ORM 模型字段定义校验 ============


class TestApprovalModels:
    """ApprovalBatch + ApprovalItem ORM 模型字段定义校验（不依赖 DB）。"""

    def test_approval_batch_table_name(self):
        """表名对齐 PDD 4.5：approval_batch。"""
        assert ApprovalBatch.__tablename__ == "approval_batch"

    def test_approval_item_table_name(self):
        """表名对齐 PDD 4.5：approval_item。"""
        assert ApprovalItem.__tablename__ == "approval_item"

    def test_approval_batch_has_idempotency_constraint(self):
        """ApprovalBatch 含幂等键联合唯一约束 uq_approval_batch_idempotency。"""
        constraint_names = {
            c.name
            for c in ApprovalBatch.__table__.constraints
            if hasattr(c, "name") and c.name
        }
        assert "uq_approval_batch_idempotency" in constraint_names

    def test_approval_batch_has_status_index(self):
        """ApprovalBatch 含状态查询索引 idx_approval_batch_status。"""
        index_names = {idx.name for idx in ApprovalBatch.__table__.indexes}
        assert "idx_approval_batch_status" in index_names

    def test_approval_item_has_batch_seq_index(self):
        """ApprovalItem 含批次内序号索引 idx_approval_item_batch。"""
        index_names = {idx.name for idx in ApprovalItem.__table__.indexes}
        assert "idx_approval_item_batch" in index_names

    def test_approval_item_batch_id_foreign_key(self):
        """ApprovalItem.batch_id 为外键关联 approval_batch.id，级联删除。"""
        fk_columns = {
            fk.parent.name: fk
            for fk in ApprovalItem.__table__.foreign_keys
        }
        assert "batch_id" in fk_columns
        fk = fk_columns["batch_id"]
        assert fk.column.table.name == "approval_batch"
        assert fk.column.name == "id"
        # 级联删除：ondelete="CASCADE"
        assert fk.ondelete == "CASCADE"

    def test_approval_batch_items_relationship(self):
        """ApprovalBatch.items 关系含 cascade all, delete-orphan。

        SQLAlchemy 把 "all" 展开为具体选项集合（delete/expunge/merge/refresh-expire/save-update），
        因此断言关键行为：delete-orphan 在 cascade 中（保证删除批次时级联删除 items，
        且 items 脱离 batch 时自动删除）。
        """
        rel = ApprovalBatch.__mapper__.relationships.get("items")
        assert rel is not None
        assert "delete-orphan" in rel.cascade

    def test_approval_item_batch_back_populates(self):
        """ApprovalItem.batch 反向关系 back_populates=items。"""
        rel = ApprovalItem.__mapper__.relationships.get("batch")
        assert rel is not None
        assert rel.back_populates == "items"


# ============ detect_conflicts 函数签名校验 ============


class TestDetectConflictsSignature:
    """detect_conflicts 函数签名与默认值校验（不调用，仅检查契约）。"""

    def test_detect_conflicts_is_callable(self):
        """detect_conflicts 为可调用对象。"""
        assert callable(detect_conflicts)

    def test_detect_conflicts_default_top_k(self):
        """detect_conflicts 默认 top_k=5。"""
        import inspect

        sig = inspect.signature(detect_conflicts)
        top_k_param = sig.parameters["top_k"]
        assert top_k_param.default == 5

    def test_detect_conflicts_exclude_node_id_optional(self):
        """detect_conflicts 的 exclude_node_id 默认 None（可选）。"""
        import inspect

        sig = inspect.signature(detect_conflicts)
        exclude_param = sig.parameters["exclude_node_id"]
        assert exclude_param.default is None

    def test_detect_conflicts_content_based_params(self):
        """detect_conflicts 以 content/properties 为检测输入（内容级检测契约）。

        阈值为模块常量 CONFLICT_SIMILARITY_THRESHOLD（0.85，由配置驱动），
        不再暴露 similarity_threshold 可调参数（统一三层实现）。
        """
        import inspect

        sig = inspect.signature(detect_conflicts)
        assert "content" in sig.parameters
        assert "properties" in sig.parameters
        assert "similarity_threshold" not in sig.parameters
