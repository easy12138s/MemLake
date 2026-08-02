"""M4 单元测试：RRF 融合算法 + FilterSpec 编译。

按实际调用场景验证（纯逻辑无 DB 依赖）：
1. RRF 算法：基本融合、k 参数、top_n 截断、空列表、单列表、分数累加、rank 从 1 开始
2. FilterSpec：默认值、SQLAlchemy/Cypher 编译、非法节点类型校验

测试隔离：纯函数测试，无 DB 与外部依赖，无需 fixture。
"""

import uuid
from datetime import datetime

import pytest

from mem_lake.search.filters import FilterSpec, compile_cypher, compile_sqlalchemy
from mem_lake.search.fusion import SearchResult, rrf_fuse


# ============ 辅助函数 ============


def _make_result(node_id: uuid.UUID, title: str = "T", score: float = 0.5) -> SearchResult:
    """构造测试用 SearchResult。"""
    return SearchResult(
        node_id=node_id,
        title=title,
        content="content",
        node_type="Requirement",
        score=score,
        source="test",
        properties={},
        tags=[],
    )


# ============ RRF 算法测试 ============


class TestRRFBasic:
    """RRF 基本融合场景。"""

    def test_rrf_basic_two_lists(self):
        """两个列表融合：两列表都出现且排名高的节点得分最高。

        场景：列表1 = [A, B, C]，列表2 = [B, A, D]
        B 在两列表都出现（列表1 rank=2，列表2 rank=1），分数累加最高
        """
        a, b, c, d = (uuid.uuid4() for _ in range(4))
        list1 = [_make_result(a), _make_result(b), _make_result(c)]
        list2 = [_make_result(b), _make_result(a), _make_result(d)]

        fused = rrf_fuse([list1, list2], k=60, top_n=10)

        assert len(fused) == 4
        # B 分数 = 1/(60+2) + 1/(60+1) ≈ 0.0161 + 0.0164 = 0.0325
        # A 分数 = 1/(60+1) + 1/(60+2) ≈ 0.0164 + 0.0161 = 0.0325（同 B）
        # 但 B 在列表2 rank=1（更高），实际 A 与 B 分数相同（交换律）
        # 验证 B 与 A 是前两名（顺序由分数决定，相同分数由 dict 迭代顺序决定）
        top_two_ids = {fused[0].node_id, fused[1].node_id}
        assert b in top_two_ids
        assert a in top_two_ids
        # C 与 D 各自单列表出现，分数较低
        assert fused[2].score < fused[1].score

    def test_rrf_score_is_rrf_formula(self):
        """验证 RRF 分数公式：score = Σ 1/(k + rank)，rank 从 1 开始。"""
        a = uuid.uuid4()
        list1 = [_make_result(a)]  # rank=1
        list2 = [_make_result(a)]  # rank=1

        fused = rrf_fuse([list1, list2], k=60, top_n=10)

        # A 在两列表都 rank=1，分数 = 1/(60+1) + 1/(60+1) = 2/61
        expected_score = 2.0 / 61
        assert fused[0].node_id == a
        assert abs(fused[0].score - expected_score) < 1e-9

    def test_rrf_rank_starts_from_1(self):
        """rank 从 1 开始（rank=1 的分数 1/(k+1)），非从 0 开始。"""
        a = uuid.uuid4()
        list1 = [_make_result(a)]  # rank=1

        fused = rrf_fuse([list1], k=60, top_n=10)

        # 若 rank 从 1 开始，分数 = 1/(60+1) = 1/61 ≈ 0.0164
        # 若 rank 从 0 开始，分数 = 1/(60+0) = 1/60 ≈ 0.0167
        assert abs(fused[0].score - 1.0 / 61) < 1e-9

    def test_rrf_same_node_accumulates(self):
        """同一节点在多列表出现分数累加（RRF 核心优势）。"""
        a = uuid.uuid4()
        # A 在 3 个列表中都出现且都 rank=1
        lists = [[_make_result(a)] for _ in range(3)]

        fused = rrf_fuse(lists, k=60, top_n=10)

        # 分数 = 3 * 1/(60+1) = 3/61
        expected_score = 3.0 / 61
        assert abs(fused[0].score - expected_score) < 1e-9


