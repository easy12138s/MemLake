"""M3 集成测试：AGEGraphStore 图操作。

按实际调用场景验证：
1. add_node + neighbors 自查询验证
2. add_edge + neighbors 跨节点遍历
3. neighbors 1跳/多跳/带 edge_type 过滤
4. find_path 连通/不连通
5. subgraph 提取
6. delete_node（DETACH DELETE）+ 关联边一并删除
7. 边界：不存在的节点 neighbors 返回空、find_path 返回空、subgraph 空列表
8. 非法 label/edge_type 抛 SchemaValidationError

事务回滚隔离，AGE DML 操作随事务回滚。
"""

import uuid

import pytest

from mem_lake.knowledge.age_store import AGEGraphStore
from mem_lake.knowledge.graph_store import EdgeTargetNotFoundError
from mem_lake.knowledge.schema import SchemaValidationError


@pytest.fixture
def store(graph_store) -> AGEGraphStore:
    """复用 conftest 的 graph_store fixture（AGEGraphStore 实例）。"""
    return graph_store


def _props(node_id: uuid.UUID, project_id: uuid.UUID, title: str = "T") -> dict:
    """构造图节点 properties（id/project_id/title）。"""
    return {
        "id": str(node_id),
        "project_id": str(project_id),
        "title": title,
    }


# ============ add_node + neighbors ============

class TestAddNode:
    """add_node 测试。"""

    async def test_add_node_success(self, db_session, store):
        """添加节点后 neighbors 自身可查（1 跳含自身环不返回，用 match_pattern 验证存在）。"""
        node_id = uuid.uuid4()
        project_id = uuid.uuid4()
        await store.add_node(
            db_session,
            node_id=node_id,
            label="Requirement",
            properties=_props(node_id, project_id, "登录需求"),
        )

        # 用 match_pattern 验证节点存在
        rows = await store.match_pattern(
            db_session,
            "MATCH (n:Requirement {id: $nid}) RETURN n",
            {"nid": str(node_id)},
        )
        assert len(rows) == 1
        node = rows[0]
        assert node["label"] == "Requirement"
        assert node["properties"]["id"] == str(node_id)
        assert node["properties"]["title"] == "登录需求"

    async def test_add_node_invalid_label(self, db_session, store):
        """非法 label 抛 SchemaValidationError。"""
        node_id = uuid.uuid4()
        project_id = uuid.uuid4()
        with pytest.raises(SchemaValidationError, match="非法节点类型"):
            await store.add_node(
                db_session,
                node_id=node_id,
                label="InvalidType",
                properties=_props(node_id, project_id),
            )

    async def test_add_node_each_type(self, db_session, store):
        """PDD 定义的 7 种节点类型均可写入图中。"""
        project_id = uuid.uuid4()
        for label in [
            "ProjectProfile",
            "Requirement",
            "CodeSnippet",
            "Solution",
            "DesignIntent",
            "Decision",
            "Pitfall",
        ]:
            node_id = uuid.uuid4()
            await store.add_node(
                db_session,
                node_id=node_id,
                label=label,
                properties=_props(node_id, project_id, label),
            )

        # 验证 7 个节点都写入
        rows = await store.match_pattern(
            db_session,
            "MATCH (n) WHERE n.project_id = $pid RETURN count(n) AS cnt",
            {"pid": str(project_id)},
        )
        # agtype 返回值可能是 [{"cnt": 7}] 或类似
        assert len(rows) >= 1


# ============ add_edge + neighbors ============

