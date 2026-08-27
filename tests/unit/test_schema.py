"""节点属性 Schema 校验单元测试。

覆盖：
- 各节点类型的合法 properties 通过校验
- 非法节点类型抛 SchemaValidationError
- 缺失必填字段抛 SchemaValidationError
- 边类型校验（合法/非法）
- properties 非 dict 类型
- 空字典、空字符串边界
- 额外字段（非必填）允许
- 校验不修改输入
"""

import pytest

from mem_lake.knowledge.schema import (
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
                {"name": "Mem Lake", "description": "记忆基础设施"},
            ),
            (
                "Requirement",
                {"requirement_id": "REQ-001", "priority": "P0", "module": "auth"},
            ),
            (
                "CodeSnippet",
                {"name": "LoginService", "type": "class", "responsibility": "登录"},
            ),
            (
                "Solution",
                {"approach": "JWT", "version": "1.0"},
            ),
            (
                "DesignIntent",
                {"rationale": "无状态易扩展"},
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
            ("Requirement", "priority"),
            ("Requirement", "module"),
            ("CodeSnippet", "name"),
            ("CodeSnippet", "type"),
            ("CodeSnippet", "responsibility"),
            ("Solution", "approach"),
            ("Solution", "version"),
            ("DesignIntent", "rationale"),
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

    def test_extra_fields_allowed(self):
        """properties 含非必填的额外字段，校验通过。"""
        props = {
            "name": "Mem Lake",
            "description": "记忆基础设施",
            "tech_stack": ["Python"],
            "team": {"pm": ["zhang"]},
        }
        validate_node("ProjectProfile", props)

    def test_input_not_mutated(self):
        """校验不修改输入 properties。"""
        props = {"name": "Mem Lake", "description": "x"}
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
        """返回指定类型的完整合法 properties。"""
        full = {
            "ProjectProfile": {"name": "x", "description": "y"},
            "Requirement": {
                "requirement_id": "REQ-001",
                "priority": "P0",
                "module": "auth",
            },
            "CodeSnippet": {
                "name": "S",
                "type": "class",
                "responsibility": "r",
            },
            "Solution": {"approach": "a", "version": "1"},
            "DesignIntent": {"rationale": "r"},
            "Decision": {"decision_id": "D1", "decision": "d"},
            "Pitfall": {"symptom": "s", "root_cause": "rc", "solution": "sol"},
        }
        return dict(full[node_type])


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