class TestRRFParameters:
    """RRF 参数行为测试。"""

    def test_rrf_default_k_is_60(self):
        """默认 k=60（PDD 与 Cormack 2009 论文默认值）。"""
        a, b = uuid.uuid4(), uuid.uuid4()
        list1 = [_make_result(a), _make_result(b)]

        # 不传 k，使用默认值 60
        fused_default = rrf_fuse([list1], top_n=10)
        fused_explicit = rrf_fuse([list1], k=60, top_n=10)

        assert fused_default[0].score == fused_explicit[0].score

    def test_rrf_k_parameter_affects_score(self):
        """k 越小，高排名（rank=1）的优势越明显。"""
        a, b = uuid.uuid4(), uuid.uuid4()
        list1 = [_make_result(a), _make_result(b)]  # A rank=1, B rank=2

        fused_k1 = rrf_fuse([list1], k=1, top_n=10)
        fused_k60 = rrf_fuse([list1], k=60, top_n=10)

        # k=1 时 A/B 分数比 = (1/2) / (1/3) = 1.5
        # k=60 时 A/B 分数比 = (1/61) / (1/62) ≈ 1.016
        ratio_k1 = fused_k1[0].score / fused_k1[1].score
        ratio_k60 = fused_k60[0].score / fused_k60[1].score
        assert ratio_k1 > ratio_k60  # k=1 时排名差异更显著

    def test_rrf_top_n_truncation(self):
        """top_n 截断：融合后只返回前 N 个。"""
        ids = [uuid.uuid4() for _ in range(5)]
        list1 = [_make_result(i) for i in ids]

        fused = rrf_fuse([list1], k=60, top_n=2)

        assert len(fused) == 2
        # 截断的是分数最高的前 2 个（rank=1 和 rank=2）
        assert fused[0].node_id == ids[0]
        assert fused[1].node_id == ids[1]


class TestRRFEdgeCases:
    """RRF 边界场景。"""

    def test_rrf_empty_lists(self):
        """空列表输入返回空。"""
        assert rrf_fuse([], k=60, top_n=10) == []
        assert rrf_fuse([[]], k=60, top_n=10) == []
        assert rrf_fuse([[], []], k=60, top_n=10) == []

    def test_rrf_single_list(self):
        """单列表输入等价于按原顺序取前 top_n（rank 单一来源）。"""
        ids = [uuid.uuid4() for _ in range(3)]
        list1 = [_make_result(i) for i in ids]

        fused = rrf_fuse([list1], k=60, top_n=10)

        # 顺序应与原列表一致（rank=1,2,3 分数递减）
        assert [r.node_id for r in fused] == ids

    def test_rrf_preserves_metadata(self):
        """融合后 SearchResult 保留首次出现的 title/content/node_type。"""
        a = uuid.uuid4()
        list1 = [
            SearchResult(
                node_id=a,
                title="原始标题",
                content="原始内容",
                node_type="Requirement",
                score=0.9,
                source="vector",
                properties={"key": "value"},
                tags=["tag1"],
            )
        ]

        fused = rrf_fuse([list1], k=60, top_n=10)

        assert fused[0].title == "原始标题"
        assert fused[0].content == "原始内容"
        assert fused[0].node_type == "Requirement"
        assert fused[0].properties == {"key": "value"}
        assert fused[0].tags == ["tag1"]
        # score 与 source 被替换为 RRF 分数与 "fused"
        assert fused[0].source == "fused"
        assert fused[0].score != 0.9  # 替换为 RRF 分数

    def test_rrf_score_ordering(self):
        """分数严格降序排序。"""
        ids = [uuid.uuid4() for _ in range(4)]
        list1 = [_make_result(ids[i]) for i in range(4)]

        fused = rrf_fuse([list1], k=60, top_n=10)

        for i in range(len(fused) - 1):
            assert fused[i].score >= fused[i + 1].score

    def test_rrf_duplicate_in_same_list(self):
        """同一列表内重复节点：分数累加（不常见但需明确行为）。"""
        a = uuid.uuid4()
        list1 = [_make_result(a), _make_result(a)]  # rank=1 与 rank=2

        fused = rrf_fuse([list1], k=60, top_n=10)

        # 同一节点在元数据中只保留首次出现（rank=1 的）
        assert len(fused) == 1
        # 分数 = 1/(60+1) + 1/(60+2)
        expected_score = 1.0 / 61 + 1.0 / 62
        assert abs(fused[0].score - expected_score) < 1e-9


# ============ FilterSpec 测试 ============


class TestFilterSpecDefaults:
    """FilterSpec 默认值与校验。"""

    def test_default_values(self):
        """默认值：status='approved'，exclude_deleted=True。"""
        spec = FilterSpec()

        assert spec.project_id is None
        assert spec.node_types is None
        assert spec.status == "approved"  # PDD 3.4：未审批不参与检索
        assert spec.exclude_deleted is True
        assert spec.tags is None
        assert spec.created_after is None
        assert spec.created_before is None

    def test_invalid_node_types_raises(self):
        """非法节点类型立即抛 ValueError。"""
        with pytest.raises(ValueError, match="非法节点类型"):
            FilterSpec(node_types=("InvalidType",))

    def test_valid_node_types_accepted(self):
        """合法节点类型正常构造。"""
        spec = FilterSpec(node_types=("Requirement", "CodeSnippet"))
        assert spec.node_types == ("Requirement", "CodeSnippet")

    def test_frozen_dataclass(self):
        """FilterSpec 是 frozen dataclass，不可变。"""
        spec = FilterSpec()
        with pytest.raises(Exception):  # FrozenInstanceError
            spec.status = "draft"  # type: ignore[misc]