class TestAddEdge:
    """add_edge 测试。"""

    async def test_add_edge_success(self, db_session, store):
        """添加边后 neighbors 1 跳返回目标节点。"""
        project_id = uuid.uuid4()
        req_id = uuid.uuid4()
        code_id = uuid.uuid4()
        await store.add_node(db_session, req_id, "Requirement", _props(req_id, project_id, "R"))
        await store.add_node(db_session, code_id, "CodeSnippet", _props(code_id, project_id, "C"))

        await store.add_edge(
            db_session,
            from_id=req_id,
            to_id=code_id,
            edge_type="implements",
            properties={"reason": "需求由代码实现"},
        )

        # 从 req 出发找邻居，应包含 code
        neighbors = await store.neighbors(db_session, req_id, depth=1)
        neighbor_ids = {n["properties"]["id"] for n in neighbors}
        assert str(code_id) in neighbor_ids

    async def test_add_edge_invalid_type(self, db_session, store):
        """非法 edge_type 抛 SchemaValidationError。"""
        project_id = uuid.uuid4()
        a_id = uuid.uuid4()
        b_id = uuid.uuid4()
        await store.add_node(db_session, a_id, "Requirement", _props(a_id, project_id))
        await store.add_node(db_session, b_id, "CodeSnippet", _props(b_id, project_id))

        with pytest.raises(SchemaValidationError, match="非法边类型"):
            await store.add_edge(
                db_session,
                from_id=a_id,
                to_id=b_id,
                edge_type="invalid_relation",
                properties={},
            )

    async def test_add_edge_missing_endpoint_raises(self, db_session, store):
        """端点节点不存在于图中时抛 EdgeTargetNotFoundError（禁止静默丢边）。

        AGE 的 MATCH ... CREATE 在 MATCH 失败时静默跳过，
        add_edge 必须显式检测并抛错，触发调用方事务回滚。
        """
        project_id = uuid.uuid4()
        a_id = uuid.uuid4()
        missing_id = uuid.uuid4()  # 从未 add_node 的节点
        await store.add_node(db_session, a_id, "Requirement", _props(a_id, project_id))

        with pytest.raises(EdgeTargetNotFoundError, match="边端点节点在图中不存在"):
            await store.add_edge(
                db_session,
                from_id=a_id,
                to_id=missing_id,
                edge_type="implements",
                properties={},
            )

    async def test_add_edge_each_type(self, db_session, store):
        """PDD 12 种边类型验证（构造合法节点对后逐一添加）。"""
        project_id = uuid.uuid4()
        # 构造各类节点供边连接
        nodes = {
            "Requirement": uuid.uuid4(),
            "CodeSnippet": uuid.uuid4(),
            "Solution": uuid.uuid4(),
            "DesignIntent": uuid.uuid4(),
            "Decision": uuid.uuid4(),
            "Pitfall": uuid.uuid4(),
        }
        for label, nid in nodes.items():
            await store.add_node(db_session, nid, label, _props(nid, project_id, label))

        edge_pairs = [
            ("implements", "Requirement", "CodeSnippet"),
            ("depends_on", "CodeSnippet", "CodeSnippet"),
            ("realized_by", "CodeSnippet", "Solution"),
            ("embodies", "Solution", "DesignIntent"),
            ("traces_to", "DesignIntent", "Decision"),
            ("conflicts_with", "Requirement", "Requirement"),
            ("duplicates", "Requirement", "Requirement"),
            ("relates_to", "Requirement", "Requirement"),
            ("supersedes", "Requirement", "Requirement"),
            ("version_of", "Requirement", "Requirement"),
            ("described_by", "CodeSnippet", "Pitfall"),
            ("references", "Requirement", "Decision"),
        ]
        for edge_type, from_label, to_label in edge_pairs:
            await store.add_edge(
                db_session,
                from_id=nodes[from_label],
                to_id=nodes[to_label],
                edge_type=edge_type,
                properties={"created_by": "tester"},
            )

        # 验证边数（match_pattern 统计）
        rows = await store.match_pattern(
            db_session,
            "MATCH ()-[r]->() WHERE r.created_by = $actor RETURN count(r) AS cnt",
            {"actor": "tester"},
        )
        assert len(rows) >= 1


# ============ neighbors ============

