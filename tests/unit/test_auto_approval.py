"""自动审批功能单元测试。

覆盖：
- _match_key_attrs 纯函数（L2 关键属性比对逻辑）
- detect_conflicts 三层检测（L1 隐含于 FilterSpec，L2 关键属性，L3 内容语义相似度）
- auto_process_batch 决策逻辑（无冲突自动通过 / 有冲突升级人工 / 状态校验）
- RBAC：review_auto_process 仅 admin 可用

所有 DB / 向量检索依赖均 mock，纯逻辑验证。
DB 集成场景由 tests/integration/test_approval_flow.py 覆盖。
"""

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mem_lake.approval.conflict import (
    CONFLICT_SIMILARITY_THRESHOLD,
    KEY_IDENTITY_FIELDS,
    _match_key_attrs,
    detect_conflicts,
)
from mem_lake.approval.service import (
    BatchStatusError,
    auto_process_batch,
)
from mem_lake.auth.rbac import ADMIN_ONLY_TOOLS, has_tool_access


# ============================================================================
# 辅助：模拟 SearchResult（避免依赖真实 DB）
# ============================================================================


@dataclass
class FakeSearchResult:
    """模拟 VectorSearcher.search 返回的 SearchResult。"""

    node_id: uuid.UUID
    title: str
    score: float
    content: str = ""
    node_type: str = "Requirement"
    properties: dict = None
    tags: list = None
    source: str = "vector"

    def __post_init__(self):
        if self.properties is None:
            self.properties = {}
        if self.tags is None:
            self.tags = []


def _make_node_item(node_type: str, properties: dict, title: str = "测试节点", content: str = "测试内容") -> MagicMock:
    """构造 node+create 的 ApprovalItem mock。"""
    item = MagicMock()
    item.item_type = "node"
    item.action = "create"
    item.entity_type = node_type
    item.payload = {
        "project_id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "properties": properties,
        "tags": [],
    }
    return item


# ============================================================================
# L2 关键属性比对：_match_key_attrs 纯函数
# ============================================================================


class TestMatchKeyAttrs:
    """_match_key_attrs 纯函数测试：L2 关键属性比对逻辑。"""

    def test_requirement_same_id_matches(self):
        """Requirement 相同 requirement_id → 匹配（即使 priority 不同）。"""
        new = {"requirement_id": "REQ-001", "priority": "P0"}
        existing = {"requirement_id": "REQ-001", "priority": "P1"}
        result = _match_key_attrs(new, existing, "Requirement")
        assert result == {"requirement_id": "REQ-001"}

    def test_requirement_different_id_no_match(self):
        """Requirement 不同 requirement_id → None（不同实体）。"""
        new = {"requirement_id": "REQ-001"}
        existing = {"requirement_id": "REQ-002"}
        assert _match_key_attrs(new, existing, "Requirement") is None

    def test_code_snippet_same_name_and_path(self):
        """CodeSnippet 相同 name + file_path → 匹配。"""
        new = {"name": "LoginService", "file_path": "src/auth.py"}
        existing = {"name": "LoginService", "file_path": "src/auth.py"}
        result = _match_key_attrs(new, existing, "CodeSnippet")
        assert result == {"name": "LoginService", "file_path": "src/auth.py"}

    def test_code_snippet_different_path_no_match(self):
        """CodeSnippet 相同 name 不同 file_path → None（不同代码片段）。"""
        new = {"name": "LoginService", "file_path": "src/auth.py"}
        existing = {"name": "LoginService", "file_path": "src/oauth.py"}
        assert _match_key_attrs(new, existing, "CodeSnippet") is None

    def test_code_snippet_different_name_no_match(self):
        """CodeSnippet 不同 name → None。"""
        new = {"name": "LoginService", "file_path": "src/auth.py"}
        existing = {"name": "LogoutService", "file_path": "src/auth.py"}
        assert _match_key_attrs(new, existing, "CodeSnippet") is None

    def test_solution_same_approach(self):
        """Solution 相同 approach → 匹配。"""
        new = {"approach": "缓存策略", "version": "v1"}
        existing = {"approach": "缓存策略", "version": "v2"}
        result = _match_key_attrs(new, existing, "Solution")
        assert result == {"approach": "缓存策略"}

    def test_pitfall_same_symptom(self):
        """Pitfall 相同 symptom → 匹配。"""
        new = {"symptom": "OOM", "solution": "加内存"}
        existing = {"symptom": "OOM", "solution": "优化查询"}
        result = _match_key_attrs(new, existing, "Pitfall")
        assert result == {"symptom": "OOM"}

    def test_unknown_type_returns_empty_dict(self):
        """未定义关键标识字段的类型返回空 dict（跳过 L2，直接进入 L3 判定）。"""
        result = _match_key_attrs({"foo": "bar"}, {"baz": "qux"}, "UnknownType")
        assert result == {}

    def test_missing_key_attr_in_existing_treated_as_mismatch(self):
        """已有节点缺失关键字段 → 视为不匹配（None != 某值）。"""
        new = {"requirement_id": "REQ-001"}
        existing = {}  # 缺失 requirement_id
        assert _match_key_attrs(new, existing, "Requirement") is None

    def test_both_missing_key_attr_treated_as_match(self):
        """双方都缺失关键字段 → 视为匹配（None == None）。"""
        new = {}
        existing = {}
        result = _match_key_attrs(new, existing, "Requirement")
        assert result == {"requirement_id": None}


