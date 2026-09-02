"""冲突检测：三层架构（硬门控 → 关键属性比对 → 内容语义相似度）。

单一实现，服务两个调用位置（approval/service.py）：
- auto_process_batch：写入批次前的前置检测（节点未写入，无需排除自身）
- review_approve：写入节点后的提示性检测（exclude_node_id 排除自身，
  可捕获同批次内先写入的重复节点）

设计依据（基于知识冲突检测最佳实践研究）：
1. 标题相似不等于内容冲突（"用户登录需求" vs "用户登录性能优化需求"标题相似但内容不同），
   查询文本用 build_embed_text（title+content+关键属性段）——与节点落库向量的构造
   完全一致，保证 query-doc 相似度在"内容相同"时达到高位
2. 内容级 embedding 比标题级 embedding 更精准（标题噪声大、信号弱、偏主题相关）
3. 关键属性比对是区分"同一实体"与"相关但不同实体"的硬判据
   （如不同 name+file_path 的代码片段是不同实体，即使标题完全相同）
 4. 内容级阈值（CONFLICT_SIMILARITY_THRESHOLD，默认 0.85）随嵌入模型变化需重新标定，
5. 标签共享只说明主题相关，不代表内容重复，不参与冲突判定
"""

import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.config import get_settings
from mem_lake.knowledge.embed import build_embed_text
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import FilterSpec
from mem_lake.search.vector import VectorSearcher

# 各节点类型的关键标识字段：用于 L2 关键属性比对
# 字段值不同 → 不同实体 → 不构成冲突
KEY_IDENTITY_FIELDS: dict[str, list[str]] = {
    "Requirement": [],  # 需求主键由服务端分配（requirement_key），不依赖业务属性判重
    "CodeSnippet": ["name", "file_path"],
    "Solution": ["approach"],
    "DesignIntent": ["rationale"],
    "Decision": ["decision_id"],
    "Pitfall": ["symptom"],
    "ProjectProfile": ["name"],
}

# 内容级冲突检测阈值
# 由配置驱动（不再硬编码模型相关值）：不同嵌入模型的余弦分布不同，
# 换模型后需按样本对实测重新标定该值（见配置 CONFLICT_SIMILARITY_THRESHOLD）。

CONFLICT_SIMILARITY_THRESHOLD = get_settings().CONFLICT_SIMILARITY_THRESHOLD


async def detect_conflicts(
    session: AsyncSession,
    *,
    vector_searcher: VectorSearcher,
    project_id: uuid.UUID | None = None,
    system_id: uuid.UUID | None = None,
    node_type: str,
    title: str,
    content: str,
    properties: dict,
    tags: list[str],
    exclude_node_id: uuid.UUID | None = None,
    top_k: int = 5,
    query_vector: list[float] | None = None,
) -> dict:
    """三层架构检测节点是否与已有知识冲突。

    三层检测流程：
    L1 硬门控：VectorSearcher 的 FilterSpec 已限制 project_id + node_type + status=approved，
       不同项目/类型的节点不会出现在候选集中，此层隐含在向量检索的过滤条件内。
    L2 关键属性比对：对向量检索召回的候选节点，比对类型特有标识字段，
       关键属性不同的节点直接排除（不同实体不构成冲突）。
    L3 内容语义相似度：用 f"{title}\\n{content}" 做向量检索（与存储时一致），
       相似度 ≥ CONFLICT_SIMILARITY_THRESHOLD（默认 0.85）且 L2 通过才视为冲突。

    参数：
        session: 异步数据库会话
        vector_searcher: VectorSearcher 实例
        project_id: 项目 ID（L1 过滤；悬浮需求为 None）
        system_id: 归属 system 域（L1 过滤；悬浮需求按此收口候选域），默认 None
        node_type: 节点类型（L1 过滤）
        title: 待检测节点标题
        content: 待检测节点正文
        properties: 待检测节点属性（L2 关键属性比对输入）
        tags: 待检测节点标签（仅记录，不参与冲突判定）
        exclude_node_id: 排除自身节点 ID（写入后检测时避免自检），默认 None
        top_k: 向量检索返回数，默认 5

    返回结构：
        {
            "has_conflict": bool,
            "conflicting_nodes": [
                {
                    "existing_node_id": "uuid",
                    "existing_node_title": "...",
                    "existing_node_type": "Requirement",
                    "similarity": 0.95,
                    "matched_key_attrs": {"name": "LoginService"},
                    "conflict_type": "duplicate"
                }
            ],
            "candidates_examined": int,
            "suggestion": "review" | None
        }
    """
    conflicting_nodes: list[dict] = []
    candidates_examined = 0

    # L1 + L3：向量检索（FilterSpec 内含 project_id/system_id + node_type + status=approved 过滤）
    # 查询文本用 build_embed_text（title+content+关键属性段），与 repository.create_node
    # 落库向量的构造一致（含属性段，属性富集提升"同实体"识别）。
    # query_vector 非空时跳过内部 embed，直接使用调用方批量预计算的查询向量（批量化优化）。
    # 悬浮需求（project_id=None）按 system_id 收口冲突候选域。
    filters = FilterSpec(
        project_id=project_id, system_id=system_id, node_types=(node_type,)
    )
    if query_vector is not None:
        vector_results = await vector_searcher.search_by_vector(
            session, query_vector, top_k=top_k, filters=filters
        )
    else:
        embed_input = build_embed_text(node_type, title, content, properties)
        vector_results = await vector_searcher.search(
            session, embed_input, top_k=top_k, filters=filters
        )

    for result in vector_results:
        # 排除自身节点（写入后检测场景）
        if exclude_node_id is not None and result.node_id == exclude_node_id:
            continue

        candidates_examined += 1

        # L3：内容语义相似度阈值过滤
        if result.score is None or result.score < CONFLICT_SIMILARITY_THRESHOLD:
            continue

        # L2：关键属性比对
        # 查询候选节点的完整属性，比对关键标识字段
        existing_node = await get_node_for_conflict(session, result.node_id)
        if existing_node is None:
            continue

        matched_attrs = _match_key_attrs(
            properties, existing_node.properties or {}, node_type
        )

        # L2 未通过（关键属性不同）→ 不同实体，不构成冲突
        if matched_attrs is None:
            continue

        # 三层全部通过 → 冲突
        conflicting_nodes.append(
            {
                "existing_node_id": str(result.node_id),
                "existing_node_title": result.title,
                "existing_node_type": node_type,
                "similarity": round(result.score, 4),
                "matched_key_attrs": matched_attrs,
                "conflict_type": "duplicate",
            }
        )

    # L0 硬判定：同项目同类型关键标识字段完全相同 → 直接判冲突
    # （与向量召回无关，捕获「关键标识相同但内容差异大」的漏检）
    exact_conflicts = await _detect_exact_key_conflicts(
        session,
        project_id=project_id,
        node_type=node_type,
        properties=properties,
        exclude_node_id=exclude_node_id,
    )
    _seen_ids = {c["existing_node_id"] for c in conflicting_nodes}
    for c in exact_conflicts:
        if c["existing_node_id"] not in _seen_ids:
            conflicting_nodes.append(c)
            _seen_ids.add(c["existing_node_id"])

    has_conflict = bool(conflicting_nodes)
    suggestion = "review" if has_conflict else None

    return {
        "has_conflict": has_conflict,
        "conflicting_nodes": conflicting_nodes,
        "candidates_examined": candidates_examined,
        "suggestion": suggestion,
    }


