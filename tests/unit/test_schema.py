"""节点属性 Schema 校验单元测试。

覆盖：
- 各节点类型的合法 properties 通过校验
- 非法节点类型抛 SchemaValidationError
- 缺失必填字段抛 SchemaValidationError
- 封闭契约：白名单外未知字段拒绝、白名单内可选字段通过
- 边类型校验（合法/非法）
- properties 非 dict 类型
- 空字典、空字符串边界
- 校验不修改输入
"""

import pytest

from mem_lake.knowledge.schema import (
    ALLOWED_FIELDS,
    EDGE_TYPES,
    NODE_TYPES,
    SchemaValidationError,
    validate_edge_type,
    validate_node,
)


# ============ 节点类型校验 ============

class TestValidateNode:
    """validate_node 校验测试。"""

    @pytest.mark.parametrize(
        "node_type,properties",
        [
            (
                "ProjectProfile",
                {
                    "name": "Mem Lake",
                    "description": "记忆基础设施",
                    "tech_stack": ["Python", "PostgreSQL"],
                    "architecture": "MCP 网关 + 单实例 PostgreSQL",
                },
            ),
            (
                "Requirement",
                {"priority": "P0", "module": "auth"},
            ),
            (
                "CodeSnippet",
                {
                    "name": "LoginService",
                    "type": "class",
                    "responsibility": "登录",
                    "file_path": "src/auth/login.py",
                },
            ),
            (
                "Solution",
                {"approach": "JWT", "version": "1.0"},
            ),
            (
                "DesignIntent",
                {"rationale": "无状态易扩展", "trade_offs": "牺牲会话存储换扩展性"},
            ),
            (
                "Decision",
                {"decision_id": "DEC-001", "decision": "采用 JWT"},
            ),
            (
                "Pitfall",
                {
                    "symptom": "token 续期冲突",
                    "root_cause": "时钟不同步",
                    "solution": "Redis 锁",
                    "severity": "P1",
                },
            ),
        ],
    )
    def test_valid_node_properties(self, node_type, properties):
        """各类节点合法 properties 不抛异常。"""
        validate_node(node_type, properties)  # 不抛即通过

    def test_invalid_node_type(self):
        """非法节点类型抛 SchemaValidationError。"""
        with pytest.raises(SchemaValidationError, match="非法节点类型"):
            validate_node("InvalidType", {"any": "thing"})

    def test_invalid_node_type_empty_string(self):
        """空字符串节点类型非法。"""
        with pytest.raises(SchemaValidationError, match="非法节点类型"):
            validate_node("", {"name": "x"})

    @pytest.mark.parametrize(
        "node_type,missing_field",
        [
            ("ProjectProfile", "name"),
            ("ProjectProfile", "description"),
            ("ProjectProfile", "tech_stack"),
            ("ProjectProfile", "architecture"),
            ("Requirement", "priority"),
            ("Requirement", "module"),
            ("CodeSnippet", "name"),
            ("CodeSnippet", "type"),
            ("CodeSnippet", "responsibility"),
            ("CodeSnippet", "file_path"),
            ("Solution", "approach"),
            ("Solution", "version"),
            ("DesignIntent", "rationale"),
            ("DesignIntent", "trade_offs"),
            ("Decision", "decision_id"),
            ("Decision", "decision"),
            ("Pitfall", "symptom"),
            ("Pitfall", "root_cause"),
            ("Pitfall", "solution"),
        ],
    )
    def test_missing_required_field(self, node_type, missing_field):
        """缺失任一必填字段抛 SchemaValidationError，错误信息含字段名。"""
        # 构造完整合法 properties 后删除目标字段
        full_props = self._full_props(node_type)
        del full_props[missing_field]
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_node(node_type, full_props)
        assert missing_field in str(exc_info.value)

    @pytest.mark.parametrize("severity", ["P10", "p0", "CRITICAL", "0", 5, ""])
    def test_pitfall_invalid_severity(self, severity):
        """Pitfall severity 非法枚举抛 SchemaValidationError。"""
        with pytest.raises(SchemaValidationError, match="severity"):
            validate_node(
                "Pitfall",
                {
                    "symptom": "s",
                    "root_cause": "rc",
                    "solution": "sol",
                    "severity": severity,
                },
            )

    def test_pitfall_valid_severity(self):
        """合法 severity 不抛异常。"""
        validate_node(
            "Pitfall",
            {
                "symptom": "s",
                "root_cause": "rc",
                "solution": "sol",
                "severity": "P2",
            },
        )

    def test_properties_not_dict(self):
        """properties 非 dict 抛 SchemaValidationError。"""
        with pytest.raises(SchemaValidationError, match="properties 必须为 dict"):
            validate_node("ProjectProfile", "not a dict")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "invalid_value",
        [None, [], 123, "", 0],
    )
    def test_properties_various_invalid_types(self, invalid_value):
        """各种非法类型 properties 抛异常。"""
        with pytest.raises(SchemaValidationError):
            validate_node("ProjectProfile", invalid_value)

    def test_empty_dict_missing_all_required(self):
        """空 dict 缺所有必填字段。"""
        with pytest.raises(SchemaValidationError, match="缺失必填字段"):
            validate_node("Requirement", {})

    def test_input_not_mutated(self):
        """校验不修改输入 properties。"""
        props = {
            "name": "Mem Lake",
            "description": "x",
            "tech_stack": ["Python"],
            "architecture": "单体",
        }
        original = dict(props)
        validate_node("ProjectProfile", props)
        assert props == original

    def test_all_node_types_in_NODE_TYPES(self):
        """NODE_TYPES 包含 PDD 定义的 7 种节点类型。"""
        expected = {
            "ProjectProfile",
            "Requirement",
            "CodeSnippet",
            "Solution",
            "DesignIntent",
            "Decision",
            "Pitfall",
        }
        assert expected == set(NODE_TYPES)

    @staticmethod
    def _full_props(node_type: str) -> dict:
        """返回指定类型的完整合法 properties（必填字段齐备）。"""
        full = {
            "ProjectProfile": {
                "name": "x",
                "description": "y",
                "tech_stack": ["Python"],
                "architecture": "单体",
            },
            "Requirement": {
                "priority": "P0",
                "module": "auth",
            },
            "CodeSnippet": {
                "name": "S",
                "type": "class",
                "responsibility": "r",
                "file_path": "src/s.py",
            },
            "Solution": {"approach": "a", "version": "1"},
            "DesignIntent": {"rationale": "r", "trade_offs": "t"},
            "Decision": {"decision_id": "D1", "decision": "d"},
            "Pitfall": {"symptom": "s", "root_cause": "rc", "solution": "sol"},
        }
        return dict(full[node_type])


