"""M6b 单元测试：search_tools 与 query_tools 的辅助函数与输出模型。

纯逻辑测试，无 DB 依赖。覆盖：
- _to_search_item_output / _to_knowledge_node_output 转换函数
- _match_time_range 时间范围过滤
- _to_audit_log_item_output 审计日志转换
- FilterSpec 构造（node_types 白名单校验）
- 输出模型字段校验
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from mem_lake.gateway.tools.query_tools import (
    AuditLogItemOutput,
    GetProjectProfileOutput,
    GetRoleSkillsOutput,
    ProjectProfileOutput,
    QueryAuditLogOutput,
    RelatedNodeOutput,
    RequirementContextOutput,
    _to_audit_log_item_output,
)
from mem_lake.gateway.tools.search_tools import (
    ConflictCheckOutput,
    HybridSearchOutput,
    ImpactScopeOutput,
    ListKnowledgeOutput,
    SearchItemOutput,
    _to_knowledge_node_output,
    _to_search_item_output,
)
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import SearchResult

# ============================================================================
# _to_search_item_output 转换测试
# ============================================================================


class TestToSearchItemOutput:
    """SearchResult → SearchItemOutput 转换测试。"""

    def test_convert_with_score(self):
        """有分数的 SearchResult 转换。"""
        node_id = uuid.uuid4()
        result = SearchResult(
            node_id=node_id,
            title="测试需求",
            content="测试内容",
            node_type="Requirement",
            score=0.95,
            source="vector",
            properties={"key": "value"},
            tags=["tag1"],
        )
        output = _to_search_item_output(result)
        assert output.node_id == node_id
        assert output.title == "测试需求"
        assert output.content == "测试内容"
        assert output.node_type == "Requirement"
        assert output.score == 0.95
        assert output.source == "vector"
        assert output.properties == {"key": "value"}
        assert output.tags == ["tag1"]

    def test_convert_without_score(self):
        """score=None 的 SearchResult（图遍历结果）转换。"""
        result = SearchResult(
            node_id=uuid.uuid4(),
            title="图遍历结果",
            content="内容",
            node_type="CodeSnippet",
            score=None,
            source="graph",
            properties={},
            tags=[],
        )
        output = _to_search_item_output(result)
        assert output.score is None
        assert output.source == "graph"

    def test_convert_with_empty_properties_and_tags(self):
        """空 properties 和 tags 转换。"""
        result = SearchResult(
            node_id=uuid.uuid4(),
            title="标题",
            content="内容",
            node_type="Solution",
            score=0.5,
            source="fused",
            properties={},
            tags=[],
        )
        output = _to_search_item_output(result)
        assert output.properties == {}
        assert output.tags == []


# ============================================================================
# _to_knowledge_node_output 转换测试
# ============================================================================


class TestToKnowledgeNodeOutput:
    """KnowledgeNode ORM → KnowledgeNodeOutput 转换测试。"""

    def test_convert_with_created_at(self):
        """有 created_at 的节点转换。"""
        node = MagicMock()
        node.id = uuid.uuid4()
        node.type = "Requirement"
        node.title = "需求标题"
        node.status = "approved"
        node.version = 3
        node.created_at = datetime(2026, 8, 2, 12, 0, 0)
        node.created_by = "ak_admin"
        node.tags = ["auth", "login"]

        output = _to_knowledge_node_output(node)
        assert output.node_id == node.id
        assert output.type == "Requirement"
        assert output.title == "需求标题"
        assert output.status == "approved"
        assert output.version == 3
        assert output.created_at == "2026-08-02T12:00:00"
        assert output.created_by == "ak_admin"
        assert output.tags == ["auth", "login"]

    def test_convert_with_none_created_at(self):
        """created_at 为 None 的节点转换。"""
        node = MagicMock()
        node.id = uuid.uuid4()
        node.type = "Pitfall"
        node.title = "踩坑"
        node.status = "approved"
        node.version = 1
        node.created_at = None
        node.created_by = "ak_dev"
        node.tags = None  # tags 为 None

        output = _to_knowledge_node_output(node)
        assert output.created_at is None
        assert output.tags == []  # None 转 []


# ============================================================================
# _match_time_range 时间范围过滤测试
# ============================================================================


class TestValidateAndThreshold:
    """审计 §2.6/§2.10/P2#9：空 query 校验 + 冲突阈值统一读配置。"""

    def test_query_validate_rejects_empty(self):
        """空 query 拒绝。"""
        from mem_lake.approval.service import PayloadValidationError
        from mem_lake.gateway.tools.search_tools import _validate_query

        with pytest.raises(PayloadValidationError):
            _validate_query("")
        with pytest.raises(PayloadValidationError):
            _validate_query("   ")

    def test_query_validate_accepts_non_empty(self):
        """非空 query 通过。"""
        from mem_lake.gateway.tools.search_tools import _validate_query

        _validate_query("登录")


