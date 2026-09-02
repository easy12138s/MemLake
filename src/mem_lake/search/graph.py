"""图遍历检索：AGE Cypher 多跳遍历、需求上下文遍历、影响范围分析。

对齐 PDD 3.3 图引擎：负责"需求 R1 实现涉及哪些代码、设计意图"等关系遍历场景。
AGE 图遍历原语（neighbors）在 M3 已实现并验证，M4 封装为检索接口。

图遍历作为独立检索路径（PDD 3.3）：
- 不参与 RRF 融合（向量/全文融合基于文本相似度，图遍历基于关系结构，二者维度不同）
- hybrid_search 返回 {"fused": ..., "graph": ...}，graph 字段独立
- 组合模式：先向量找起点（vector_search），再图遍历展开（graph_traverse）

实现策略：
- AGE 图节点只存 id/project_id/title，完整内容在 knowledge_node 表
- GraphSearcher 先调用 GraphStore 原语获取邻居节点 ID 列表，再批量查 PG 表获取完整内容
- FilterSpec 在 PG 表查询阶段过滤（AGE 图节点不存 status/is_deleted/tags/created_at）
"""

import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import FilterSpec, compile_sqlalchemy
from mem_lake.search.fusion import SearchResult, _truncate


def _extract_node_id(agtype_dict: Any) -> uuid.UUID | None:
    """从单个 agtype 节点 dict 提取节点 UUID；缺失/非法 id 返回 None（跳过）。"""
    if not isinstance(agtype_dict, dict):
        return None
    nid_str = (agtype_dict.get("properties") or {}).get("id")
    if not nid_str:
        return None
    try:
        return uuid.UUID(str(nid_str))
    except (ValueError, AttributeError):
        return None


def _extract_node_ids(agtype_dicts: Iterable[Any]) -> list[uuid.UUID]:
    """从 agtype 节点 dict 列表批量提取 UUID（跳过非法项）。"""
    ids: list[uuid.UUID] = []
    for d in agtype_dicts:
        nid = _extract_node_id(d)
        if nid is not None:
            ids.append(nid)
    return ids