class TestCompileSqlalchemy:
    """compile_sqlalchemy 编译测试。"""

    def test_none_spec_returns_empty(self):
        """spec=None 返回空列表（不过滤）。"""
        assert compile_sqlalchemy(None) == []

    def test_project_id_clause(self):
        """project_id 编译为 = 子句。"""
        pid = uuid.uuid4()
        spec = FilterSpec(project_id=pid)

        clauses = compile_sqlalchemy(spec)

        assert len(clauses) >= 1
        # 验证 SQL 包含 project_id（具体 SQL 形式由 SQLAlchemy 决定）
        compiled = str(clauses[0].compile(compile_kwargs={"literal_binds": True}))
        assert "project_id" in compiled

    def test_node_types_clause(self):
        """node_types 编译为 IN 子句。"""
        spec = FilterSpec(node_types=("Requirement", "CodeSnippet"))

        clauses = compile_sqlalchemy(spec)

        compiled = str(clauses[0].compile(compile_kwargs={"literal_binds": True}))
        assert "IN" in compiled.upper() or "in" in compiled

    def test_status_clause_default_approved(self):
        """默认 status='approved' 编译为 = 子句。"""
        spec = FilterSpec()

        clauses = compile_sqlalchemy(spec)

        # 找到 status 子句
        status_clauses = [
            c for c in clauses if "status" in str(c.compile(compile_kwargs={"literal_binds": True}))
        ]
        assert len(status_clauses) == 1
        compiled = str(status_clauses[0].compile(compile_kwargs={"literal_binds": True}))
        assert "approved" in compiled

    def test_status_empty_string_no_clause(self):
        """status='' 时不生成 status 子句（显式不过滤状态）。"""
        spec = FilterSpec(status="")

        clauses = compile_sqlalchemy(spec)

        status_clauses = [
            c for c in clauses if "status" in str(c.compile(compile_kwargs={"literal_binds": True}))
        ]
        assert len(status_clauses) == 0

    def test_exclude_deleted_clause(self):
        """exclude_deleted=True 编译为 is_deleted = False 子句。"""
        spec = FilterSpec()

        clauses = compile_sqlalchemy(spec)

        deleted_clauses = [
            c
            for c in clauses
            if "is_deleted" in str(c.compile(compile_kwargs={"literal_binds": True}))
        ]
        assert len(deleted_clauses) == 1

    def test_tags_clause(self):
        """tags 编译为 JSONB @> (contains) 子句。

        JSONB 类型无 literal renderer，不能用 literal_binds=True，
        改用普通 compile 检查 SQL 是否含 tags 字段名。
        """
        spec = FilterSpec(tags=("auth", "P0"))

        clauses = compile_sqlalchemy(spec)

        # JSONB contains 子句编译后含 "tags" 字段名
        tags_clauses = [c for c in clauses if "tags" in str(c.compile())]
        assert len(tags_clauses) == 1

    def test_created_after_before_clauses(self):
        """created_after/created_before 编译为 >= / <= 子句。"""
        after = datetime(2026, 1, 1)
        before = datetime(2026, 12, 31)
        spec = FilterSpec(created_after=after, created_before=before)

        clauses = compile_sqlalchemy(spec)

        time_clauses = [
            c
            for c in clauses
            if "created_at" in str(c.compile(compile_kwargs={"literal_binds": True}))
        ]
        assert len(time_clauses) == 2

    def test_all_conditions_combined(self):
        """所有过滤条件同时设置时全部编译。"""
        spec = FilterSpec(
            project_id=uuid.uuid4(),
            node_types=("Requirement",),
            tags=("auth",),
            created_after=datetime(2026, 1, 1),
            created_before=datetime(2026, 12, 31),
        )

        clauses = compile_sqlalchemy(spec)

        # project_id + node_types + status(默认) + exclude_deleted(默认) + tags + created_after + created_before = 7
        assert len(clauses) == 7


class TestCompileCypher:
    """compile_cypher 编译测试。"""

    def test_none_spec_returns_empty(self):
        """spec=None 返回空字符串（图层不过滤）。"""
        assert compile_cypher(None) == ""

    def test_no_project_id_returns_empty(self):
        """spec 有但 project_id=None 返回空字符串。"""
        spec = FilterSpec(node_types=("Requirement",))
        assert compile_cypher(spec) == ""

    def test_project_id_clause(self):
        """project_id 编译为 n.project_id = $project_id。"""
        pid = uuid.uuid4()
        spec = FilterSpec(project_id=pid)

        clause = compile_cypher(spec)

        assert "n.project_id = $project_id" in clause

    def test_custom_node_var(self):
        """自定义 node_var 参数。"""
        pid = uuid.uuid4()
        spec = FilterSpec(project_id=pid)

        clause = compile_cypher(spec, node_var="m")

        assert "m.project_id = $project_id" in clause

    def test_other_filters_not_in_cypher(self):
        """status/is_deleted/tags/时间 不在 Cypher 中（图层不存这些字段）。"""
        spec = FilterSpec(
            project_id=uuid.uuid4(),
            node_types=("Requirement",),
            tags=("auth",),
            created_after=datetime(2026, 1, 1),
        )

        clause = compile_cypher(spec)

        # 只应包含 project_id，不含其他字段
        assert "status" not in clause
        assert "is_deleted" not in clause
        assert "tags" not in clause
        assert "created_at" not in clause
