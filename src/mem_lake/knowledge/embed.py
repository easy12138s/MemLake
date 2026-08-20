"""嵌入文本构造：将节点 title/content 与关键属性拼接为向量化输入。

PDD 3.3：向量检索基于 knowledge_node.content_vector。仅用 title+content 会丢失
properties 中的判别性信息（如 Pitfall.root_cause / CodeSnippet.name），导致按属性
关键词无法召回。此处按节点类型纳入关键属性，提升语义召回（对应 dev 测试报告 P2）。
"""

from typing import Any

# 各节点类型参与嵌入的关键属性键（顺序即拼接顺序）
EMBED_PROPERTY_FIELDS: dict[str, list[str]] = {
    "ProjectProfile": ["name", "description", "tech_stack", "architecture"],
    "Requirement": ["requirement_id", "priority", "module", "acceptance_criteria"],
    "CodeSnippet": ["name", "type", "responsibility", "file_path", "language"],
    "Solution": ["approach", "version", "alternatives"],
    "DesignIntent": ["rationale", "trade_offs", "references"],
    "Decision": ["decision_id", "decision"],
    "Pitfall": ["symptom", "root_cause", "solution", "severity"],
}

# 单条属性值最大字符数，防止超长属性撑爆嵌入输入
_EMBED_VALUE_MAX_LEN = 512


def _render_value(value: Any) -> str:
    """将属性值渲染为字符串（列表/字典展开，超长截断）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        rendered = " ".join(str(v) for v in value)
    elif isinstance(value, dict):
        rendered = " ".join(f"{k}={v}" for k, v in value.items())
    else:
        rendered = str(value)
    if len(rendered) > _EMBED_VALUE_MAX_LEN:
        rendered = rendered[:_EMBED_VALUE_MAX_LEN] + "..."
    return rendered


def build_embed_text(
    node_type: str,
    title: str,
    content: str,
    properties: dict[str, Any] | None,
) -> str:
    """构造节点向量化输入文本。

    结构：title + 正文 + 选中关键属性（k: v）。空属性跳过。
    用于 create_node / update_node / regenerate_vector 的 EmbeddingClient 输入。
    """
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if content:
        parts.append(content.strip())
    if properties:
        for key in EMBED_PROPERTY_FIELDS.get(node_type, []):
            rendered = _render_value(properties.get(key))
            if rendered:
                parts.append(f"{key}: {rendered}")
    return "\n".join(parts)
