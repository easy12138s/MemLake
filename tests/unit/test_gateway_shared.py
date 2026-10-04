"""gateway/tools/_shared.py 单元测试：ToolAnnotations + 输出模型 + 异常转换 + items 构造。

纯单测，无 DB 依赖。覆盖：
- ToolAnnotations 常量的 wire format（camelCase alias）
- build_node_item/build_edge_item 构造逻辑与必填字段校验
- to_tool_error 异常转换映射
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from mem_lake.approval.models import ApprovalBatch, ApprovalItem
from mem_lake.approval.service import (
    BatchNotFoundError,
    BatchStatusError,
    PayloadValidationError,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    WRITE_TOOL_ANNOTATIONS,
    ApprovalResultOutput,
    WriteToolOutput,
    build_edge_item,
    build_node_item,
    build_update_node_item,
    to_tool_error,
)
from mem_lake.knowledge.repository import NodeNotFoundError
from mem_lake.knowledge.schema import SchemaValidationError


class TestToolAnnotations:
    """ToolAnnotations 常量测试。"""

    def test_read_annotations_wire_format(self):
        """READ_TOOL_ANNOTATIONS 的 wire format 为 camelCase。"""
        dumped = READ_TOOL_ANNOTATIONS.model_dump(by_alias=True)
        assert dumped["readOnlyHint"] is True
        assert dumped["destructiveHint"] is False
        assert dumped["idempotentHint"] is True
        assert dumped["openWorldHint"] is False

    def test_write_annotations_wire_format(self):
        """WRITE_TOOL_ANNOTATIONS 的 wire format 为 camelCase。"""
        dumped = WRITE_TOOL_ANNOTATIONS.model_dump(by_alias=True)
        assert dumped["readOnlyHint"] is False
        assert dumped["destructiveHint"] is False
        assert dumped["idempotentHint"] is True
        assert dumped["openWorldHint"] is False

    def test_read_only_is_read_only(self):
        """读工具 read_only_hint=True。"""
        assert READ_TOOL_ANNOTATIONS.read_only_hint is True

    def test_write_is_not_read_only(self):
        """写工具 read_only_hint=False。"""
        assert WRITE_TOOL_ANNOTATIONS.read_only_hint is False


class TestWriteToolOutput:
    """WriteToolOutput 测试。"""

    def test_from_batch_construction(self):
        """from_batch 从 ApprovalBatch 构造输出。"""
        batch_id = uuid.uuid4()
        project_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        batch = ApprovalBatch(
            id=batch_id,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_test",
            submitter_role="pm",
            summary="1 个节点",
            status="pending_review",
            submitted_at=now,
        )
        batch.items = [
            ApprovalItem(seq=1, item_type="node", action="create", entity_type="Requirement")
        ]

        output = WriteToolOutput.from_batch(batch)
        assert output.batch_id == batch_id
        assert output.status == "pending_review"
        assert output.submitted_at == now
        assert output.item_count == 1

    def test_from_batch_empty_items(self):
        """from_batch 处理 items 为空列表（falsy 走 else 0 分支）。"""
        batch_id = uuid.uuid4()
        project_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        batch = ApprovalBatch(
            id=batch_id,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_test",
            submitter_role="pm",
            summary="0 个节点",
            status="pending_review",
            submitted_at=now,
        )
        batch.items = []  # 空列表是 falsy，触发 `if batch.items else 0` 分支

        output = WriteToolOutput.from_batch(batch)
        assert output.item_count == 0


class TestApprovalResultOutput:
    """ApprovalResultOutput 测试。"""

    def test_construction_with_conflict_hint(self):
        """构造含 conflict_hint 的输出。"""
        output = ApprovalResultOutput(
            batch_id=uuid.uuid4(),
            status="approved",
            reviewed_at=datetime.now(timezone.utc),
            conflict_hint={"has_conflict": True, "nodes_with_conflict": 1},
        )
        assert output.status == "approved"
        assert output.conflict_hint["has_conflict"] is True

    def test_construction_without_conflict_hint(self):
        """构造不含 conflict_hint 的输出（reject 场景）。"""
        output = ApprovalResultOutput(
            batch_id=uuid.uuid4(),
            status="rejected",
            reviewed_at=datetime.now(timezone.utc),
            conflict_hint=None,
        )
        assert output.status == "rejected"
        assert output.conflict_hint is None


class TestBuildNodeItem:
    """build_node_item 测试。"""

    def test_valid_node_item(self):
        """构造合法 node item（Requirement 需 system_id）。"""
        project_id = uuid.uuid4()
        system_id = uuid.uuid4()
        item = build_node_item(
            ref="requirement",
            node_type="Requirement",
            title="测试需求",
            content="测试内容",
            properties={"priority": "P0", "module": "auth"},
            tags=["auth"],
            project_id=project_id,
            system_id=system_id,
            created_by="ak_test",
        )
        assert item["item_type"] == "node"
        assert item["action"] == "create"
        assert item["entity_type"] == "Requirement"
        assert item["payload"]["ref"] == "requirement"
        assert item["payload"]["title"] == "测试需求"
        assert item["payload"]["properties"]["priority"] == "P0"
        assert item["payload"]["tags"] == ["auth"]
        assert item["payload"]["project_id"] == str(project_id)
        assert item["payload"]["system_id"] == str(system_id)
        assert item["payload"]["created_by"] == "ak_test"

    def test_default_tags_empty(self):
        """tags 默认为空列表。"""
        item = build_node_item(
            ref="req",
            node_type="Requirement",
            title="t",
            content="c",
            properties={"k": "v"},
            project_id=uuid.uuid4(),
            system_id=uuid.uuid4(),
            created_by="ak",
        )
        assert item["payload"]["tags"] == []

    def test_default_source_empty(self):
        """source 默认为空 dict。"""
        item = build_node_item(
            ref="req",
            node_type="Requirement",
            title="t",
            content="c",
            properties={"k": "v"},
            project_id=uuid.uuid4(),
            system_id=uuid.uuid4(),
            created_by="ak",
        )
        assert item["payload"]["source"] == {}

    def test_missing_properties_raises(self):
        """缺 properties 抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="缺少 properties 字段"):
            build_node_item(
                ref="req",
                node_type="Requirement",
                title="t",
                content="c",
                properties=None,  # type: ignore
                project_id=uuid.uuid4(),
                system_id=uuid.uuid4(),
                created_by="ak",
            )

    def test_empty_properties_raises(self):
        """空 properties dict 抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="缺少 properties 字段"):
            build_node_item(
                ref="req",
                node_type="Requirement",
                title="t",
                content="c",
                properties={},  # type: ignore
                project_id=uuid.uuid4(),
                system_id=uuid.uuid4(),
                created_by="ak",
            )

    def test_requirement_missing_system_id_raises(self):
        """Requirement 缺 system_id 抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="必须归属 system"):
            build_node_item(
                ref="req",
                node_type="Requirement",
                title="t",
                content="c",
                properties={"k": "v"},
                project_id=uuid.uuid4(),
                system_id=None,  # type: ignore
                created_by="ak",
            )

    def test_asset_missing_project_id_raises(self):
        """非 Requirement 资产缺 project_id 抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="必须归属 project"):
            build_node_item(
                ref="req",
                node_type="CodeSnippet",
                title="t",
                content="c",
                properties={"k": "v"},
                project_id=None,  # type: ignore
                created_by="ak",
            )

    def test_missing_created_by_raises(self):
        """缺 created_by 抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="缺少 created_by"):
            build_node_item(
                ref="req",
                node_type="Requirement",
                title="t",
                content="c",
                properties={"k": "v"},
                project_id=uuid.uuid4(),
                system_id=uuid.uuid4(),
                created_by="",
            )