# ============================================================================
# 兜底逻辑：search_similar_requirements 省略 scope 时按 Key claims 回退
# ============================================================================


class TestResolveSearchScopeFallback:
    """_resolve_search_scope_fallback 三分支 + admin 不接收。"""

    def setup_method(self):
        from mem_lake.gateway.tools.search_tools import _resolve_search_scope_fallback
        self.resolve = _resolve_search_scope_fallback

    def test_explicit_ids_pass_through(self):
        p, s = uuid.uuid4(), uuid.uuid4()
        out = self.resolve("dev", ["sys-a"], project_id=p, system_id=s)
        assert out == (p, s)

    def test_single_system_fallback(self):
        sid = uuid.uuid4()
        pid, sid_out = self.resolve("dev", [str(sid)], project_id=None, system_id=None)
        assert pid is None
        assert sid_out == sid

    def test_no_system_bound_error(self):
        with pytest.raises(ValueError, match="未绑定"):
            self.resolve("dev", [], project_id=None, system_id=None)

    def test_multiple_systems_error_lists_candidates(self):
        s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
        with pytest.raises(ValueError, match="多个 system"):
            self.resolve("dev", [s1, s2], project_id=None, system_id=None)

    def test_admin_no_fallback(self):
        with pytest.raises(ValueError, match="至少提供一个"):
            self.resolve("admin", [], project_id=None, system_id=None)


# ============================================================================
# 引擎明细开关：默认省 token 不回传 vector/fulltext，调试时显式打开
# ============================================================================


class TestEngineDetailsToggle:
    """_run_hybrid_search 的 include_engine_details 三态。"""

    def _make_result(self):
        from mem_lake.search.fusion import SearchResult

        nid = uuid.uuid4()
        return {
            "fused": [
                SearchResult(node_id=nid, title="需求A", content="内容A", node_type="Requirement",
                             score=0.9, source="fused", properties={}, tags=[]),
            ],
            "vector": [
                SearchResult(node_id=nid, title="需求A", content="内容A", node_type="Requirement",
                             score=0.9, source="vector", properties={}, tags=[]),
            ],
            "fulltext": [
                SearchResult(node_id=nid, title="需求A", content="内容A", node_type="Requirement",
                             score=0.01, source="fulltext", properties={}, tags=[]),
            ],
        }

    async def test_engine_details_omitted_by_default(self, monkeypatch):
        """默认不回传引擎明细：vector/fulltext 为 None（序列化后不出现该键），fused 正常返回。"""
        from types import SimpleNamespace

        from mem_lake.gateway.tools import search_tools

        monkeypatch.setattr(
            search_tools, "get_lifespan_context",
            lambda: SimpleNamespace(embedding_client=None, graph_store=None),
        )
        monkeypatch.setattr(
            search_tools, "hybrid_search",
            lambda **kw: _async_return(self._make_result()),
        )

        out = await search_tools._run_hybrid_search(
            project_id=uuid.uuid4(), query="登录", node_types=("Requirement",),
            top_n=10, tags=None,
        )
        assert len(out.fused) == 1
        assert out.vector is None
        assert out.fulltext is None
        # 序列化后不得出现误导性的空数组键（ISSUE-04：空列表像「候选池为空」）
        dumped = out.model_dump(exclude_none=True)
        assert "vector" not in dumped
        assert "fulltext" not in dumped

    async def test_engine_details_included_when_flag(self, monkeypatch):
        """include_engine_details=True 时回传引擎明细（调试/评估用）。"""
        from types import SimpleNamespace

        from mem_lake.gateway.tools import search_tools

        monkeypatch.setattr(
            search_tools, "get_lifespan_context",
            lambda: SimpleNamespace(embedding_client=None, graph_store=None),
        )
        monkeypatch.setattr(
            search_tools, "hybrid_search",
            lambda **kw: _async_return(self._make_result()),
        )

        out = await search_tools._run_hybrid_search(
            project_id=uuid.uuid4(), query="登录", node_types=("Requirement",),
            top_n=10, tags=None, include_engine_details=True,
        )
        assert len(out.fused) == 1
        assert len(out.vector) == 1
        assert len(out.fulltext) == 1

    async def test_candidates_total_counts_pre_filter(self, monkeypatch):
        """candidates_total=阈值过滤前候选数，returned/total=过滤后条数（ISSUE-03）。"""
        from types import SimpleNamespace

        from mem_lake.gateway.tools import search_tools
        from mem_lake.search.fusion import SearchResult

        fused_items = [
            SearchResult(node_id=uuid.uuid4(), title="低分向量命中1", content="x",
                         node_type="Requirement", score=0.31, source="fused", properties={}, tags=[]),
            SearchResult(node_id=uuid.uuid4(), title="低分向量命中2", content="x",
                         node_type="Requirement", score=0.25, source="fused", properties={}, tags=[]),
            SearchResult(node_id=uuid.uuid4(), title="纯全文命中", content="x",
                         node_type="Requirement", score=0.01, source="fused", properties={}, tags=[]),
        ]
        # vector 命中前 2 个（低分）——min_score 过滤依据 vector_score_map
        vector_items = fused_items[:2]

        async def fake_hybrid(**kw):
            return {
                "fused": fused_items,
                "vector": vector_items,
                "fulltext": [],
                "graph": [],
            }

        monkeypatch.setattr(
            search_tools, "get_lifespan_context",
            lambda: SimpleNamespace(embedding_client=None, graph_store=None),
        )
        monkeypatch.setattr(search_tools, "hybrid_search", fake_hybrid)

        out = await search_tools._run_hybrid_search(
            project_id=uuid.uuid4(), query="q", node_types=("Requirement",),
            top_n=10, tags=None, min_score=0.9,
        )
        assert out.candidates_total == 3  # 过滤前
        assert len(out.fused) == 1  # 仅纯全文命中保留
        assert out.total == out.returned == 1