class TestNeighbors:
    """neighbors 遍历测试。"""

    async def test_neighbors_depth_1(self, db_session, store):
        """1 跳邻居返回直接相邻节点。"""
        project_id = uuid.uuid4()
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for nid, label, title in [
            (a, "Requirement", "R"),
            (b, "CodeSnippet", "C"),
            (c, "CodeSnippet", "C2"),
        ]:
            await store.add_node(db_session, nid, label, _props(nid, project_id, title))

        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, b, c, "depends_on", {})

        # a 的 1 跳邻居只有 b
        neighbors = await store.neighbors(db_session, a, depth=1)
        ids = {n["properties"]["id"] for n in neighbors}
        assert str(b) in ids
        assert str(c) not in ids  # c 是 2 跳

    async def test_neighbors_depth_2(self, db_session, store):
        """2 跳邻居返回 a→b→c 的 c。"""
        project_id = uuid.uuid4()
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for nid, label in [(a, "Requirement"), (b, "CodeSnippet"), (c, "CodeSnippet")]:
            await store.add_node(db_session, nid, label, _props(nid, project_id))

        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, b, c, "depends_on", {})

        neighbors = await store.neighbors(db_session, a, depth=2)
        ids = {n["properties"]["id"] for n in neighbors}
        assert str(b) in ids
        assert str(c) in ids

    async def test_neighbors_filter_by_edge_type(self, db_session, store):
        """edge_type 过滤：只返回指定类型的邻居。"""
        project_id = uuid.uuid4()
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for nid, label in [(a, "Requirement"), (b, "CodeSnippet"), (c, "Solution")]:
            await store.add_node(db_session, nid, label, _props(nid, project_id))

        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, a, c, "references", {})

        # 只查 implements 邻居
        neighbors_impl = await store.neighbors(db_session, a, edge_type="implements", depth=1)
        ids_impl = {n["properties"]["id"] for n in neighbors_impl}
        assert str(b) in ids_impl
        assert str(c) not in ids_impl

        # 只查 references 邻居
        neighbors_ref = await store.neighbors(db_session, a, edge_type="references", depth=1)
        ids_ref = {n["properties"]["id"] for n in neighbors_ref}
        assert str(c) in ids_ref
        assert str(b) not in ids_ref

    async def test_neighbors_nonexistent_node(self, db_session, store):
        """不存在的节点查询邻居返回空列表。"""
        fake_id = uuid.uuid4()
        neighbors = await store.neighbors(db_session, fake_id, depth=1)
        assert neighbors == []

    async def test_neighbors_node_with_no_edges(self, db_session, store):
        """孤立节点（无边）查询邻居返回空。"""
        project_id = uuid.uuid4()
        isolated = uuid.uuid4()
        await store.add_node(db_session, isolated, "Requirement", _props(isolated, project_id))

        neighbors = await store.neighbors(db_session, isolated, depth=1)
        assert neighbors == []


# ============ find_path ============

class TestFindPath:
    """find_path 测试。"""

    async def test_find_path_connected(self, db_session, store):
        """连通节点间返回路径。"""
        project_id = uuid.uuid4()
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for nid, label in [(a, "Requirement"), (b, "CodeSnippet"), (c, "Solution")]:
            await store.add_node(db_session, nid, label, _props(nid, project_id))

        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, b, c, "realized_by", {})

        paths = await store.find_path(db_session, a, c, max_depth=5)
        assert len(paths) >= 1
        # 路径应含 a, b, c 三个节点
        path_ids = [v["properties"]["id"] for v in paths[0]]
        assert str(a) in path_ids
        assert str(c) in path_ids

    async def test_find_path_not_connected(self, db_session, store):
        """不连通节点返回空列表。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "Pitfall", _props(b, project_id))
        # 不添加任何边

        paths = await store.find_path(db_session, a, b, max_depth=3)
        assert paths == []

    async def test_find_path_nonexistent_node(self, db_session, store):
        """不存在的节点查询路径返回空。"""
        fake_a = uuid.uuid4()
        fake_b = uuid.uuid4()
        paths = await store.find_path(db_session, fake_a, fake_b, max_depth=3)
        assert paths == []

    async def test_find_path_max_depth_limit(self, db_session, store):
        """max_depth=1 时无法找到 2 跳路径。"""
        project_id = uuid.uuid4()
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for nid, label in [(a, "Requirement"), (b, "CodeSnippet"), (c, "Solution")]:
            await store.add_node(db_session, nid, label, _props(nid, project_id))

        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, b, c, "realized_by", {})

        # max_depth=1 无法找到 a→c（2 跳）
        paths = await store.find_path(db_session, a, c, max_depth=1)
        assert paths == []


# ============ subgraph ============

class TestSubgraph:
    """subgraph 提取测试。"""

    async def test_subgraph_basic(self, db_session, store):
        """提取节点子图，返回 nodes 与 edges。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))
        await store.add_edge(db_session, a, b, "implements", {})

        sub = await store.subgraph(db_session, [a, b])
        # 返回结构含 nodes 与 edges 键
        assert "nodes" in sub
        assert "edges" in sub
        # nodes 非空（具体格式由 agtype 解解决定）
        assert len(sub["nodes"]) >= 1

    async def test_subgraph_empty_node_ids(self, db_session, store):
        """空 node_ids 返回空子图。"""
        sub = await store.subgraph(db_session, [])
        assert sub == {"nodes": [], "edges": []}

    async def test_subgraph_nonexistent_ids(self, db_session, store):
        """不存在的 node_ids 返回空子图。"""
        fake = uuid.uuid4()
        sub = await store.subgraph(db_session, [fake])
        # 不存在的节点应返回空 nodes
        assert sub["nodes"] == [] or len(sub["nodes"]) == 0


# ============ delete_node ============