class TestBuildUpdateNodeItem:
    """build_update_node_item 测试（审批流更新节点内容）。"""

    def test_valid_update_item_title_only(self):
        """仅更新 title：action=update，payload 只含所传字段。"""
        node_id = uuid.uuid4()
        item = build_update_node_item(
            node_id=node_id,
            node_type="CodeSnippet",
            title="新标题",
        )
        assert item["item_type"] == "node"
        assert item["action"] == "update"
        assert item["entity_type"] == "CodeSnippet"
        payload = item["payload"]
        assert payload["node_id"] == str(node_id)
        assert payload["title"] == "新标题"
        # 未传字段不应出现在 payload 中（保持与 update_node 的 None=不更新语义）
        assert "content" not in payload
        assert "properties" not in payload
        assert "tags" not in payload

    def test_partial_fields_preserved(self):
        """同时传 content 与 tags，其余字段不出现。"""
        item = build_update_node_item(
            node_id=uuid.uuid4(),
            node_type="Pitfall",
            content="新正文",
            tags=["bug"],
        )
        payload = item["payload"]
        assert payload["content"] == "新正文"
        assert payload["tags"] == ["bug"]
        assert "title" not in payload

    def test_properties_replacement(self):
        """properties：payload 直接载入（整体替换语义）。"""
        props = {"service": "auth", "lang": "python"}
        item = build_update_node_item(
            node_id=uuid.uuid4(),
            node_type="CodeSnippet",
            properties=props,
        )
        assert item["payload"]["properties"] == props

    def test_all_fields_none_raises(self):
        """所有字段均为 None：抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="至少提供一个要变更的字段"):
            build_update_node_item(node_id=uuid.uuid4(), node_type="Requirement")


class TestBuildEdgeItem:
    """build_edge_item 测试。"""

    def test_valid_edge_item(self):
        """构造合法 edge item。"""
        item = build_edge_item(
            from_ref="requirement",
            to_ref="REQ-001",
            edge_type="supersedes",
            properties={"reason": "版本更新"},
        )
        assert item["item_type"] == "edge"
        assert item["action"] == "create"
        assert item["entity_type"] == "supersedes"
        assert item["payload"]["from_ref"] == "requirement"
        assert item["payload"]["to_ref"] == "REQ-001"
        assert item["payload"]["properties"]["reason"] == "版本更新"

    def test_default_properties_empty(self):
        """properties 默认为空 dict。"""
        item = build_edge_item(
            from_ref="a", to_ref="b", edge_type="implements"
        )
        assert item["payload"]["properties"] == {}

    def test_missing_from_ref_raises(self):
        """缺 from_ref 抛 PayloadValidationError（实现为合并校验消息）。"""
        with pytest.raises(PayloadValidationError, match="边缺少 from_ref 或 to_ref"):
            build_edge_item(
                from_ref="", to_ref="b", edge_type="implements"
            )

    def test_missing_to_ref_raises(self):
        """缺 to_ref 抛 PayloadValidationError（实现为合并校验消息）。"""
        with pytest.raises(PayloadValidationError, match="边缺少 from_ref 或 to_ref"):
            build_edge_item(
                from_ref="a", to_ref="", edge_type="implements"
            )

    def test_both_missing_raises(self):
        """同时缺 from_ref 和 to_ref 抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="边缺少 from_ref 或 to_ref"):
            build_edge_item(
                from_ref="", to_ref="", edge_type="implements"
            )

    def test_uuid_string_preserved(self):
        """UUID 字符串作为 ref 直接保留。"""
        uid = str(uuid.uuid4())
        item = build_edge_item(
            from_ref=uid, to_ref=uid, edge_type="relates_to"
        )
        assert item["payload"]["from_ref"] == uid
        assert item["payload"]["to_ref"] == uid


