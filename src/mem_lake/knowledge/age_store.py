"""AGEGraphStore 实现：基于 Apache AGE 的 Cypher 操作与边 CRUD。

技术决策（Phase 1 探索 + 网络搜索 AGE 官方文档 + 实测验证）：
- apache-age-python 驱动依赖 psycopg2 同步 cursor，与项目 psycopg3 async 不兼容，改用原生 SQL
- AGE cypher() 第三参数（params）只能与 Prepared Statements 配合使用（AGE 官方文档明确：
  https://age.apache.org/age-manual/master/intro/cypher.html "The parameter map can only
  be used with Prepared Statements. An error will be thrown otherwise."）
- PREPARE/EXECUTE 中的 $1（PG positional param）与 psycopg3 wire protocol 的 $1 冲突，
  导致 IndeterminateDatatype 错误。解决方案：graph_name 从 config 读取（非用户输入），
  安全地嵌入为字符串字面量，避免 psycopg3 参数化与 PREPARE $1 冲突。
- EXECUTE 的 agtype map 通过随机 dollar-quote 标签包裹（json.dumps 生成，随机标签防碰撞）
- label/edge_type 通过 schema 白名单校验后字符串拼接（Cypher 语法部分，非值）
"""

import json
import re
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.config import get_settings
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.schema import validate_edge_type, validate_node_type

# agtype 返回值类型后缀（::vertex / ::edge / ::path）
# AGE 嵌套结构（path、subgraph）内含多个后缀，需全局移除（非仅末尾）。
# \b 边界匹配避免误伤 "vertexx" 等子串；:: 不出现在 JSON 语法部分，
# 测试数据中的字符串值不含 ::vertex/::edge/::path 字面量。
_AGE_TYPE_SUFFIX_RE = re.compile(r"::(vertex|edge|path)\b")