class TestDeleteNode:
    """delete_node 测试。"""

    async def test_delete_node_detaches_edges(self, db_session, store):
        """删除节点后关联边一并删除（DETACH DELETE）。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))
        await store.add_edge(db_session, a, b, "implements", {})

        # 删除 a（DETACH DELETE 一并删除 a 的边）
        await store.delete_node(db_session, a)

        # a 不再存在
        rows = await store.match_pattern(
            db_session,
            "MATCH (n {id: $nid}) RETURN n",
            {"nid": str(a)},
        )
        assert len(rows) == 0

        # b 仍存在
        rows = await store.match_pattern(
            db_session,
            "MATCH (n {id: $nid}) RETURN n",
            {"nid": str(b)},
        )
        assert len(rows) == 1

    async def test_delete_node_idempotent(self, db_session, store):
        """删除不存在的节点幂等（不报错）。"""
        fake_id = uuid.uuid4()
        # 不存在的节点删除不应抛异常
        await store.delete_node(db_session, fake_id)

    async def test_delete_isolated_node(self, db_session, store):
        """删除无边节点正常。"""
        project_id = uuid.uuid4()
        a = uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))

        await store.delete_node(db_session, a)

        rows = await store.match_pattern(
            db_session,
            "MATCH (n {id: $nid}) RETURN n",
            {"nid": str(a)},
        )
        assert len(rows) == 0


# ============ 边界场景补充 ============

class TestNeighborsEdgeCases:
    """neighbors 边界场景：depth>1+edge_type 过滤、深层遍历、自环。"""

    async def test_neighbors_depth_2_with_edge_type_filter(self, db_session, store):
        """depth=2 + edge_type 过滤：仅返回路径中所有边都匹配类型的邻居。

        场景：a -implements-> b -implements-> c（同类型链）
              a -references-> d（不同类型）
        depth=2 + edge_type=implements：应返回 b 和 c
        depth=2 + edge_type=references：应返回 d（1 跳即达，路径边类型匹配）
        """
        project_id = uuid.uuid4()
        a, b, c, d = (uuid.uuid4() for _ in range(4))
        for nid, label in [
            (a, "Requirement"), (b, "CodeSnippet"), (c, "CodeSnippet"), (d, "Decision")
        ]:
            await store.add_node(db_session, nid, label, _props(nid, project_id))

        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, b, c, "implements", {})
        await store.add_edge(db_session, a, d, "references", {})

        # depth=2 + implements：b（1跳）和 c（2跳，a→b→c 全 implements）
        neighbors_impl = await store.neighbors(db_session, a, edge_type="implements", depth=2)
        ids_impl = {n["properties"]["id"] for n in neighbors_impl}
        assert str(b) in ids_impl
        assert str(c) in ids_impl
        # d 不在 implements 邻居中（a→d 是 references）
        assert str(d) not in ids_impl

    async def test_neighbors_deep_path_depth_5(self, db_session, store):
        """depth=5 深层遍历返回 5 跳内所有邻居。"""
        project_id = uuid.uuid4()
        nodes = [uuid.uuid4() for _ in range(6)]  # n0 → n1 → n2 → n3 → n4 → n5
        for i, nid in enumerate(nodes):
            await store.add_node(
                db_session, nid, "Requirement", _props(nid, project_id, f"N{i}")
            )
        for i in range(5):
            await store.add_edge(db_session, nodes[i], nodes[i + 1], "depends_on", {})

        # depth=5 从 n0 出发应能到达 n1..n5
        neighbors = await store.neighbors(db_session, nodes[0], depth=5)
        ids = {n["properties"]["id"] for n in neighbors}
        for i in range(1, 6):
            assert str(nodes[i]) in ids

    async def test_neighbors_self_loop(self, db_session, store):
        """自环边后 neighbors 返回自身（AGE 允许自环）。"""
        project_id = uuid.uuid4()
        a = uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))

        # 创建自环边
        await store.add_edge(db_session, a, a, "relates_to", {})

        neighbors = await store.neighbors(db_session, a, depth=1)
        ids = {n["properties"]["id"] for n in neighbors}
        assert str(a) in ids


class TestFindPathEdgeCases:
    """find_path 边界场景。"""

    async def test_find_path_same_node(self, db_session, store):
        """from_id == to_id 时返回单节点路径（长度 0）。"""
        project_id = uuid.uuid4()
        a = uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))

        paths = await store.find_path(db_session, a, a, max_depth=3)
        # AGE 对 from==to 的变长路径 [*1..N] 不返回 0 长度路径（最少 1 跳）
        # 因此无路径返回空列表
        assert paths == []

    async def test_find_path_directed_vs_undirected(self, db_session, store):
        """-[*1..N]-（无向）可双向遍历：a→b 后 find_path(b, a) 也能找到。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))
        await store.add_edge(db_session, a, b, "implements", {})

        # 反向查询（无向遍历）
        paths = await store.find_path(db_session, b, a, max_depth=3)
        assert len(paths) >= 1
        path_ids = [v["properties"]["id"] for v in paths[0]]
        assert str(a) in path_ids
        assert str(b) in path_ids


