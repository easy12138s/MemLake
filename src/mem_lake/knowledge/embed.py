"""嵌入文本构造：将节点 title/content 与关键属性拼接为向量化输入。

PDD 3.3：向量检索基于 knowledge_node.content_vector。仅用 title+content 会丢失
properties 中的判别性信息（如 Pitfall.root_cause / CodeSnippet.name），导致按属性
关键词无法召回。此处按节点类型纳入关键属性，提升语义召回（对应 dev 测试报告 P2）。
"""

from typing import Any

# 各节点类型参与嵌入的关键属性键（顺序即拼接顺序）
EMBED_PROPERTY_FIELDS: dict[str, list[str]] = {
    "ProjectProfile": ["name", "description", "tech_stack", "architecture", "work_dir", "repo"],
    "Requirement": ["priority", "module", "acceptance_criteria"],
    "CodeSnippet": ["name", "type", "responsibility", "file_path", "language"],
    "Solution": ["approach", "version", "alternatives"],
    "DesignIntent": ["rationale", "trade_offs", "references"],
    "Decision": ["decision_id", "decision"],
    "Pitfall": ["symptom", "root_cause", "solution", "severity"],
}

# 单条属性值最大字符数，防止超长属性撑爆嵌入输入。
# 32k 上下文适配（A）：模型原生支持 32768 token，原 512 上限会丢弃长属性
# （如 Pitfall.root_cause / solution）的判别信息。放宽到 4096 仍远小于模型上限，
# 单 facet 文本即使用 Qwen3-Embedding-0.6B 也不会触及 token 截断守卫。
_EMBED_VALUE_MAX_LEN = 4096


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


# 多向量（D）：每个 facet 单独 embed，检索时按节点做 max-pooling（ColBERT 式
# maxsim），避免"title+content+全属性拼一段"导致的语义稀释；长属性（4096 上限）
# 也能独立承载判别信号，按属性关键词检索可精准召回对应 facet。
#
# facet 命名约定：
# - "content"：title + content（与冲突检测用 build_embed_text 的标题/正文部分对齐）
# - 其余为 EMBED_PROPERTY_FIELDS 中"有值"的属性键（如 "root_cause" / "solution"）
FACET_CONTENT = "content"


def build_embed_facets(
    node_type: str,
    title: str,
    content: str,
    properties: dict[str, Any] | None,
) -> dict[str, str]:
    """构造节点多向量嵌入输入：facet 名 → 该 facet 的向量化文本。

    返回 dict 至少含 "content" facet（title+content）；其余为节点类型关键属性中
    非空的字段，各自独立成 facet。空节点（无 title/content/属性）返回空 dict，
    调用方据此跳过向量写入（与 content_vector 为 NULL 的既有语义一致）。

    用于 create_node / update_node / regenerate_vector / batch_regenerate_vectors
    的逐 facet EmbeddingClient 输入；检索侧使用 max-pooling 融合。
    """
    facets: dict[str, str] = {}
    content_parts: list[str] = []
    if title:
        content_parts.append(title.strip())
    if content:
        content_parts.append(content.strip())
    if content_parts:
        facets[FACET_CONTENT] = "\n".join(content_parts)

    if properties:
        for key in EMBED_PROPERTY_FIELDS.get(node_type, []):
            rendered = _render_value(properties.get(key))
            if rendered:
                facets[key] = f"{key}: {rendered}"
    return facets