class TestToToolError:
    """to_tool_error 异常转换测试。"""

    def test_payload_validation_error(self):
        """PayloadValidationError 转换为 ToolError。"""
        err = to_tool_error(PayloadValidationError("校验失败"))
        assert isinstance(err, ToolError)
        assert "参数校验失败" in str(err)

    def test_schema_validation_error(self):
        """SchemaValidationError 转换为 ToolError。"""
        err = to_tool_error(SchemaValidationError("schema 错误"))
        assert isinstance(err, ToolError)
        assert "Schema 校验失败" in str(err)

    def test_batch_not_found_error(self):
        """BatchNotFoundError 转换为 ToolError。"""
        err = to_tool_error(BatchNotFoundError("batch 不存在"))
        assert isinstance(err, ToolError)
        assert "批次不存在" in str(err)

    def test_batch_status_error(self):
        """BatchStatusError 转换为 ToolError。"""
        err = to_tool_error(BatchStatusError("状态错误"))
        assert isinstance(err, ToolError)
        assert "批次状态错误" in str(err)

    def test_node_not_found_error(self):
        """NodeNotFoundError 转换为 ToolError。"""
        err = to_tool_error(NodeNotFoundError("节点不存在"))
        assert isinstance(err, ToolError)
        assert "节点不存在" in str(err)

    def test_tool_error_passthrough(self):
        """ToolError 直接返回不包装。"""
        original = ToolError("原始错误")
        err = to_tool_error(original)
        assert err is original

    def test_unknown_exception_generic_message(self):
        """未识别异常返回通用错误信息（不暴露内部细节）。"""
        err = to_tool_error(RuntimeError("内部错误详情"))
        assert isinstance(err, ToolError)
        assert "工具调用失败" in str(err)
        # 不应暴露内部错误详情
        assert "内部错误详情" not in str(err)


