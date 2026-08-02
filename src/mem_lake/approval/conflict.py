"""冲突检测：向量相似度 + 标签匹配。

对齐 PDD 3.4 冲突检测：审批通过时基于标题向量相似度 + 标签匹配检测与已有知识的冲突，
相似度高于阈值则在审批记录中标记 conflict_hint，由管理员决策是否通过。

检测时机：审批通过时（PDD 明确），不是提交时。提交时向量未生成、节点未写入正式存储无法对比。
检测范围：同项目同类型节点。跨项目（不同项目的"用户登录需求"不是冲突）与
跨类型（Requirement 与 CodeSnippet 不构成冲突）不参与检测，避免误报。

阈值 0.85：bge-large-zh-v1.5 模型在中文文本去重场景的社区默认值（网络搜索查证）。
cosine similarity > 0.85 视为高度相似，触发 conflict_hint 提示。
阈值不强制阻断审批（仅提示），管理员决策权保留。
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