# ============ 封闭契约（未知字段拒绝 + 白名单内可选字段通过） ============

# 各类型白名单内的全部可选字段（与 schema.py xxx_OPTIONAL 对齐）
_ALL_OPTIONAL_FIELDS: dict[str, set[str]] = {
    "ProjectProfile": {"conventions", "team", "work_dir", "repo"},
    "Requirement": {"acceptance_criteria", "source_doc", "version", "external_id"},
    "CodeSnippet": {"signature", "snippet", "language"},
    "Solution": {"alternatives"},
    "DesignIntent": {"references"},
    "Decision": set(),
    "Pitfall": {"severity"},
}


class TestClosedContract:
    """封闭契约：必填 ∪ 可选白名单之外的未知字段一律拒绝。"""

    @pytest.mark.parametrize("node_type", sorted(NODE_TYPES))
    def test_unknown_field_rejected(self, node_type):
        """各类型含白名单外未知字段抛错，错误信息含未知字段名与合法字段列表。"""
        props = TestValidateNode._full_props(node_type)
        props["unknown_field"] = "x"
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_node(node_type, props)
        msg = str(exc_info.value)
        assert "未知字段" in msg
        assert "unknown_field" in msg
        # 错误信息列出该类型全部合法字段
        for field in ALLOWED_FIELDS[node_type]:
            assert field in msg

    def test_deprecated_requirement_id_rejected(self):
        """已废弃的 requirement_id 属未知字段，显式拒绝。"""
        with pytest.raises(SchemaValidationError, match="requirement_id"):
            validate_node(
                "Requirement",
                {"priority": "P0", "module": "auth", "requirement_id": "REQ-001"},
            )

    @pytest.mark.parametrize("node_type", sorted(NODE_TYPES))
    def test_all_optional_fields_pass(self, node_type):
        """白名单内全部可选字段（有值）与必填字段齐备时通过。"""
        props = TestValidateNode._full_props(node_type)
        for field in _ALL_OPTIONAL_FIELDS[node_type]:
            # severity 有枚举校验，取合法值
            props[field] = "P1" if field == "severity" else "v"
        validate_node(node_type, props)  # 不抛即通过

    @pytest.mark.parametrize("node_type", sorted(NODE_TYPES))
    def test_allowed_fields_consistent(self, node_type):
        """ALLOWED_FIELDS = 必填 ∪ 可选（与测试侧声明一致）。"""
        optional = _ALL_OPTIONAL_FIELDS[node_type]
        required = {
            "ProjectProfile": {"name", "description", "tech_stack", "architecture"},
            "Requirement": {"priority", "module"},
            "CodeSnippet": {"name", "type", "responsibility", "file_path"},
            "Solution": {"approach", "version"},
            "DesignIntent": {"rationale", "trade_offs"},
            "Decision": {"decision_id", "decision"},
            "Pitfall": {"symptom", "root_cause", "solution"},
        }[node_type]
        assert ALLOWED_FIELDS[node_type] == required | optional


# ============ 边类型校验 ============

class TestValidateEdgeType:
    """validate_edge_type 校验测试。"""

    @pytest.mark.parametrize(
        "edge_type",
        [
            "implements",
            "depends_on",
            "realized_by",
            "embodies",
            "traces_to",
            "conflicts_with",
            "duplicates",
            "relates_to",
            "supersedes",
            "version_of",
            "described_by",
            "references",
        ],
    )
    def test_valid_edge_types(self, edge_type):
        """PDD 定义的 12 种边类型均通过校验。"""
        validate_edge_type(edge_type)

    @pytest.mark.parametrize(
        "invalid_type",
        ["", "implement", "depends", "REFERENCES", "implements ", "references_x"],
    )
    def test_invalid_edge_types(self, invalid_type):
        """非法边类型抛 SchemaValidationError。"""
        with pytest.raises(SchemaValidationError, match="非法边类型"):
            validate_edge_type(invalid_type)

    def test_all_edge_types_in_EDGE_TYPES(self):
        """EDGE_TYPES 包含 PDD 定义的 12 种边类型。"""
        assert len(EDGE_TYPES) == 12
        assert "implements" in EDGE_TYPES
        assert "references" in EDGE_TYPES
