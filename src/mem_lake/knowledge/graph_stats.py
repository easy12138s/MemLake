"""图统计与规则建边服务（ENH-01 能力内核）。

职责：
- get_graph_stats：节点/边按 type、system 维度聚合（经 AGE Cypher，遵循 age_store
  的 _exec_cypher 参数化模式与注入防护——label/type 为 Cypher 函数返回值，system_id
  为参数过滤，无用户输入拼入 Cypher 语法片段）。
- get_graph_quality_report：图质量基线（孤儿节点/重复度/连通分量）。为规避 AGE
  顶层算法缺失，全量拉取节点与边后在 Python 端用并查集计算连通分量，确定性且可测。
- build_rule_edge_items：规则边生成器（首批硬编码规则 snippet_module_references）：
  按节点属性（CodeSnippet.responsibility 含 Requirement.module）匹配，产出 references
  边审批项；不直写，由调用方经 submit_batch 走审批。

本模块作为服务层（session + graph_store 显式注入），由 gateway/tools/graph_tools.py
薄封装为 MCP 工具。
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.knowledge.age_store import AGEGraphStore
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.search.filters import node_active_approved

# 首批硬编码规则名（白名单校验，防未知规则）
RULE_SNIPPET_MODULE_REFERENCES = "snippet_module_references"
ALLOWED_RULES: frozenset[str] = frozenset({RULE_SNIPPET_MODULE_REFERENCES})

# 规则边类型
REFERENCE_EDGE_TYPE = "references"


async def _aggregate(
    session: AsyncSession,
    graph_store: AGEGraphStore,
    cypher: str,
) -> dict[str, int]:
    """执行聚合 Cypher（RETURN {key, cnt} AS result），解析为 {key: count} 字典。"""
    rows = await graph_store._exec_cypher(session, cypher)
    result: dict[str, int] = {}
    for row in rows:
        parsed = graph_store._parse_agtype(row)
        if not isinstance(parsed, dict):
            continue
        key = parsed.get("key")
        cnt = parsed.get("cnt")
        if key is None or cnt is None:
            continue
        result[str(key)] = int(cnt)
    return result


async def get_graph_stats(
    session: AsyncSession,
    graph_store: AGEGraphStore,
) -> dict[str, Any]:
    """图统计：节点/边按 type、system 维度计数。

    返回结构：
    {
        "nodes_by_type": {label: count, ...},        # label(n)
        "nodes_by_system": {system_id: count, ...},  # n.system_id（排除空串=未挂 system）
        "edges_by_type": {edge_type: count, ...},    # type(r)
        "edges_by_system": {source_system: count, ...},  # 边源端点 a.system_id（排除空串）
    }
    """
    nodes_by_type = await _aggregate(
        session,
        graph_store,
        "MATCH (n) WITH label(n) AS key, count(*) AS cnt "
        "RETURN {key: key, cnt: cnt} AS result",
    )
    nodes_by_system = await _aggregate(
        session,
        graph_store,
        "MATCH (n) WHERE n.system_id <> '' "
        "WITH n.system_id AS key, count(*) AS cnt "
        "RETURN {key: key, cnt: cnt} AS result",
    )
    edges_by_type = await _aggregate(
        session,
        graph_store,
        "MATCH ()-[r]->() WITH type(r) AS key, count(*) AS cnt "
        "RETURN {key: key, cnt: cnt} AS result",
    )
    edges_by_system = await _aggregate(
        session,
        graph_store,
        "MATCH (a)-[r]->(b) WHERE a.system_id <> '' "
        "WITH a.system_id AS key, count(*) AS cnt "
        "RETURN {key: key, cnt: cnt} AS result",
    )
    return {
        "nodes_by_type": nodes_by_type,
        "nodes_by_system": nodes_by_system,
        "edges_by_type": edges_by_type,
        "edges_by_system": edges_by_system,
    }


async def _fetch_nodes(
    session: AsyncSession, graph_store: AGEGraphStore
) -> list[dict[str, Any]]:
    """拉取全量图节点投影 {id, label, title, system_id}。"""
    rows = await graph_store._exec_cypher(
        session,
        "MATCH (n) "
        "RETURN {id: n.id, label: label(n), title: n.title, system_id: n.system_id} AS result",
    )
    nodes: list[dict[str, Any]] = []
    for row in rows:
        parsed = graph_store._parse_agtype(row)
        if isinstance(parsed, dict):
            nodes.append(parsed)
    return nodes


async def _fetch_edges(
    session: AsyncSession, graph_store: AGEGraphStore
) -> list[tuple[str, str, str]]:
    """拉取全量有向边 {(from_id, to_id, edge_type)}。"""
    rows = await graph_store._exec_cypher(
        session,
        "MATCH (a)-[r]->(b) "
        "RETURN {from_id: a.id, to_id: b.id, edge_type: type(r)} AS result",
    )
    edges: list[tuple[str, str, str]] = []
    for row in rows:
        parsed = graph_store._parse_agtype(row)
        if isinstance(parsed, dict):
            fid = parsed.get("from_id")
            tid = parsed.get("to_id")
            etype = parsed.get("edge_type")
            if fid is not None and tid is not None and etype is not None:
                edges.append((str(fid), str(tid), str(etype)))
    return edges


def _connected_components(
    node_ids: list[str], edges: list[tuple[str, str, str]]
) -> int:
    """并查集统计连通分量数（无视图，遍历为无向）。"""
    parent: dict[str, str] = {nid: nid for nid in node_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for fid, tid, _etype in edges:
        if fid in parent and tid in parent:
            union(fid, tid)

    roots: set[str] = set()
    for nid in node_ids:
        roots.add(find(nid))
    return len(roots)


async def get_graph_quality_report(
    session: AsyncSession,
    graph_store: AGEGraphStore,
) -> dict[str, Any]:
    """图质量基线报告。

    返回结构：
    {
        "total_nodes": int,   # 节点总数
        "total_edges": int,   # 边总数（有向边条数）
        "orphan_nodes": int,  # 孤儿节点数（连边数为 0）
        "duplicate_groups_count": int,   # 重复分组数（同 label+title 出现 >1）
        "duplicate_node_count": int,     # 落入重复分组的节点总数
        "connected_components": int,     # 连通分量数
    }
    """
    nodes = await _fetch_nodes(session, graph_store)
    edges = await _fetch_edges(session, graph_store)

    node_ids = [str(n.get("id", "")) for n in nodes if n.get("id")]

    # 孤立节点：未作为任何有向边端点<from|to>出现
    endpoint_ids: set[str] = {e[0] for e in edges} | {e[1] for e in edges}
    orphan_nodes = sum(1 for nid in node_ids if nid not in endpoint_ids)

    # 重复度：同 (label, title) 分组出现 >1 的节点数
    title_groups: dict[tuple[str, str], int] = {}
    for n in nodes:
        key = (str(n.get("label", "")), str(n.get("title", "")))
        title_groups[key] = title_groups.get(key, 0) + 1
    duplicate_groups = {k: v for k, v in title_groups.items() if v > 1}
    duplicate_node_count = sum(duplicate_groups.values())

    components = _connected_components(node_ids, edges)

    return {
        "total_nodes": len(node_ids),
        "total_edges": len(edges),
        "orphan_nodes": orphan_nodes,
        "duplicate_groups_count": len(duplicate_groups),
        "duplicate_node_count": duplicate_node_count,
        "connected_components": components,
    }


def _norm(text: str | None) -> str:
    """归一化匹配文本：小写并去除首尾空白。"""
    return (text or "").strip().lower()


async def _load_project_nodes(
    session: AsyncSession, node_type: str, project_id: uuid.UUID | None
) -> list[KnowledgeNode]:
    """加载指定类型的 approved 且未删除节点；project_id 非空时限定项目域。"""
    stmt = (
        select(KnowledgeNode)
        .where(KnowledgeNode.type == node_type)
        .where(*node_active_approved())
    )
    if project_id is not None:
        stmt = stmt.where(KnowledgeNode.project_id == project_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _existing_reference_edges(
    session: AsyncSession, graph_store: AGEGraphStore
) -> set[tuple[str, str]]:
    """返回图中已有 references 有向边集合 {(from_id, to_id)}（供规则建边幂等跳过）。"""
    edges = await _fetch_edges(session, graph_store)
    return {(f, t) for f, t, etype in edges if etype == REFERENCE_EDGE_TYPE}


def _match_snippet_module_references(
    snippets: list[KnowledgeNode],
    requirements: list[KnowledgeNode],
) -> list[tuple[KnowledgeNode, KnowledgeNode]]:
    """规则 snippet_module_references：匹配 (CodeSnippet, Requirement)。

    判定（按 properties 属性匹配）：把 CodeSnippet 的 responsibility 文本
    （归一化后）与 Requirement 的 module 属性（归一化后，非空）做子串包含判定——
    当 snippet 声明负责的文本中包含某需求的 module 时，视为 snippet 引用了该需求。
    同项目域内（snippet.project_id == requirement.project_id）才纳入匹配。
    返回匹配对列表。
    """
    matches: list[tuple[KnowledgeNode, KnowledgeNode]] = []
    for sn in snippets:
        resp = _norm(sn.properties.get("responsibility") if sn.properties else None)
        if not resp:
            continue
        for req in requirements:
            mod = _norm(req.properties.get("module") if req.properties else None)
            if not mod:
                continue
            if req.project_id != sn.project_id:
                continue
            if mod in resp:
                matches.append((sn, req))
    return matches


async def build_rule_edge_items(
    session: AsyncSession,
    graph_store: AGEGraphStore,
    *,
    rule: str = RULE_SNIPPET_MODULE_REFERENCES,
    project_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按规则生成 references 边审批项（不直写，返回审批项由调用方 submit_batch）。

    参数：
        rule: 硬编码规则名（当前仅 snippet_module_references）
        project_id: 限定项目域；None=全部（本规则内仍按同项目配对）

    返回：
        (edge_items, matched_detail)
        edge_items: 待提交的 edge+create 审批项（from_ref/to_ref=节点 UUID，走审批流）
        matched_detail: [{source, source_title, target, target_title, module}] 供出参展示

    幂等：跳过图中已存在的 references 边（(from_id, to_id) 已在图则不重复产出）。
    """
    if rule not in ALLOWED_RULES:
        raise ValueError(f"未知规则: {rule}，合法规则: {sorted(ALLOWED_RULES)}")

    snippets = await _load_project_nodes(session, "CodeSnippet", project_id)
    requirements = await _load_project_nodes(session, "Requirement", project_id)
    matches = _match_snippet_module_references(snippets, requirements)

    existing = await _existing_reference_edges(session, graph_store)

    edge_items: list[dict[str, Any]] = []
    matched_detail: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sn, req in matches:
        from_key = str(sn.id)
        to_key = str(req.id)
        if (from_key, to_key) in seen or (from_key, to_key) in existing:
            continue
        seen.add((from_key, to_key))
        edge_items.append(
            {
                "item_type": "edge",
                "action": "create",
                "entity_type": REFERENCE_EDGE_TYPE,
                "payload": {
                    "from_ref": from_key,
                    "to_ref": to_key,
                    "properties": {},
                },
            }
        )
        module = req.properties.get("module") if req.properties else None
        matched_detail.append(
            {
                "source": from_key,
                "source_title": sn.title,
                "target": to_key,
                "target_title": req.title,
                "module": str(module) if module is not None else "",
            }
        )
    return edge_items, matched_detail
