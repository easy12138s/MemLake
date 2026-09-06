"""ENH-01 图能力集成测试：get_graph_stats / get_graph_quality_report / 规则边生成。

基于真实 DB + AGE 图（db_session 事务回滚隔离，graph_store 复用 conftest）。
agent 构建图数据后验证：
- get_graph_stats：节点/边按 type、system 维度聚合正确
- get_graph_quality_report：孤儿节点/重复度/连通分量计算正确
- build_rule_edge_items + submit_batch：规则边生成走审批批次（pending_review）
"""

import uuid

import pytest

from mem_lake.approval.repository import submit_batch
from mem_lake.knowledge.graph_stats import (
    RULE_SNIPPET_MODULE_REFERENCES,
    build_rule_edge_items,
    get_graph_quality_report,
    get_graph_stats,
)
from mem_lake.knowledge.repository import create_node


async def _clear_graph(db_session, graph_store) -> None:
    """清空图中全部节点与边（仅当前测试事务内；db_session 结束 rollback 还原）。"""
    await graph_store._exec_cypher(db_session, "MATCH (n) DETACH DELETE n")


async def await_create_node(
    db_session,
    graph_store,
    *,
    node_type: str,
    title: str,
    properties: dict,
    project_id: uuid.UUID,
    system_id: uuid.UUID,
):
    return await create_node(
        db_session,
        graph_store=graph_store,
        embedding_client=None,
        project_id=project_id,
        node_type=node_type,
        title=title,
        content=title,
        properties=properties,
        created_by="admin-key",
        system_id=system_id,
        generate_vector=False,
    )


class TestGetGraphStats:
    """get_graph_stats 节点/边按 type、system 维度聚合。"""

    @pytest.mark.asyncio
    async def test_stats_aggregates_by_type_and_system(
        self, db_session, graph_store
    ):
        await _clear_graph(db_session, graph_store)
        pid = uuid.uuid4()
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        n1 = await await_create_node(
            db_session, graph_store, node_type="CodeSnippet", title="svc",
            properties={"name": "Svc", "type": "class", "responsibility": "auth", "file_path": "a.py"},
            project_id=pid, system_id=s1,
        )
        await await_create_node(  # n2：Requirement
            db_session, graph_store, node_type="Requirement", title="reqX",
            properties={"priority": "P0", "module": "auth"}, project_id=pid, system_id=s1,
        )
        n3 = await await_create_node(
            db_session, graph_store, node_type="Requirement", title="dup",
            properties={"priority": "P1", "module": "report"}, project_id=pid, system_id=s1,
        )
        await await_create_node(  # n4：另一个 system 的同名 Requirement
            db_session, graph_store, node_type="Requirement", title="dup",
            properties={"priority": "P1", "module": "report"}, project_id=pid, system_id=s2,
        )
        # 一条有向边 n1 --depends_on--> n3
        await graph_store.add_edge(
            db_session, from_id=n1.id, to_id=n3.id,
            edge_type="depends_on", properties={},
        )

        stats = await get_graph_stats(db_session, graph_store)

        assert stats["nodes_by_type"] == {"CodeSnippet": 1, "Requirement": 3}
        assert stats["nodes_by_system"] == {str(s1): 3, str(s2): 1}
        assert stats["edges_by_type"] == {"depends_on": 1}
        assert stats["edges_by_system"] == {str(s1): 1}

    @pytest.mark.asyncio
    async def test_empty_graph_returns_empty_counts(self, db_session, graph_store):
        """空图：各维度计数为空字典，不抛错。"""
        await _clear_graph(db_session, graph_store)
        stats = await get_graph_stats(db_session, graph_store)
        assert stats["nodes_by_type"] == {}
        assert stats["nodes_by_system"] == {}
        assert stats["edges_by_type"] == {}
        assert stats["edges_by_system"] == {}


