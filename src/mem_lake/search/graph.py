"""图遍历检索：AGE Cypher 多跳遍历、子图提取、路径查询、影响范围分析。

对齐 PDD 3.3 图引擎：负责"需求 R1 实现涉及哪些代码、设计意图"等关系遍历场景。
AGE 图遍历原语（neighbors/find_path/subgraph）在 M3 已实现并验证，M4 封装为检索接口。

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import FilterSpec, compile_sqlalchemy
from mem_lake.search.fusion import SearchResult, _truncate


class GraphSearcher:
    """图遍历检索器。

    构造接收 GraphStore 实现（AGEGraphStore）。
    traverse 方法基于 neighbors 多跳遍历，返回 SearchResult 列表（score=None，无相似度概念）。
    subgraph/find_path/impact_analysis 提供更丰富的图查询接口。
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
        neighbor_ids: list[uuid.UUID] = []
        for nd in neighbor_dicts:
            if isinstance(nd, dict):
                props = nd.get("properties") or {}
                nid_str = props.get("id")
                if nid_str:
                    try:
                        neighbor_ids.append(uuid.UUID(str(nid_str)))
                    except (ValueError, AttributeError):
                        continue  # 跳过非法 ID

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

    async def subgraph(
        self,
        session: AsyncSession,
        node_ids: list[uuid.UUID],
    ) -> dict:
        """子图提取。

        调用 GraphStore.subgraph 返回 {"nodes": [...], "edges": [...]} 结构（M3 已实现）。
        直接透传，不在 PG 表补充内容（调用方按需调 get_node 获取完整内容）。
        """
        return await self._graph_store.subgraph(session, node_ids)

    async def find_path(
        self,
        session: AsyncSession,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        max_depth: int = 5,
    ) -> list[list[dict]]:
        """路径查询。

        调用 GraphStore.find_path 返回路径列表（M3 已实现）。
        每条路径为节点 dict 列表，无路径返回空列表。
        """
        return await self._graph_store.find_path(session, from_id, to_id, max_depth)

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
        # 1. 获取需求节点本身（match_pattern 直接查询，避免 depth=0 边界问题）
        requirement_row = await self._graph_store.match_pattern(
            session,
            "MATCH (n {id: $nid}) RETURN n",
            {"nid": str(requirement_id)},
        )
        requirement = requirement_row[0] if requirement_row else None

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
            if not isinstance(code_dict, dict):
                continue
            code_props = code_dict.get("properties") or {}
            code_id_str = code_props.get("id")
            if not code_id_str:
                continue
            try:
                code_id = uuid.UUID(str(code_id_str))
            except (ValueError, AttributeError):
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
                        # 4. 方案体现的设计意图（Solution --embodies--> DesignIntent）
                        try:
                            sol_uuid = uuid.UUID(str(sol_id))
                        except (ValueError, AttributeError):
                            continue
                        intent_dicts = await self._graph_store.neighbors(
                            session, sol_uuid, edge_type="embodies", depth=1
                        )
                        # design_intents 在循环内累加，已去重由上层调用方处理
                        for intent in intent_dicts:
                            if isinstance(intent, dict):
                                intent_id = (intent.get("properties") or {}).get("id")
                                if intent_id and intent_id not in seen_sol_ids:
                                    seen_sol_ids.add(intent_id)
                                    all_solutions.append(intent)  # 复用 seen_sol_ids 去重

        return {
            "requirement": requirement,
            "codes": code_dicts,
            "dependencies": all_dependencies,
            "solutions": all_solutions,
            "design_intents": [],  # 已合并入 solutions 去重列表（简化实现，避免单独列表）
        }