async def _detect_exact_key_conflicts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_type: str,
    properties: dict,
    exclude_node_id: uuid.UUID | None = None,
) -> list[dict]:
    """L0 硬判定：同项目同类型下关键标识字段完全相同即判冲突（不依赖向量相似度）。

    捕获三层检测只在向量相似度 ≥ 阈值时才比对关键属性时，
    「关键标识相同但内容/标题差异大」的重复节点漏检。
    """
    key_fields = KEY_IDENTITY_FIELDS.get(node_type, [])
    if not key_fields:
        return []

    # 新节点必须携带全部关键字段，否则无法精确匹配
    if any(properties.get(f) is None for f in key_fields):
        return []

    conditions = [
        KnowledgeNode.project_id == project_id,
        KnowledgeNode.type == node_type,
        KnowledgeNode.status == "approved",
        KnowledgeNode.is_deleted.is_(False),
    ]
    for field in key_fields:
        conditions.append(KnowledgeNode.properties[field].astext == str(properties[field]))

    stmt = select(KnowledgeNode).where(and_(*conditions))
    if exclude_node_id is not None:
        stmt = stmt.where(KnowledgeNode.id != exclude_node_id)

    result = await session.execute(stmt)
    nodes = list(result.scalars().all())

    conflicts = []
    for n in nodes:
        matched = {f: properties[f] for f in key_fields}
        conflicts.append(
            {
                "existing_node_id": str(n.id),
                "existing_node_title": n.title,
                "existing_node_type": node_type,
                "similarity": None,
                "matched_key_attrs": matched,
                "conflict_type": "duplicate",
            }
        )
    return conflicts


def _match_key_attrs(
    new_props: dict, existing_props: dict, node_type: str
) -> dict | None:
    """比对关键标识字段，全部相同返回匹配字典，任一不同返回 None。

    参数：
        new_props: 待检测节点属性
        existing_props: 已有节点属性
        node_type: 节点类型（确定哪些字段是关键标识）

    返回：
        匹配时：{"field": "value", ...}（所有关键字段及其值）
        不匹配时：None（关键属性不同 → 不同实体）
    """
    key_fields = KEY_IDENTITY_FIELDS.get(node_type, [])
    if not key_fields:
        # 未定义关键标识字段的类型，跳过 L2，直接进入 L3 判定
        return {}

    matched = {}
    for field in key_fields:
        new_val = new_props.get(field)
        existing_val = existing_props.get(field)
        if new_val != existing_val:
            return None
        matched[field] = new_val
    return matched


async def get_node_for_conflict(
    session: AsyncSession, node_id: uuid.UUID
) -> KnowledgeNode | None:
    """查询节点用于冲突检测（只查 approved 且未删除的节点）。

    与 repository.get_node 不同：不抛异常，返回 None 表示节点不存在或已归档。
    """
    stmt = (
        select(KnowledgeNode)
        .where(KnowledgeNode.id == node_id)
        .where(KnowledgeNode.status == "approved")
        .where(KnowledgeNode.is_deleted.is_(False))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