async def _async_return(value):
    return value


# ============================================================================
# _to_audit_log_item_output 转换测试
# ============================================================================


class TestToAuditLogItemOutput:
    """AuditLog ORM → AuditLogItemOutput 转换测试。"""

    def test_convert_full(self):
        """完整字段转换。"""
        log = MagicMock()
        log.id = uuid.uuid4()
        log.actor = "ak_admin"
        log.action = "write"
        log.target_type = "node"
        log.target_id = uuid.uuid4()
        log.detail = {"node_type": "Requirement", "title": "需求"}
        log.created_at = datetime(2026, 8, 2, 12, 0, 0)

        output = _to_audit_log_item_output(log)
        assert output.log_id == log.id
        assert output.actor == "ak_admin"
        assert output.action == "write"
        assert output.target_type == "node"
        assert output.target_id == log.target_id
        assert output.detail == {"node_type": "Requirement", "title": "需求"}
        assert output.created_at == "2026-08-02T12:00:00"

    def test_convert_with_none_detail(self):
        """detail 为 None 的日志转换。"""
        log = MagicMock()
        log.id = uuid.uuid4()
        log.actor = "ak_pm"
        log.action = "update"
        log.target_type = "edge"
        log.target_id = None
        log.detail = None
        log.created_at = None

        output = _to_audit_log_item_output(log)
        assert output.detail == {}  # None 转 {}
        assert output.target_id is None
        assert output.created_at is None


# ============================================================================
# FilterSpec 构造测试
# ============================================================================


class TestFilterSpecForTools:
    """FilterSpec 构造测试（工具层使用的过滤条件）。"""

    def test_valid_node_types(self):
        """合法节点类型构造成功。"""
        spec = FilterSpec(
            project_id=uuid.uuid4(),
            node_types=("Requirement",),
        )
        assert spec.node_types == ("Requirement",)
        assert spec.status == "approved"  # 默认值
        assert spec.exclude_deleted is True  # 默认值

    def test_invalid_node_types_raises(self):
        """非法节点类型抛 ValueError。"""
        with pytest.raises(ValueError, match="非法节点类型"):
            FilterSpec(node_types=("InvalidType",))

    def test_tags_filter(self):
        """tags 过滤。"""
        spec = FilterSpec(
            project_id=uuid.uuid4(),
            node_types=("CodeSnippet",),
            tags=("auth", "login"),
        )
        assert spec.tags == ("auth", "login")


# ============================================================================
# 输出模型字段校验测试
# ============================================================================