class TestGetGraphQualityReport:
    """get_graph_quality_report 孤儿/重复/连通分量。"""

    @pytest.mark.asyncio
    async def test_report_metrics(self, db_session, graph_store):
        await _clear_graph(db_session, graph_store)
        pid = uuid.uuid4()
        s1 = uuid.uuid4()
        n1 = await await_create_node(
            db_session, graph_store, node_type="CodeSnippet", title="svc",
            properties={"name": "Svc", "type": "class", "responsibility": "auth", "file_path": "a.py"},
            project_id=pid, system_id=s1,
        )
        # n2 孤立 Requirement
        await await_create_node(  # noqa: F841
            db_session, graph_store, node_type="Requirement", title="orphan",
            properties={"priority": "P0", "module": "auth"}, project_id=pid, system_id=s1,
        )
        # n3/n4 同 type+title（重复），n3 与 n1 相连，n4 孤立
        n3 = await await_create_node(
            db_session, graph_store, node_type="Requirement", title="dup",
            properties={"priority": "P1", "module": "report"}, project_id=pid, system_id=s1,
        )
        # n4 孤立 Requirement（单元义：构成重复分组）
        await await_create_node(  # noqa: F841
            db_session, graph_store, node_type="Requirement", title="dup",
            properties={"priority": "P1", "module": "report"}, project_id=pid, system_id=s1,
        )
        await graph_store.add_edge(
            db_session, from_id=n1.id, to_id=n3.id,
            edge_type="depends_on", properties={},
        )

        report = await get_graph_quality_report(db_session, graph_store)

        assert report["total_nodes"] == 4
        assert report["total_edges"] == 1
        assert report["orphan_nodes"] == 2  # n2, n4
        assert report["duplicate_groups_count"] == 1  # ("Requirement","dup")
        assert report["duplicate_node_count"] == 2  # n3, n4
        assert report["connected_components"] == 3  # {n1,n3}, {n2}, {n4}


class TestGenerateRuleEdges:
    """规则边生成：snippet_module_references → 审批批次。"""

    async def _seed(self, db_session, graph_store):
        pid = uuid.uuid4()
        s1 = uuid.uuid4()
        # CodeSnippet responsibility 含 module "auth"
        snippet = await await_create_node(
            db_session, graph_store, node_type="CodeSnippet", title="LoginService",
            properties={"name": "LoginService", "type": "class",
                        "responsibility": "负责 auth 模块登录鉴权", "file_path": "src/auth/login.py"},
            project_id=pid, system_id=s1,
        )
        # Requirement module=auth（应命中）；另一个 module=billing（不应命中）
        req_auth = await await_create_node(
            db_session, graph_store, node_type="Requirement", title="登录需求",
            properties={"priority": "P0", "module": "auth"}, project_id=pid, system_id=s1,
        )
        req_billing = await await_create_node(
            db_session, graph_store, node_type="Requirement", title="计费需求",
            properties={"priority": "P1", "module": "billing"}, project_id=pid, system_id=s1,
        )
        return pid, snippet, req_auth, req_billing

    @pytest.mark.asyncio
    async def test_rule_matches_module_and_skips_non_match(
        self, db_session, graph_store
    ):
        pid, snippet, req_auth, req_billing = await self._seed(db_session, graph_store)
        items, matched = await build_rule_edge_items(
            db_session, graph_store, rule=RULE_SNIPPET_MODULE_REFERENCES, project_id=pid,
        )

        assert len(items) == 1
        edge = items[0]
        assert edge["item_type"] == "edge"
        assert edge["action"] == "create"
        assert edge["entity_type"] == "references"
        assert edge["payload"]["from_ref"] == str(snippet.id)
        assert edge["payload"]["to_ref"] == str(req_auth.id)
        assert matched[0]["module"] == "auth"
        assert req_billing.id not in {m["target"] for m in matched}

    @pytest.mark.asyncio
    async def test_submit_rule_edges_creates_pending_batch(
        self, db_session, graph_store
    ):
        """经 submit_batch（batch_type=generate_rule_edges）提交为 pending_review 批次。"""
        pid, snippet, req_auth, _req_billing = await self._seed(db_session, graph_store)
        items, _matched = await build_rule_edge_items(
            db_session, graph_store, rule=RULE_SNIPPET_MODULE_REFERENCES, project_id=pid,
        )
        assert items

        batch = await submit_batch(
            db_session,
            project_id=pid,
            batch_type="generate_rule_edges",
            submitted_by="admin-key",
            submitter_role="admin",
            items=items,
        )
        assert batch.status == "pending_review"
        assert batch.batch_type == "generate_rule_edges"
        assert len(batch.items) == 1
        assert batch.items[0].entity_type == "references"

    @pytest.mark.asyncio
    async def test_existing_reference_edge_skipped(self, db_session, graph_store):
        """图中已有该 references 边时，规则引擎幂等跳过（不再产出）。"""
        pid, snippet, req_auth, _req_billing = await self._seed(db_session, graph_store)
        # 预置一条已存在的 references 边
        await graph_store.add_edge(
            db_session, from_id=snippet.id, to_id=req_auth.id,
            edge_type="references", properties={},
        )
        items, matched = await build_rule_edge_items(
            db_session, graph_store, rule=RULE_SNIPPET_MODULE_REFERENCES, project_id=pid,
        )
        assert items == []
        assert matched == []

    @pytest.mark.asyncio
    async def test_unknown_rule_raises(self, db_session, graph_store):
        with pytest.raises(ValueError):
            await build_rule_edge_items(
                db_session, graph_store, rule="no_such_rule", project_id=None,
            )