class TestWriteToolOutputReceipt:
    """批次五（报告 P1-2）：宽松模式回执含 created 节点清单与建边计数。"""

    def _lax_batch(self):
        """auto_approved 批次 mock：2 node（target_id 回填）+ 1 edge。"""
        from datetime import datetime, timezone

        node1 = MagicMock()
        node1.item_type, node1.action, node1.entity_type = "node", "create", "Pitfall"
        node1.target_id = uuid.uuid4()
        node1.payload = {"ref": "Pit", "title": "退费判据陷阱"}

        node2 = MagicMock()
        node2.item_type, node2.action, node2.entity_type = "node", "create", "Solution"
        node2.target_id = uuid.uuid4()
        node2.payload = {"ref": "Sol", "title": "双锚点方案"}

        edge1 = MagicMock()
        edge1.item_type, edge1.action, edge1.entity_type = "edge", "create", "described_by"
        edge1.target_id, edge1.payload = None, {}

        batch = MagicMock()
        batch.id = uuid.uuid4()
        batch.status = "approved"
        batch.submitted_at = datetime.now(timezone.utc)
        batch.items = [node1, node2, edge1]
        return batch, node1, node2

    def test_auto_approved_receipt_contains_created_and_edges(self):
        """auto_approved：created 含 ref/node_id/type/title，edges_created=边项数。"""
        batch, n1, n2 = self._lax_batch()
        out = WriteToolOutput.from_batch(batch, decision="auto_approved")
        assert out.edges_created == 1
        assert len(out.created) == 2
        by_ref = {c["ref"]: c for c in out.created}
        assert by_ref["Pit"]["node_id"] == str(n1.target_id)
        assert by_ref["Pit"]["node_type"] == "Pitfall"
        assert by_ref["Sol"]["title"] == "双锚点方案"

    def test_strict_and_pending_receipt_omit_created(self):
        """严格模式（decision=None）与 needs_human_review 不返回 created（节点未入库）。"""
        batch, _, _ = self._lax_batch()
        strict = WriteToolOutput.from_batch(batch, decision=None)
        assert strict.created is None and strict.edges_created is None
        pending = WriteToolOutput.from_batch(batch, decision="needs_human_review")
        assert pending.created is None and pending.edges_created is None
