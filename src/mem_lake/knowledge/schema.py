"""节点属性 Schema 规范定义与校验：ProjectProfile、Requirement、CodeSnippet 等。

对齐 PDD 4.4 节点属性 Schema 规范。每类节点的 properties JSONB 字段遵循特定 schema，
Mem Lake 负责校验。校验在 repository 写入前执行，不合规抛 SchemaValidationError。
"""

NODE_TYPES: frozenset[str] = frozenset({
    "ProjectProfile",
    "Requirement",
    "CodeSnippet",
    "Solution",
    "DesignIntent",
    "Decision",
    "Pitfall",
})

EDGE_TYPES: frozenset[str] = frozenset({
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
})

# 各类节点的必填字段定义（基于 PDD 4.4）
PROJECT_PROFILE_REQUIRED: set[str] = {"name", "description"}
REQUIREMENT_REQUIRED: set[str] = {"requirement_id", "priority", "module"}
CODE_SNIPPET_REQUIRED: set[str] = {"name", "type", "responsibility"}
SOLUTION_REQUIRED: set[str] = {"approach", "version"}
DESIGN_INTENT_REQUIRED: set[str] = {"rationale"}
DECISION_REQUIRED: set[str] = {"decision_id", "decision"}
PITFALL_REQUIRED: set[str] = {"symptom", "solution"}

NODE_SCHEMA: dict[str, set[str]] = {
    "ProjectProfile": PROJECT_PROFILE_REQUIRED,
    "Requirement": REQUIREMENT_REQUIRED,
    "CodeSnippet": CODE_SNIPPET_REQUIRED,
    "Solution": SOLUTION_REQUIRED,
    "DesignIntent": DESIGN_INTENT_REQUIRED,
    "Decision": DECISION_REQUIRED,
    "Pitfall": PITFALL_REQUIRED,
}


class SchemaValidationError(Exception):
    """节点属性或边类型校验失败时抛出。"""


def validate_node_type(node_type: str) -> None:
    """校验节点类型是否在 NODE_TYPES 中。不合规抛 SchemaValidationError。

    用于图节点写入等仅需校验类型的场景（图节点不存完整 properties）。
    """
    if node_type not in NODE_TYPES:
        raise SchemaValidationError(
            f"非法节点类型: {node_type}，合法类型: {sorted(NODE_TYPES)}"
        )


def validate_node(node_type: str, properties: dict) -> None:
    """校验节点类型与 properties 必填字段。

    - 校验 node_type 在 NODE_TYPES 中
    - 校验 properties 包含该类型的所有必填字段
    不合规抛 SchemaValidationError，含缺失字段列表。

    用于 knowledge_node 表写入前的完整校验（repository.create_node/update_node）。
    图节点写入（age_store.add_node）只需 validate_node_type，避免冗余校验。
    """
    validate_node_type(node_type)

    if not isinstance(properties, dict):
        raise SchemaValidationError(
            f"properties 必须为 dict，实际类型: {type(properties).__name__}"
        )

    required = NODE_SCHEMA[node_type]
    missing = required - set(properties.keys())
    if missing:
        raise SchemaValidationError(
            f"节点 {node_type} 缺失必填字段: {sorted(missing)}，"
            f"必填字段: {sorted(required)}"
        )


def validate_edge_type(edge_type: str) -> None:
    """校验边类型是否在 EDGE_TYPES 中。不合规抛 SchemaValidationError。"""
    if edge_type not in EDGE_TYPES:
        raise SchemaValidationError(
            f"非法边类型: {edge_type}，合法类型: {sorted(EDGE_TYPES)}"
        )