class GraphSearcher:
    """图遍历检索器。

    构造接收 GraphStore 实现（AGEGraphStore）。
    traverse 方法基于 neighbors 多跳遍历，返回 SearchResult 列表（score=None，无相似度概念）。
    context_traverse/impact_analysis 提供更丰富的图查询接口。
    """

    def __init__(self, graph_store: GraphStore) -> None:
        self._graph_store = graph_store

    async def traverse(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
        edge_type: str | None = None,
        depth: int = 3,
        filters: FilterSpec | None = None,
    ) -> list[SearchResult]:
        """多跳邻居遍历检索。

        参数：
            session: 异步数据库会话
            node_id: 遍历起点节点 ID
            edge_type: 边类型过滤，None 表示不限类型
            depth: 遍历深度，默认 3
            filters: 统一过滤条件（在 PG 表查询阶段过滤，AGE 层不过滤 status/is_deleted 等）

        返回：
            SearchResult 列表，score=None（图遍历无相似度），source="graph"。
            顺序由 AGE 返回顺序决定（不做二次排序）。

        边界：
            - 起点节点不存在返回空列表（neighbors 对不存在节点返回空）
            - 起点节点无边返回空列表
        """
        # 1. 调用 GraphStore.neighbors 获取邻居节点（agtype dict 列表，含 properties.id）
        neighbor_dicts = await self._graph_store.neighbors(
            session, node_id, edge_type=edge_type, depth=depth
        )

        if not neighbor_dicts:
            return []

        # 2. 提取邻居节点 ID（agtype dict 结构：{"properties": {"id": "uuid-str"}, ...}）
        neighbor_ids = _extract_node_ids(neighbor_dicts)

        if not neighbor_ids:
            return []

        # 3. 批量查询 knowledge_node 表获取完整内容
        stmt = select(KnowledgeNode).where(KnowledgeNode.id.in_(neighbor_ids))

        # 编译 FilterSpec 为 WHERE 子句（status/is_deleted/tags 等过滤在 PG 阶段）
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            stmt = stmt.where(*where_clauses)

        result = await session.execute(stmt)

        # 4. 构造 SearchResult 列表
        search_results: list[SearchResult] = []
        for node in result.scalars():
            search_results.append(
                SearchResult(
                    node_id=node.id,
                    title=node.title,
                    content=_truncate(node.content),
                    node_type=node.type,
                    score=None,  # 图遍历无相似度分数
                    source="graph",
                    properties=node.properties or {},
                    tags=node.tags or [],
                )
            )

        return search_results

    async def context_traverse(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
        depth: int = 2,
        filters: FilterSpec | None = None,
    ) -> list[SearchResult]:
        """需求上下文遍历：返回邻居节点并透出真实 edge_type（路径边类型）与 depth（跳数）。

        区别于 traverse：本方法走 GraphStore.neighbors_with_context，附带路径边类型与
        跳数，供 get_requirement_context 替换原 "unknown" 占位字段。遍历为无向，direction
        无意义，故不返回。
        """
        neighbor_ctxs = await self._graph_store.neighbors_with_context(
            session, node_id, depth=depth
        )
        if not neighbor_ctxs:
            return []

        id_to_ctx: dict[uuid.UUID, dict] = {}
        neighbor_ids: list[uuid.UUID] = []
        for nc in neighbor_ctxs:
            nid = _extract_node_id(nc.get("node"))
            if nid is None:
                continue
            if nid not in id_to_ctx:
                id_to_ctx[nid] = {
                    "edge_types": nc.get("edge_types") or [],
                    "depth": nc.get("depth"),
                }
                neighbor_ids.append(nid)

        if not neighbor_ids:
            return []

        stmt = select(KnowledgeNode).where(KnowledgeNode.id.in_(neighbor_ids))
        where_clauses = compile_sqlalchemy(filters)
        if where_clauses:
            stmt = stmt.where(*where_clauses)
        result = await session.execute(stmt)

        search_results: list[SearchResult] = []
        for node in result.scalars():
            ctx = id_to_ctx.get(node.id)
            search_results.append(
                SearchResult(
                    node_id=node.id,
                    title=node.title,
                    content=_truncate(node.content),
                    node_type=node.type,
                    score=None,
                    source="graph",
                    properties=node.properties or {},
                    tags=node.tags or [],
                    edge_types=ctx.get("edge_types") if ctx else None,
                    graph_depth=ctx.get("depth") if ctx else None,
                )
            )
        return search_results

    async def impact_analysis(
        self,
        session: AsyncSession,
        requirement_id: uuid.UUID,
        max_depth: int = 5,
    ) -> dict:
        """影响范围分析（PDD 4.3 示例）。

        从需求出发，遍历所有关联代码及其依赖，再展开到方案与设计意图：
        Requirement --implements--> CodeSnippet --depends_on--> CodeSnippet
        CodeSnippet --realized_by--> Solution --embodies--> DesignIntent

        过滤规则：所有节点以 PG 表为准（status=approved 且未软删除）——
        AGE 图节点不存 status/is_deleted（归档节点默认保留图投影以维持
        遍历历史），故遍历结果统一经 PG 过滤后才返回。

        参数：
            session: 异步数据库会话
            requirement_id: 需求节点 ID
            max_depth: depends_on 依赖链遍历深度，默认 5

        返回：
            {
                "requirement": <node dict or None>,
                "codes": [<node dict>, ...],       # 直接实现该需求的代码
                "dependencies": [<node dict>, ...], # 代码依赖链（去重）
                "solutions": [<node dict>, ...],   # 代码对应的实现方案
                "design_intents": [<node dict>, ...], # 方案体现的设计意图
            }
        """
        # 1. 需求节点：PG 查询（存在 + approved + 未软删除），不存在返回全空结果
        req_row = (
            await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.id == requirement_id,
                    KnowledgeNode.status == "approved",
                    KnowledgeNode.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if req_row is None:
            return {
                "requirement": None,
                "codes": [],
                "dependencies": [],
                "solutions": [],
                "design_intents": [],
            }
        requirement = {
            "id": str(req_row.id),
            "label": req_row.type,
            "properties": {
                "id": str(req_row.id),
                "title": req_row.title,
                "project_id": str(req_row.project_id) if req_row.project_id else None,
                "system_id": str(req_row.system_id) if req_row.system_id else None,
            },
        }

        # 2. 获取实现代码（Requirement --implements--> CodeSnippet）
        code_dicts = await self._graph_store.neighbors(
            session, requirement_id, edge_type="implements", depth=1
        )

        # 3. 对每个代码节点，遍历 depends_on 依赖链与 realized_by 方案
        all_dependencies: list[dict] = []
        all_solutions: list[dict] = []
        seen_dep_ids: set[str] = set()
        seen_sol_ids: set[str] = set()

        for code_dict in code_dicts:
            code_id = _extract_node_id(code_dict)
            if code_id is None:
                continue

            # 3a. 依赖链遍历（depth=max_depth）
            dep_dicts = await self._graph_store.neighbors(
                session, code_id, edge_type="depends_on", depth=max_depth
            )
            for dep in dep_dicts:
                if isinstance(dep, dict):
                    dep_id = (dep.get("properties") or {}).get("id")
                    if dep_id and dep_id not in seen_dep_ids:
                        seen_dep_ids.add(dep_id)
                        all_dependencies.append(dep)

            # 3b. 实现方案（CodeSnippet --realized_by--> Solution）
            sol_dicts = await self._graph_store.neighbors(
                session, code_id, edge_type="realized_by", depth=1
            )
            for sol in sol_dicts:
                if isinstance(sol, dict):
                    sol_id = (sol.get("properties") or {}).get("id")
                    if sol_id and sol_id not in seen_sol_ids:
                        seen_sol_ids.add(sol_id)
                        all_solutions.append(sol)

        # 4. PG 过滤 codes/dependencies/solutions（archived 图投影保留但按状态过滤）
        approved_ids = await self._query_approved_ids(
            session, _extract_node_ids([*code_dicts, *all_dependencies, *all_solutions])
        )

        def _filter_approved(dicts: list[dict]) -> list[dict]:
            return [
                d
                for d in dicts
                if isinstance(d, dict)
                and (d.get("properties") or {}).get("id") in approved_ids
            ]

        codes = _filter_approved(code_dicts)
        dependencies = _filter_approved(all_dependencies)
        solutions = _filter_approved(all_solutions)

        # 5. 仅对 approved 的方案展开设计意图（Solution --embodies--> DesignIntent）
        #    （归档方案不再展开其意图：方案已不构成影响范围，其意图同样不算）
        all_intents: list[dict] = []
        seen_intent_ids: set[str] = set()
        for sol in solutions:
            sol_uuid = _extract_node_id(sol)
            if sol_uuid is None:
                continue
            intent_dicts = await self._graph_store.neighbors(
                session, sol_uuid, edge_type="embodies", depth=1
            )
            for intent in intent_dicts:
                if isinstance(intent, dict):
                    intent_id = (intent.get("properties") or {}).get("id")
                    if intent_id and intent_id not in seen_intent_ids:
                        seen_intent_ids.add(intent_id)
                        all_intents.append(intent)

        intent_approved_ids = await self._query_approved_ids(
            session, _extract_node_ids(all_intents)
        )
        design_intents = [
            d
            for d in all_intents
            if isinstance(d, dict)
            and (d.get("properties") or {}).get("id") in intent_approved_ids
        ]

        return {
            "requirement": requirement,
            "codes": codes,
            "dependencies": dependencies,
            "solutions": solutions,
            "design_intents": design_intents,
        }

    async def _query_approved_ids(
        self, session: AsyncSession, node_ids: list[uuid.UUID]
    ) -> set[str]:
        """批量查询节点中 approved 且未软删除的 id 字符串集合。"""
        if not node_ids:
            return set()
        result = await session.execute(
            select(KnowledgeNode.id).where(
                KnowledgeNode.id.in_(node_ids),
                KnowledgeNode.status == "approved",
                KnowledgeNode.is_deleted.is_(False),
            )
        )
        return {str(row[0]) for row in result}
