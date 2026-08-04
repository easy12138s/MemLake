"""冲突检测：向量相似度 + 标签匹配 + 关键属性比对。

模块包含两套冲突检测函数：
- detect_conflicts：审批通过后的事后提示性检测（宽松，基于标题向量 + 标签匹配）
- detect_conflicts_v2：自动审批的前置检测（严格，三层架构：硬门控→关键属性→内容语义）

detect_conflicts_v2 的设计依据（基于知识冲突检测最佳实践研究）：
1. 标题相似不等于内容冲突（"用户登录需求" vs "用户登录性能优化需求"标题相似但内容不同）
2. 内容级 embedding 比 标题级 embedding 更精准（标题噪声大、信号弱、偏主题相关）
3. 关键属性比对是区分"同一实体"与"相关但不同实体"的硬判据
   （不同 requirement_id 的需求是不同实体，即使标题完全相同）
4. 内容级阈值 0.92（而非 0.85）在语义等价判定中 F1 最优，能区分"相关"与"重复"
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import FilterSpec
from mem_lake.search.vector import VectorSearcher


async def detect_conflicts(
    session: AsyncSession,
    *,
    vector_searcher: VectorSearcher,
    project_id: uuid.UUID,
    node_type: str,
    title: str,
    tags: list[str],
    exclude_node_id: uuid.UUID | None = None,
    similarity_threshold: float = 0.85,
    top_k: int = 5,
) -> dict:
    """检测与已有知识的冲突。

    参数：
        session: 异步数据库会话
        vector_searcher: VectorSearcher 实例（用于向量相似度检索）
        project_id: 项目 ID（限定检测范围，只对比同项目节点）
        node_type: 节点类型（限定检测范围，只对比同类型节点）
        title: 待检测节点标题（向量化输入）
        tags: 待检测节点标签（标签匹配输入）
        exclude_node_id: 排除自身节点 ID（审批通过时节点已写入，避免自检）
        similarity_threshold: 相似度阈值，默认 0.85
            （bge-large-zh-v1.5 中文文本去重社区默认值，cosine similarity > 0.85 视为高度相似）
        top_k: 向量检索返回数，默认 5

    返回 conflict_hint 结构：
        {
            "similar_nodes": [
                {"node_id": "uuid-str", "title": "...", "similarity": 0.92, "tags": [...]}
            ],
            "tag_matches": [
                {"node_id": "uuid-str", "title": "...", "shared_tags": ["auth"]}
            ],
            "has_conflict": True/False,
            "suggestion": "review" / "manual_merge" / None
        }

    检测逻辑：
    1. 向量相似度：VectorSearcher.search(title, top_k, FilterSpec(project_id, node_types=(node_type,)))
       过滤 score >= similarity_threshold 的结果，排除自身节点
    2. 标签匹配：查询同项目同类型节点中 tags 与待检测 tags 有交集的节点
       （PG JSONB 查询 + Python 端 set 交集判断）
    3. 合并去重：同一节点既相似又标签匹配时，在 similar_nodes 中保留并标注 tag_matches
    4. 建议生成：
       - has_conflict=False → suggestion=None
       - 存在 similar_nodes（相似度高）→ suggestion="review"
       - 仅存在 tag_matches 无 similar_nodes → suggestion="manual_merge"
    """
    similar_nodes: list[dict] = []
    tag_matches: list[dict] = []
    seen_similar_ids: set[str] = set()

    # 1. 向量相似度检测
    filters = FilterSpec(project_id=project_id, node_types=(node_type,))
    vector_results = await vector_searcher.search(session, title, top_k=top_k, filters=filters)

    for result in vector_results:
        # 排除自身节点
        if exclude_node_id is not None and result.node_id == exclude_node_id:
            continue
        # score = 1 - cosine_distance，范围 0~1
        if result.score is not None and result.score >= similarity_threshold:
            similar_nodes.append(
                {
                    "node_id": str(result.node_id),
                    "title": result.title,
                    "similarity": round(result.score, 4),
                    "tags": result.tags,
                }
            )
            seen_similar_ids.add(str(result.node_id))

    # 2. 标签匹配检测
    if tags:
        tag_match_nodes = await _find_nodes_with_shared_tags(
            session,
            project_id=project_id,
            node_type=node_type,
            tags=tags,
            exclude_node_id=exclude_node_id,
        )
        for node in tag_match_nodes:
            shared = set(node.tags or []) & set(tags)
            if shared:
                tag_matches.append(
                    {
                        "node_id": str(node.id),
                        "title": node.title,
                        "shared_tags": sorted(shared),
                    }
                )

    # 3. 生成建议
    has_conflict = bool(similar_nodes) or bool(tag_matches)
    if not has_conflict:
        suggestion = None
    elif similar_nodes:
        suggestion = "review"
    else:
        suggestion = "manual_merge"

    return {
        "similar_nodes": similar_nodes,
        "tag_matches": tag_matches,
        "has_conflict": has_conflict,
        "suggestion": suggestion,
    }


async def _find_nodes_with_shared_tags(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_type: str,
    tags: list[str],
    exclude_node_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[KnowledgeNode]:
    """查询同项目同类型节点中 tags 与给定 tags 有交集的节点。

    策略：先按 project_id + type 过滤候选，Python 端做 set 交集判断。
    tags 列为 JSONB 数组，PostgreSQL 原生不支持"数组交集"操作符（@> 是子集判断），
    改用候选集 + Python 端交集，避免全表扫描且语义明确。
    """
    stmt = (
        select(KnowledgeNode)
        .where(KnowledgeNode.project_id == project_id)
        .where(KnowledgeNode.type == node_type)
        .where(KnowledgeNode.status == "approved")
        .where(KnowledgeNode.is_deleted.is_(False))
        .limit(limit * 5)  # 候选集放大，避免过滤后不足
    )
    if exclude_node_id is not None:
        stmt = stmt.where(KnowledgeNode.id != exclude_node_id)

    result = await session.execute(stmt)
    candidates = result.scalars().all()

    # Python 端交集过滤
    tag_set = set(tags)
    matched: list[KnowledgeNode] = []
    for node in candidates:
        node_tags = set(node.tags or [])
        if node_tags & tag_set:
            matched.append(node)
            if len(matched) >= limit:
                break

    return matched


# ============================================================================
# V2: 自动审批前置冲突检测（三层架构）
# ============================================================================

# 各节点类型的关键标识字段：用于 L2 关键属性比对
# 字段值不同 → 不同实体 → 不构成冲突
KEY_IDENTITY_FIELDS: dict[str, list[str]] = {
    "Requirement": ["requirement_id"],
    "CodeSnippet": ["name", "file_path"],
    "Solution": ["approach"],
    "DesignIntent": ["rationale"],
    "Decision": ["decision_id"],
    "Pitfall": ["symptom"],
    "ProjectProfile": ["name"],
}

# 内容级冲突检测阈值
# 0.92：bge-large-zh-v1.5 在内容级语义等价判定中，0.92+ 视为高度重复
# 远高于标题级 0.85，因为内容级 embedding 信息更完整、信号更强
CONFLICT_SIMILARITY_THRESHOLD_V2 = 0.92


async def detect_conflicts_v2(
    session: AsyncSession,
    *,
    vector_searcher: VectorSearcher,
    project_id: uuid.UUID,
    node_type: str,
    title: str,
    content: str,
    properties: dict,
    tags: list[str],
    top_k: int = 5,
) -> dict:
    """前置冲突检测（审批前）：三层架构检测节点是否与已有知识冲突。

    三层检测流程：
    L1 硬门控：VectorSearcher 的 FilterSpec 已限制 project_id + node_type + status=approved，
       不同项目/类型的节点不会出现在候选集中，此层隐含在向量检索的过滤条件内。
    L2 关键属性比对：对向量检索召回的候选节点，比对类型特有标识字段，
       关键属性不同的节点直接排除（不同实体不构成冲突）。
    L3 内容语义相似度：用 f"{title}\\n{content}" 做向量检索（与存储时一致），
       相似度 ≥ 0.92 且 L2 通过才视为冲突。

    与 detect_conflicts 的核心区别：
    - 用 f"{title}\\n{content}" 做向量检索（而非仅标题），信息更完整
    - 增加关键属性比对（L2），不同实体直接排除，避免误报
    - 阈值 0.92（而非 0.85），降低误报率
    - 节点未写入，无需 exclude_node_id
    - 不做标签匹配（标签共享只说明主题相关，不代表内容重复）

    参数：
        session: 异步数据库会话
        vector_searcher: VectorSearcher 实例
        project_id: 项目 ID（L1 过滤）
        node_type: 节点类型（L1 过滤）
        title: 待检测节点标题
        content: 待检测节点正文
        properties: 待检测节点属性（L2 关键属性比对输入）
        tags: 待检测节点标签（仅记录，不参与冲突判定）
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
                    "matched_key_attrs": {"requirement_id": "REQ-001"},
                    "conflict_type": "duplicate"
                }
            ],
            "candidates_examined": int,
            "suggestion": "review" | None
        }
    """
    conflicting_nodes: list[dict] = []
    candidates_examined = 0

    # L1 + L3：向量检索（FilterSpec 内含 project_id + node_type + status=approved 过滤）
    # 用 f"{title}\n{content}" 做查询（与 repository.create_node 向量生成方式一致）
    embed_input = f"{title}\n{content}"
    filters = FilterSpec(project_id=project_id, node_types=(node_type,))
    vector_results = await vector_searcher.search(
        session, embed_input, top_k=top_k, filters=filters
    )

    for result in vector_results:
        candidates_examined += 1

        # L3：内容语义相似度阈值过滤
        if result.score is None or result.score < CONFLICT_SIMILARITY_THRESHOLD_V2:
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

    has_conflict = bool(conflicting_nodes)
    suggestion = "review" if has_conflict else None

    return {
        "has_conflict": has_conflict,
        "conflicting_nodes": conflicting_nodes,
        "candidates_examined": candidates_examined,
        "suggestion": suggestion,
    }


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