# 边属性键白名单正则：仅允许字母/数字/下划线，首字符为字母或下划线
# 用于动态构建 SET 子句时防止 Cypher 注入（属性键拼入 Cypher 语法部分）
_EDGE_PROP_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class AGEGraphStore(GraphStore):
    """基于 Apache AGE 的 GraphStore 实现。

    通过原生 SQL 调用 ag_catalog.cypher 执行 Cypher。
    properties 值通过 PREPARE/EXECUTE 参数化，label/edge_type 通过白名单校验。
    """

    def __init__(self, graph_name: str) -> None:
        self._graph_name = graph_name

    async def _ensure_age_session(self, session: AsyncSession) -> None:
        """确保会话已加载 AGE 扩展并设置 search_path（幂等）。"""
        await session.execute(text("LOAD 'age'"))
        await session.execute(text("SET search_path = ag_catalog, public"))

    async def _exec_cypher(
        self,
        session: AsyncSession,
        cypher_stmt: str,
        params: dict | None = None,
    ) -> list[Any]:
        """执行 Cypher 语句并返回结果列表。

        - params 为 None 时：直接执行 cypher()（无第三参数）
        - params 非 None 时：用 PREPARE/EXECUTE/DEALLOCATE 参数化
        - cypher_stmt 用 $age$ ... $age$ dollar-quote 包裹

        技术要点（AGE 官方文档 + psycopg3 兼容性）：
        - AGE cypher() 第三参数只能用于 Prepared Statements，不能直接传字面量
        - PREPARE 的 $1（PG positional param）与 psycopg3 wire protocol 的 $1 冲突，
          因此 graph_name 嵌入为字符串字面量（来自 config，非用户输入，安全）
        - EXECUTE 的 agtype map 用随机 dollar-quote 标签包裹，避免内容碰撞
        """
        await self._ensure_age_session(session)
        dollar_cypher = f"$age$ {cypher_stmt} $age$"
        # graph_name 从 config 读取（非用户输入），安全嵌入为字面量
        graph_literal = f"'{self._graph_name}'"

        if params is None:
            sql = text(
                f"SELECT * FROM cypher({graph_literal}, {dollar_cypher}) AS (result agtype)"
            )
            result = await session.execute(sql)
            return [row[0] for row in result]

        # 参数化：PREPARE/EXECUTE/DEALLOCATE
        # graph_name 嵌入为字面量，避免 psycopg3 参数 $N 与 PREPARE 的 $1 冲突
        stmt_name = f"age_query_{uuid.uuid4().hex[:8]}"
        params_json = json.dumps(params)

        prepare_sql = text(
            f"PREPARE {stmt_name}(agtype) AS "
            f"SELECT * FROM cypher({graph_literal}, {dollar_cypher}, $1) AS (result agtype)"
        )
        await session.execute(prepare_sql)

        try:
            # EXECUTE 传 agtype map：用随机 dollar-quote 标签包裹（json.dumps 保证合法 JSON，
            # 随机标签防止 params 内容含 dollar-quote 导致碰撞）
            tag = f"p{uuid.uuid4().hex[:8]}"
            execute_sql = text(f"EXECUTE {stmt_name}(${tag}${params_json}${tag}$)")
            result = await session.execute(execute_sql)
            return [row[0] for row in result]
        finally:
            await session.execute(text(f"DEALLOCATE {stmt_name}"))

    def _parse_agtype(self, value: Any) -> dict | list | None:
        """解析 agtype 字符串为 Python 对象。

        AGE 返回 '{"id":..., "label":..., "properties":{...}}::vertex' 形式的字符串。
        嵌套结构（path、collect 的 vertex/edge 列表）内含多个 ::vertex/::edge/::path
        类型后缀，统一移除后 json.loads 解析。解析失败返回 None（不抛异常，由调用方判空）。
        """
        if value is None:
            return None
        s = str(value)
        # 全局移除所有 ::vertex/::edge/::path 类型后缀（含嵌套结构内的多个后缀）
        s = _AGE_TYPE_SUFFIX_RE.sub("", s)
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None

    async def add_node(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
        label: str,
        properties: dict,
    ) -> None:
        """添加节点。label 经白名单校验，properties 通过 PREPARE 参数化。

        图节点仅引用 knowledge_node.id（不重复存储完整 properties），
        携带 id/project_id/title 用于图查询过滤与展示。label 校验用
        validate_node_type（仅校验类型，不校验必填字段，因图节点不存完整属性）。
        """
        validate_node_type(label)
        cypher = (
            f"CREATE (n:{label} {{id: $node_id, project_id: $project_id, title: $title}}) "
            f"RETURN n"
        )
        params = {
            "node_id": str(node_id),
            "project_id": str(properties.get("project_id", "")),
            "title": str(properties.get("title", "")),
        }
        await self._exec_cypher(session, cypher, params)

    async def add_edge(
        self,
        session: AsyncSession,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_type: str,
        properties: dict,
    ) -> None:
        """添加边。edge_type 经白名单校验，properties 逐属性 SET。

        AGE SET 子句仅支持 SET r.prop = $value 逐属性设置（不支持 SET r = $map，
        参见 AGE 官方文档 https://age.apache.org/age-manual/master/clauses/set.html）。
        因此将 properties dict 展开为独立 Cypher 参数，动态构建 SET 子句。
        属性键经白名单正则校验（仅字母/数字/下划线），防止 Cypher 注入。
        """
        validate_edge_type(edge_type)

        # 校验属性键安全性（拼入 Cypher 语法部分，非参数值）
        for key in properties:
            if not _EDGE_PROP_KEY_RE.match(key):
                raise ValueError(
                    f"非法边属性键: {key}，仅允许字母/数字/下划线且首字符为字母或下划线"
                )

        # 动态构建 SET 子句：r.key1 = $key1, r.key2 = $key2
        set_clauses = ", ".join(f"r.{k} = ${k}" for k in properties) if properties else ""

        cypher = (
            f"MATCH (a {{id: $from_id}}), (b {{id: $to_id}}) "
            f"CREATE (a)-[r:{edge_type}]->(b)"
        )
        if set_clauses:
            cypher += f" SET {set_clauses}"
        # 不使用 RETURN：AGE 终端 SET 子句不返回结果，匹配 (result agtype) 单列定义
        # （AGE 文档：terminal clause requires single column definition, returns 0 rows）

        # 展开属性为独立参数（与 SET 子句中的 $key 对应）
        params: dict[str, Any] = {
            "from_id": str(from_id),
            "to_id": str(to_id),
        }
        for k, v in properties.items():
            params[k] = v

        await self._exec_cypher(session, cypher, params)

    async def neighbors(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
        edge_type: str | None = None,
        depth: int = 1,
    ) -> list[dict]:
        """邻居遍历。edge_type=None 时不限类型，depth 控制遍历深度。

        边类型过滤实现说明（基于 AGE 官方文档 + SQLAlchemy 兼容性 + AGE v1.7.0 实测）：
        - AGE 官方语法 [:TYPE*1..N] 可过滤变长边类型（AGE MATCH 文档示例
          MATCH p = (actor)-[:ACTED_IN*2]-(co_actor)），但 SQLAlchemy text()
          将 [:TYPE*1..N] 中的 :TYPE 误解析为绑定参数，报错
          "A value is required for bind parameter 'TYPE'"
        - AGE v1.7.0 不支持 openCypher 的 ALL(x IN list WHERE ...) 谓词
        - depth=1 方案：用单边模式 -[r]- 配合 WHERE type(r) = 'TYPE'
          （AGE MATCH 文档：MATCH (:Person)-[r]->(movie) RETURN type(r)）
          r 为单条边，type(r) 返回其类型，实测可用
        - depth>1 方案：取变长路径 + 边列表，Python 端按 edge_type 过滤
          （AGE v1.7.0 限制，无法在 Cypher 内完成全路径边类型校验）
        - edge_type 已由 validate_edge_type 白名单校验（12 种合法类型），
          安全拼入 Cypher 字符串字面量（非用户输入）
        """
        if edge_type is not None:
            validate_edge_type(edge_type)
            if depth == 1:
                # depth=1：单边模式，r 为单条边，WHERE type(r) = 'TYPE' 过滤
                cypher = (
                    f"MATCH (n {{id: $node_id}})-[r]-(m) "
                    f"WHERE type(r) = '{edge_type}' "
                    f"RETURN DISTINCT m"
                )
                params = {"node_id": str(node_id)}
                rows = await self._exec_cypher(session, cypher, params)
                return [p for p in (self._parse_agtype(r) for r in rows) if p is not None]
            else:
                # depth>1：AGE v1.7.0 不支持 ALL() 谓词，取路径后在 Python 端过滤
                cypher = (
                    f"MATCH (n {{id: $node_id}})-[r*1..{depth}]-(m) "
                    f"RETURN {{node: m, edges: r}} AS result"
                )
                params = {"node_id": str(node_id)}
                rows = await self._exec_cypher(session, cypher, params)
                result: list[dict] = []
                seen: set[str] = set()
                for row in rows:
                    parsed = self._parse_agtype(row)
                    if not isinstance(parsed, dict):
                        continue
                    edges = parsed.get("edges") or []
                    # 检查路径中所有边类型都匹配（边 dict 的 label 字段即边类型）
                    if not all(
                        isinstance(e, dict) and e.get("label") == edge_type
                        for e in edges
                    ):
                        continue
                    node = parsed.get("node")
                    if not isinstance(node, dict):
                        continue
                    nid = node.get("properties", {}).get("id")
                    if nid and nid not in seen:
                        seen.add(nid)
                        result.append(node)
                return result

        # 无 edge_type 过滤：变长模式直接返回
        cypher = (
            f"MATCH (n {{id: $node_id}})-[*1..{depth}]-(m) "
            f"RETURN DISTINCT m"
        )
        params = {"node_id": str(node_id)}
        rows = await self._exec_cypher(session, cypher, params)
        return [p for p in (self._parse_agtype(r) for r in rows) if p is not None]

    async def find_path(
        self,
        session: AsyncSession,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        max_depth: int = 5,
    ) -> list[list[dict]]:
        """路径查询。返回路径列表，每条路径为节点 dict 列表。无路径返回空。

        注：AGE v1.7.0 不支持 shortestPath() 函数（Neo4j 特有语法）。
        改用变长路径匹配 + LIMIT 1 返回首条路径。AGE v1.8.0 的 shortest_path
        函数在 PG17 上尚不可用，后续版本升级后可切换。

        AGE path 返回格式说明（实测验证）：
        - RETURN p 返回扁平 list：[vertex, edge, vertex, edge, vertex]::path
          不便直接区分节点与边
        - 改用 RETURN {nodes: nodes(p), edges: relationships(p)} 返回结构化 map
          nodes(p) 和 relationships(p) 是 AGE 内建函数，分别提取路径节点与边
          实测返回 {"nodes": [...::vertex], "edges": [...::edge]} 由 _parse_agtype 解析
        """
        cypher = (
            f"MATCH p=(a {{id: $from_id}})-[*1..{max_depth}]-(b {{id: $to_id}}) "
            f"RETURN {{nodes: nodes(p), edges: relationships(p)}} AS result LIMIT 1"
        )
        params = {"from_id": str(from_id), "to_id": str(to_id)}
        rows = await self._exec_cypher(session, cypher, params)
        paths: list[list[dict]] = []
        for row in rows:
            parsed = self._parse_agtype(row)
            if not isinstance(parsed, dict):
                continue
            nodes = parsed.get("nodes")
            if isinstance(nodes, list) and nodes:
                paths.append(nodes)
        return paths

    async def match_pattern(
        self,
        session: AsyncSession,
        pattern: str,
        params: dict | None = None,
    ) -> list[dict]:
        """图模式匹配。pattern 为 Cypher MATCH 子句（受信任调用方构造）。"""
        rows = await self._exec_cypher(session, pattern, params)
        result: list[dict] = []
        for row in rows:
            parsed = self._parse_agtype(row)
            if parsed is not None:
                result.append(parsed)
        return result

    async def subgraph(
        self,
        session: AsyncSession,
        node_ids: list[uuid.UUID],
    ) -> dict[str, Any]:
        """子图提取。返回 {"nodes": [...], "edges": [...]}。

        将 nodes 与 edges 收集为单个 agtype map 返回，匹配 (result agtype) 单列定义。
        OPTIONAL MATCH 保证孤立节点也被收集（r 为 null 时 collect 自动排除 null）。
        嵌套 vertex/edge 元素含 ::vertex/::edge 后缀，由 _parse_agtype 统一移除后解析。
        """
        cypher = (
            "MATCH (n) WHERE n.id IN $ids "
            "WITH n "
            "OPTIONAL MATCH (n)-[r]-(m) "
            "RETURN {nodes: collect(DISTINCT n), edges: collect(DISTINCT r)} AS result"
        )
        params = {"ids": [str(nid) for nid in node_ids]}
        rows = await self._exec_cypher(session, cypher, params)
        if not rows:
            return {"nodes": [], "edges": []}
        parsed = self._parse_agtype(rows[0])
        if not isinstance(parsed, dict):
            return {"nodes": [], "edges": []}
        return {
            "nodes": parsed.get("nodes", []) or [],
            "edges": parsed.get("edges", []) or [],
        }

    async def delete_node(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
    ) -> None:
        """删除节点及其关联边（DETACH DELETE）。幂等。"""
        cypher = "MATCH (n {id: $node_id}) DETACH DELETE n"
        params = {"node_id": str(node_id)}
        await self._exec_cypher(session, cypher, params)


@lru_cache
def get_graph_store() -> AGEGraphStore:
    """返回 AGEGraphStore 进程单例。graph_name 从 config 读取。"""
    settings = get_settings()
    return AGEGraphStore(graph_name=settings.AGE_GRAPH_NAME)