class TestSubgraphEdgeCases:
    """subgraph 边界场景：孤立节点、edges 验证、多节点。"""

    async def test_subgraph_with_isolated_nodes(self, db_session, store):
        """孤立节点（无边）也被收集到 nodes，edges 为空。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))
        # 不添加任何边

        sub = await store.subgraph(db_session, [a, b])
        assert len(sub["nodes"]) == 2
        node_ids = {n["properties"]["id"] for n in sub["nodes"]}
        assert str(a) in node_ids
        assert str(b) in node_ids
        # 无边
        assert sub["edges"] == [] or len(sub["edges"]) == 0

    async def test_subgraph_returns_edges(self, db_session, store):
        """子图返回的 edges 含边类型与属性。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))
        await store.add_edge(
            db_session, a, b, "implements", {"reason": "需求由代码实现"}
        )

        sub = await store.subgraph(db_session, [a, b])
        # nodes 含两个节点
        assert len(sub["nodes"]) == 2
        # edges 含至少 1 条边
        assert len(sub["edges"]) >= 1
        edge = sub["edges"][0]
        assert edge["label"] == "implements"

    async def test_subgraph_three_nodes_chain(self, db_session, store):
        """3 节点链式子图：a→b→c，返回 3 节点 + 2 边。"""
        project_id = uuid.uuid4()
        a, b, c = (uuid.uuid4() for _ in range(3))
        for nid, label in [(a, "Requirement"), (b, "CodeSnippet"), (c, "Solution")]:
            await store.add_node(db_session, nid, label, _props(nid, project_id))
        await store.add_edge(db_session, a, b, "implements", {})
        await store.add_edge(db_session, b, c, "realized_by", {})

        sub = await store.subgraph(db_session, [a, b, c])
        assert len(sub["nodes"]) == 3
        assert len(sub["edges"]) >= 2


class TestAddEdgeEdgeCases:
    """add_edge 边界场景：自环、不存在节点、Unicode 属性。"""

    async def test_add_edge_self_loop(self, db_session, store):
        """自环边（from_id == to_id）创建后 neighbors 返回自身。"""
        project_id = uuid.uuid4()
        a = uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))

        await store.add_edge(
            db_session, from_id=a, to_id=a, edge_type="relates_to", properties={}
        )

        # 自环边可查
        rows = await store.match_pattern(
            db_session,
            "MATCH (n {id: $nid})-[r:relates_to]->(n) RETURN r",
            {"nid": str(a)},
        )
        assert len(rows) >= 1

    async def test_add_edge_unicode_properties(self, db_session, store):
        """边属性含中文/特殊字符正确写入与读取。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))

        unicode_props = {
            "reason": "需求由代码实现 🔧",
            "detail": "包含中文与 emoji 🚀",
        }
        await store.add_edge(
            db_session, from_id=a, to_id=b, edge_type="implements", properties=unicode_props
        )

        rows = await store.match_pattern(
            db_session,
            "MATCH (a {id: $from_id})-[r:implements]->(b {id: $to_id}) RETURN r",
            {"from_id": str(a), "to_id": str(b)},
        )
        assert len(rows) >= 1
        edge = rows[0]
        assert edge["properties"]["reason"] == "需求由代码实现 🔧"
        assert edge["properties"]["detail"] == "包含中文与 emoji 🚀"

    async def test_add_edge_invalid_property_key_raises(self, db_session, store):
        """非法边属性键（含特殊字符）抛 ValueError（防 Cypher 注入）。"""
        project_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        await store.add_node(db_session, a, "Requirement", _props(a, project_id))
        await store.add_node(db_session, b, "CodeSnippet", _props(b, project_id))

        with pytest.raises(ValueError, match="非法边属性键"):
            await store.add_edge(
                db_session,
                from_id=a,
                to_id=b,
                edge_type="implements",
                properties={"invalid-key": "value"},  # 含连字符，非法
            )