# ============================================================================
# detect_conflicts 三层检测（mock 向量检索 + 节点查询）
# ============================================================================


class TestDetectConflictsV2:
    """detect_conflicts 三层冲突检测逻辑测试。"""

    @pytest.mark.asyncio
    async def test_no_candidates_no_conflict(self):
        """向量检索无候选 → 无冲突。"""
        session = AsyncMock()
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(return_value=[])

        result = await detect_conflicts(
            session,
            vector_searcher=vector_searcher,
            project_id=uuid.uuid4(),
            node_type="Requirement",
            title="测试需求",
            content="测试内容",
            properties={"requirement_id": "REQ-001"},
            tags=[],
        )
        assert result["has_conflict"] is False
        assert result["candidates_examined"] == 0
        assert result["conflicting_nodes"] == []
        assert result["suggestion"] is None

    @pytest.mark.asyncio
    async def test_l3_below_threshold_no_conflict(self):
        """L3 过滤：相似度 < 0.92 → 无冲突。"""
        session = AsyncMock()
        candidate = FakeSearchResult(
            node_id=uuid.uuid4(), title="已有需求", score=0.80
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(return_value=[candidate])

        result = await detect_conflicts(
            session,
            vector_searcher=vector_searcher,
            project_id=uuid.uuid4(),
            node_type="Requirement",
            title="测试需求",
            content="测试内容",
            properties={"requirement_id": "REQ-001"},
            tags=[],
        )
        assert result["has_conflict"] is False
        assert result["candidates_examined"] == 1

    @pytest.mark.asyncio
    async def test_l3_threshold_boundary_no_conflict(self):
        """L3 阈值边界：score 恰好 0.91（< 0.92）→ 无冲突。"""
        session = AsyncMock()
        candidate = FakeSearchResult(
            node_id=uuid.uuid4(), title="已有需求", score=0.91
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(return_value=[candidate])

        result = await detect_conflicts(
            session,
            vector_searcher=vector_searcher,
            project_id=uuid.uuid4(),
            node_type="Requirement",
            title="测试",
            content="内容",
            properties={"requirement_id": "REQ-001"},
            tags=[],
        )
        assert result["has_conflict"] is False

    @pytest.mark.asyncio
    async def test_l2_different_key_attr_no_conflict(self):
        """L2 过滤：相似度 ≥ 0.92 但关键属性不同 → 无冲突。"""
        session = AsyncMock()
        candidate = FakeSearchResult(
            node_id=uuid.uuid4(), title="已有需求", score=0.95
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(return_value=[candidate])

        existing_node = MagicMock()
        existing_node.properties = {"requirement_id": "REQ-002"}

        with patch(
            "mem_lake.approval.conflict.get_node_for_conflict",
            new=AsyncMock(return_value=existing_node),
        ):
            result = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="测试需求",
                content="测试内容",
                properties={"requirement_id": "REQ-001"},
                tags=[],
            )
        assert result["has_conflict"] is False
        assert result["candidates_examined"] == 1

    @pytest.mark.asyncio
    async def test_three_layers_pass_conflict_detected(self):
        """三层全通过 → 冲突（相似度 ≥ 0.92 + 关键属性相同）。"""
        session = AsyncMock()
        candidate = FakeSearchResult(
            node_id=uuid.uuid4(), title="已有需求", score=0.95
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(return_value=[candidate])

        existing_node = MagicMock()
        existing_node.properties = {"requirement_id": "REQ-001"}

        with patch(
            "mem_lake.approval.conflict.get_node_for_conflict",
            new=AsyncMock(return_value=existing_node),
        ):
            result = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="测试需求",
                content="测试内容",
                properties={"requirement_id": "REQ-001"},
                tags=[],
            )
        assert result["has_conflict"] is True
        assert len(result["conflicting_nodes"]) == 1
        conflict = result["conflicting_nodes"][0]
        assert conflict["similarity"] == 0.95
        assert conflict["matched_key_attrs"] == {"requirement_id": "REQ-001"}
        assert conflict["conflict_type"] == "duplicate"
        assert result["suggestion"] == "review"

    @pytest.mark.asyncio
    async def test_existing_node_not_found_skipped(self):
        """候选节点查询返回 None（已归档/删除）→ 跳过，无冲突。"""
        session = AsyncMock()
        candidate = FakeSearchResult(
            node_id=uuid.uuid4(), title="已有需求", score=0.95
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(return_value=[candidate])

        with patch(
            "mem_lake.approval.conflict.get_node_for_conflict",
            new=AsyncMock(return_value=None),
        ):
            result = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="测试",
                content="内容",
                properties={"requirement_id": "REQ-001"},
                tags=[],
            )
        assert result["has_conflict"] is False
        assert result["candidates_examined"] == 1

    @pytest.mark.asyncio
    async def test_multiple_candidates_partial_conflict(self):
        """多候选：部分冲突 → 只返回冲突的。"""
        session = AsyncMock()
        candidate_a = FakeSearchResult(
            node_id=uuid.uuid4(), title="需求A", score=0.95
        )
        candidate_b = FakeSearchResult(
            node_id=uuid.uuid4(), title="需求B", score=0.93
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(
            return_value=[candidate_a, candidate_b]
        )

        # 第一个候选关键属性相同（冲突），第二个不同（不冲突）
        node_a = MagicMock()
        node_a.properties = {"requirement_id": "REQ-001"}
        node_b = MagicMock()
        node_b.properties = {"requirement_id": "REQ-999"}

        async def fake_get_node(session, node_id):
            if node_id == candidate_a.node_id:
                return node_a
            return node_b

        with patch(
            "mem_lake.approval.conflict.get_node_for_conflict",
            new=fake_get_node,
        ):
            result = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="测试",
                content="内容",
                properties={"requirement_id": "REQ-001"},
                tags=[],
            )
        assert result["has_conflict"] is True
        assert len(result["conflicting_nodes"]) == 1
        assert result["candidates_examined"] == 2

    @pytest.mark.asyncio
    async def test_exclude_node_id_skips_self(self):
        """exclude_node_id 排除自身节点（写入后检测场景，避免自检）。"""
        self_id = uuid.uuid4()
        session = AsyncMock()
        candidate_self = FakeSearchResult(
            node_id=self_id, title="自身", score=0.99
        )
        candidate_other = FakeSearchResult(
            node_id=uuid.uuid4(), title="已有需求", score=0.95
        )
        vector_searcher = MagicMock()
        vector_searcher.search = AsyncMock(
            return_value=[candidate_self, candidate_other]
        )

        existing_node = MagicMock()
        existing_node.properties = {"requirement_id": "REQ-001"}

        with patch(
            "mem_lake.approval.conflict.get_node_for_conflict",
            new=AsyncMock(return_value=existing_node),
        ):
            result = await detect_conflicts(
                session,
                vector_searcher=vector_searcher,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="测试需求",
                content="测试内容",
                properties={"requirement_id": "REQ-001"},
                tags=[],
                exclude_node_id=self_id,
            )
        # 自身被排除（candidates_examined=1），仅另一个候选构成冲突
        assert result["candidates_examined"] == 1
        assert result["has_conflict"] is True
        assert (
            result["conflicting_nodes"][0]["existing_node_id"]
            == str(candidate_other.node_id)
        )


# ============================================================================
# auto_process_batch 决策逻辑（mock detect_conflicts + review_approve）
# ============================================================================


class TestAutoProcessBatch:
    """auto_process_batch 自动审批决策逻辑测试。"""

    @pytest.mark.asyncio
    async def test_no_conflict_auto_approved(self):
        """无冲突 → 调用 review_approve，返回 auto_approved。"""
        session = AsyncMock()

        batch = MagicMock()
        batch.id = uuid.uuid4()
        batch.status = "pending_review"
        batch.items = []  # 无 node+create 项

        approved_batch = MagicMock()
        approved_batch.id = batch.id
        approved_batch.status = "approved"
        approved_batch.summary = "0 个节点 + 0 个关系"
        approved_batch.batch_type = "publish_requirement"
        approved_batch.submitted_by = "pm-key"
        approved_batch.items = []

        with patch(
            "mem_lake.approval.service.get_batch_detail",
            new=AsyncMock(return_value=batch),
        ), patch(
            "mem_lake.approval.service.review_approve",
            new=AsyncMock(return_value=approved_batch),
        ) as mock_approve:
            result = await auto_process_batch(
                session,
                batch_id=batch.id,
                reviewed_by="admin-key",
                graph_store=MagicMock(),
                embedding_client=MagicMock(),
                vector_searcher=MagicMock(),
            )

        assert result["decision"] == "auto_approved"
        assert result["conflict_hint"]["has_conflict"] is False
        assert result["conflict_hint"]["checked_nodes"] == 0
        assert result["batch"].status == "approved"
        mock_approve.assert_called_once()

    @pytest.mark.asyncio
    async def test_has_conflict_needs_human_review(self):
        """有冲突 → 不调用 review_approve，返回 needs_human_review。"""
        session = AsyncMock()

        item = _make_node_item(
            "Requirement", {"requirement_id": "REQ-001"}
        )
        batch = MagicMock()
        batch.id = uuid.uuid4()
        batch.status = "pending_review"
        batch.items = [item]

        conflict_result = {
            "has_conflict": True,
            "conflicting_nodes": [
                {
                    "existing_node_id": str(uuid.uuid4()),
                    "similarity": 0.95,
                    "matched_key_attrs": {"requirement_id": "REQ-001"},
                }
            ],
            "candidates_examined": 3,
            "suggestion": "review",
        }

        with patch(
            "mem_lake.approval.service.get_batch_detail",
            new=AsyncMock(return_value=batch),
        ), patch(
            "mem_lake.approval.service.detect_conflicts",
            new=AsyncMock(return_value=conflict_result),
        ), patch(
            "mem_lake.approval.service.review_approve",
            new=AsyncMock(),
        ) as mock_approve:
            result = await auto_process_batch(
                session,
                batch_id=batch.id,
                reviewed_by="admin-key",
                graph_store=MagicMock(),
                embedding_client=MagicMock(),
                vector_searcher=MagicMock(),
            )

        assert result["decision"] == "needs_human_review"
        assert result["conflict_hint"]["has_conflict"] is True
        assert result["conflict_hint"]["checked_nodes"] == 1
        assert len(result["conflict_hint"]["conflicting_nodes"]) == 1
        assert result["conflict_hint"]["suggestion"] == "review"
        mock_approve.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_pending_status_raises(self):
        """非 pending_review 状态 → 抛 BatchStatusError。"""
        session = AsyncMock()
        batch = MagicMock()
        batch.id = uuid.uuid4()
        batch.status = "approved"
        batch.items = []

        with patch(
            "mem_lake.approval.service.get_batch_detail",
            new=AsyncMock(return_value=batch),
        ):
            with pytest.raises(BatchStatusError):
                await auto_process_batch(
                    session,
                    batch_id=batch.id,
                    reviewed_by="admin-key",
                    graph_store=MagicMock(),
                    embedding_client=MagicMock(),
                    vector_searcher=MagicMock(),
                )

    @pytest.mark.asyncio
    async def test_multiple_nodes_all_no_conflict_auto_approved(self):
        """多节点全部无冲突 → 自动通过。"""
        session = AsyncMock()

        item1 = _make_node_item("Requirement", {"requirement_id": "REQ-001"})
        item2 = _make_node_item("CodeSnippet", {"name": "Svc", "file_path": "a.py"})
        batch = MagicMock()
        batch.id = uuid.uuid4()
        batch.status = "pending_review"
        batch.items = [item1, item2]

        no_conflict_result = {
            "has_conflict": False,
            "conflicting_nodes": [],
            "candidates_examined": 2,
            "suggestion": None,
        }

        approved_batch = MagicMock()
        approved_batch.id = batch.id
        approved_batch.status = "approved"
        approved_batch.items = [item1, item2]

        with patch(
            "mem_lake.approval.service.get_batch_detail",
            new=AsyncMock(return_value=batch),
        ), patch(
            "mem_lake.approval.service.detect_conflicts",
            new=AsyncMock(return_value=no_conflict_result),
        ), patch(
            "mem_lake.approval.service.review_approve",
            new=AsyncMock(return_value=approved_batch),
        ):
            result = await auto_process_batch(
                session,
                batch_id=batch.id,
                reviewed_by="admin-key",
                graph_store=MagicMock(),
                embedding_client=MagicMock(),
                vector_searcher=MagicMock(),
            )

        assert result["decision"] == "auto_approved"
        assert result["conflict_hint"]["checked_nodes"] == 2
        assert result["conflict_hint"]["candidates_examined"] == 4  # 2+2

    @pytest.mark.asyncio
    async def test_edge_items_skipped_in_conflict_check(self):
        """edge 项不参与冲突检测（只检测 node+create）。"""
        session = AsyncMock()

        edge_item = MagicMock()
        edge_item.item_type = "edge"
        edge_item.action = "create"
        edge_item.entity_type = "implements"
        edge_item.payload = {"from_ref": "a", "to_ref": "b"}

        batch = MagicMock()
        batch.id = uuid.uuid4()
        batch.status = "pending_review"
        batch.items = [edge_item]

        approved_batch = MagicMock()
        approved_batch.id = batch.id
        approved_batch.status = "approved"
        approved_batch.items = [edge_item]

        with patch(
            "mem_lake.approval.service.get_batch_detail",
            new=AsyncMock(return_value=batch),
        ), patch(
            "mem_lake.approval.service.review_approve",
            new=AsyncMock(return_value=approved_batch),
        ), patch(
            "mem_lake.approval.service.detect_conflicts",
            new=AsyncMock(),
        ) as mock_detect:
            result = await auto_process_batch(
                session,
                batch_id=batch.id,
                reviewed_by="admin-key",
                graph_store=MagicMock(),
                embedding_client=MagicMock(),
                vector_searcher=MagicMock(),
            )

        assert result["decision"] == "auto_approved"
        assert result["conflict_hint"]["checked_nodes"] == 0
        mock_detect.assert_not_called()


# ============================================================================
# RBAC：review_auto_process 权限校验
# ============================================================================


class TestReviewAutoProcessRBAC:
    """review_auto_process 工具的 RBAC 权限校验。"""

    def test_review_auto_process_in_admin_only_tools(self):
        """review_auto_process 在 ADMIN_ONLY_TOOLS 中。"""
        assert "review_auto_process" in ADMIN_ONLY_TOOLS

    def test_admin_can_access(self):
        """admin 角色可访问 review_auto_process。"""
        assert has_tool_access("admin", "review_auto_process") is True

    def test_pm_cannot_access(self):
        """pm 角色不可访问 review_auto_process。"""
        assert has_tool_access("pm", "review_auto_process") is False

    def test_dev_cannot_access(self):
        """dev 角色不可访问 review_auto_process。"""
        assert has_tool_access("dev", "review_auto_process") is False


# ============================================================================
# 常量校验
# ============================================================================


class TestConflictConstants:
    """冲突检测常量校验。"""

    def test_conflict_threshold_is_092(self):
        """内容级冲突阈值固定为 0.92。"""
        assert CONFLICT_SIMILARITY_THRESHOLD == 0.92

    def test_key_identity_fields_covers_all_node_types(self):
        """KEY_IDENTITY_FIELDS 覆盖全部 7 种节点类型。"""
        expected_types = {
            "ProjectProfile",
            "Requirement",
            "CodeSnippet",
            "Solution",
            "DesignIntent",
            "Decision",
            "Pitfall",
        }
        assert set(KEY_IDENTITY_FIELDS.keys()) == expected_types

    def test_requirement_key_field_is_requirement_id(self):
        """Requirement 的关键标识字段为 requirement_id。"""
        assert KEY_IDENTITY_FIELDS["Requirement"] == ["requirement_id"]

    def test_code_snippet_key_fields_are_name_and_file_path(self):
        """CodeSnippet 的关键标识字段为 name + file_path。"""
        assert KEY_IDENTITY_FIELDS["CodeSnippet"] == ["name", "file_path"]
