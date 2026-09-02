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

# 各类节点的必填字段定义（与工具层 Field description 契约对齐）
PROJECT_PROFILE_REQUIRED: set[str] = {"name", "description", "tech_stack", "architecture"}
REQUIREMENT_REQUIRED: set[str] = {"priority", "module"}
CODE_SNIPPET_REQUIRED: set[str] = {"name", "type", "responsibility", "file_path"}
SOLUTION_REQUIRED: set[str] = {"approach", "version"}
DESIGN_INTENT_REQUIRED: set[str] = {"rationale", "trade_offs"}
DECISION_REQUIRED: set[str] = {"decision_id", "decision"}
PITFALL_REQUIRED: set[str] = {"symptom", "root_cause", "solution"}

# 各类节点的可选字段白名单。properties 为封闭契约（对齐 JSON Schema
# additionalProperties: false 惯例）：必填 ∪ 可选之外的未知字段一律拒绝，
# 避免杂字段（含已废弃的 requirement_id 等）静默入库。
PROJECT_PROFILE_OPTIONAL: set[str] = {"conventions", "team", "work_dir", "repo"}
REQUIREMENT_OPTIONAL: set[str] = {"acceptance_criteria", "source_doc", "version", "external_id"}
CODE_SNIPPET_OPTIONAL: set[str] = {"signature", "snippet", "language"}
SOLUTION_OPTIONAL: set[str] = {"alternatives"}
DESIGN_INTENT_OPTIONAL: set[str] = {"references"}
DECISION_OPTIONAL: set[str] = set()
PITFALL_OPTIONAL: set[str] = {"severity"}

NODE_SCHEMA: dict[str, set[str]] = {
    "ProjectProfile": PROJECT_PROFILE_REQUIRED,
    "Requirement": REQUIREMENT_REQUIRED,
    "CodeSnippet": CODE_SNIPPET_REQUIRED,
    "Solution": SOLUTION_REQUIRED,
    "DesignIntent": DESIGN_INTENT_REQUIRED,
    "Decision": DECISION_REQUIRED,
    "Pitfall": PITFALL_REQUIRED,
}

# 每类节点的合法字段全集（必填 ∪ 可选）
ALLOWED_FIELDS: dict[str, set[str]] = {
    "ProjectProfile": PROJECT_PROFILE_REQUIRED | PROJECT_PROFILE_OPTIONAL,
    "Requirement": REQUIREMENT_REQUIRED | REQUIREMENT_OPTIONAL,
    "CodeSnippet": CODE_SNIPPET_REQUIRED | CODE_SNIPPET_OPTIONAL,
    "Solution": SOLUTION_REQUIRED | SOLUTION_OPTIONAL,
    "DesignIntent": DESIGN_INTENT_REQUIRED | DESIGN_INTENT_OPTIONAL,
    "Decision": DECISION_REQUIRED | DECISION_OPTIONAL,
    "Pitfall": PITFALL_REQUIRED | PITFALL_OPTIONAL,
}

# Pitfall 严重级合法枚举（描述承诺 P0~P3，schema 层枚举校验）
SEVERITY_ENUM: frozenset[str] = frozenset({"P0", "P1", "P2", "P3"})


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
    """校验节点类型、properties 必填字段与封闭字段白名单。

    - 校验 node_type 在 NODE_TYPES 中
    - 校验 properties 包含该类型的所有必填字段
    - 校验 properties 不含白名单（必填 ∪ 可选）之外的未知字段
    不合规抛 SchemaValidationError，错误信息含缺失/未知字段与合法字段列表。

    用于 knowledge_node 表写入前的完整校验（repository.create_node/update_node），
    亦被审批提交时校验（approval._validate_item_payload）复用。
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

    allowed = ALLOWED_FIELDS[node_type]
    unknown = set(properties.keys()) - allowed
    if unknown:
        raise SchemaValidationError(
            f"节点 {node_type} 含未知字段: {sorted(unknown)}，"
            f"合法字段: {sorted(allowed)}"
        )

    # Pitfall severity 枚举校验（存在则须合法，避免非法严重级入库）
    if node_type == "Pitfall" and "severity" in properties:
        if properties["severity"] not in SEVERITY_ENUM:
            raise SchemaValidationError(
                f"节点 Pitfall 的 severity 非法: {properties['severity']!r}，"
                f"合法值: {sorted(SEVERITY_ENUM)}"
            )


def validate_edge_type(edge_type: str) -> None:
    """校验边类型是否在 EDGE_TYPES 中。不合规抛 SchemaValidationError。"""
    if edge_type not in EDGE_TYPES:
        raise SchemaValidationError(
            f"非法边类型: {edge_type}，合法类型: {sorted(EDGE_TYPES)}"
        )