class TestOutputModels:
    """输出模型字段校验测试。"""

    def test_search_item_output(self):
        """SearchItemOutput 字段校验。"""
        item = SearchItemOutput(
            node_id=uuid.uuid4(),
            title="标题",
            content="内容",
            node_type="Requirement",
            score=0.8,
            source="fused",
            properties={"k": "v"},
            tags=["tag"],
        )
        assert item.source == "fused"
        assert item.score == 0.8

    def test_hybrid_search_output_default_omits_engine_lists(self):
        """默认不回传 vector/fulltext（None，序列化省略）；returned/candidates_total 就位。"""
        output = HybridSearchOutput(
            query="测试",
            fused=[],
            total=0,
        )
        assert output.vector is None
        assert output.fulltext is None
        assert output.returned == 0
        assert output.candidates_total == 0
        dumped = output.model_dump(exclude_none=True)
        assert "vector" not in dumped and "fulltext" not in dumped

    def test_hybrid_search_output_total_equals_returned(self):
        """total 为历史字段，语义等同 returned（避免「命中总数」误读，ISSUE-03）。"""
        from mem_lake.gateway.tools.search_tools import SearchItemOutput

        items = [
            SearchItemOutput(
                node_id=uuid.uuid4(), title="t", content="c",
                node_type="Requirement", score=0.9, source="fused",
            )
        ]
        output = HybridSearchOutput(query="q", fused=items, total=1, candidates_total=7)
        assert output.total == output.returned == len(output.fused)
        assert output.candidates_total == 7

    def test_conflict_check_output_no_conflict(self):
        """ConflictCheckOutput 无冲突场景。"""
        output = ConflictCheckOutput(
            requirement_id=uuid.uuid4(),
            has_conflict=False,
            conflicts=[],
            threshold=0.85,
            suggestion=None,
        )
        assert output.has_conflict is False
        assert output.suggestion is None

    def test_impact_scope_output_empty(self):
        """ImpactScopeOutput 空影响范围。"""
        output = ImpactScopeOutput(
            requirement_id=uuid.uuid4(),
            requirement=None,
        )
        assert output.requirement is None
        assert output.codes == []
        assert output.dependencies == []

    def test_list_knowledge_output(self):
        """ListKnowledgeOutput 字段校验。"""
        output = ListKnowledgeOutput(
            project_id=uuid.uuid4(),
            nodes=[],
            total=0,
            limit=100,
            offset=0,
        )
        assert output.total == 0

    def test_get_role_skills_output(self):
        """GetRoleSkillsOutput 字段校验。"""
        output = GetRoleSkillsOutput(
            role="pm",
            skills_markdown="# PM Skills",
            version="1.0.0",
            installation_guide="## Skills 文件放置指南",
        )
        assert output.role == "pm"
        assert output.version == "1.0.0"
        assert "放置指南" in output.installation_guide

    def test_get_project_profile_output_none(self):
        """GetProjectProfileOutput 无画像场景。"""
        output = GetProjectProfileOutput(
            project_id=uuid.uuid4(),
            profile=None,
        )
        assert output.profile is None

    def test_project_profile_output(self):
        """ProjectProfileOutput 字段校验。"""
        output = ProjectProfileOutput(
            node_id=uuid.uuid4(),
            title="项目名",
            content="描述",
            properties={"tech_stack": ["Python"]},
            tags=["tag"],
            version=1,
            created_at="2026-08-02T12:00:00",
            created_by="ak_admin",
        )
        assert output.properties == {"tech_stack": ["Python"]}

    def test_related_node_output(self):
        """RelatedNodeOutput 字段校验。"""
        output = RelatedNodeOutput(
            node_id=uuid.uuid4(),
            title="关联节点",
            content="内容",
            node_type="CodeSnippet",
            edge_type=["implements"],
            depth=1,
        )
        assert output.edge_type == ["implements"]
        assert output.depth == 1

    def test_requirement_context_output_not_found(self):
        """RequirementContextOutput 需求不存在场景。"""
        output = RequirementContextOutput(
            requirement_id=uuid.uuid4(),
            requirement=None,
            related_nodes=[],
            total=0,
        )
        assert output.requirement is None
        assert output.total == 0

    def test_audit_log_item_output(self):
        """AuditLogItemOutput 字段校验。"""
        output = AuditLogItemOutput(
            log_id=uuid.uuid4(),
            actor="ak_admin",
            action="write",
            target_type="node",
            target_id=uuid.uuid4(),
            detail={"key": "value"},
            created_at="2026-08-02T12:00:00",
        )
        assert output.action == "write"

    def test_query_audit_log_output(self):
        """QueryAuditLogOutput 字段校验。"""
        output = QueryAuditLogOutput(
            logs=[],
            total=0,
            limit=100,
            offset=0,
        )
        assert output.logs == []
        assert output.limit == 100
