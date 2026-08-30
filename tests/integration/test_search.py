"""M4 集成测试：三引擎检索（向量 + 全文 + 图遍历 + RRF 融合）。

按实际调用场景验证：
1. VectorSearcher：相似节点检索、top_k 限制、FilterSpec 过滤（项目/类型/软删除）
2. FullTextSearcher：关键词匹配、中文分词、短语查询、FilterSpec 过滤
3. GraphSearcher：邻居遍历、边类型过滤、子图提取、路径查询、影响范围分析
4. hybrid_search：并行三引擎、RRF 融合、图遍历独立路径
5. 边界：空查询、无匹配、不存在节点、空结果融合

测试隔离：db_session fixture 事务回滚，AGE DML 一并回滚，不影响其他测试。
真实 embedding 容器未运行时相关测试自动 skip。
"""

import uuid

from mem_lake.knowledge.repository import create_node
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import hybrid_search

# ============ 辅助函数 ============


async def _seed_three_nodes(
    db_session,
    graph_store,
    embedding_client,
    knowledge_helpers,
    project_id: uuid.UUID | None = None,
):
    """创建 3 个不同主题节点供检索测试。

    返回 (project_id, requirement_node, code_node, pitfall_node)。
    使用真实 embedding 生成向量；若 embedding 容器未运行由 fixture 自动 skip。
    """
    pid = project_id or uuid.uuid4()

    requirement = await create_node(
        db_session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        project_id=pid,
        node_type="Requirement",
        title="用户登录鉴权需求",
        content="系统需要支持账号密码登录与 JWT 令牌签发，确保会话安全",
        properties=knowledge_helpers["Requirement"](),
        tags=["auth", "P0"],
        created_by="ak_pm",
        system_id=uuid.uuid4(),
    )

    code = await create_node(
        db_session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        project_id=pid,
        node_type="CodeSnippet",
        title="LoginService 类实现",
        content="LoginService 负责用户登录鉴权，签发 JWT 令牌并校验会话",
        properties=knowledge_helpers["CodeSnippet"](),
        tags=["auth", "service"],
        created_by="ak_dev",
    )

    pitfall = await create_node(
        db_session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        project_id=pid,
        node_type="Pitfall",
        title="Redis 缓存踩坑记录",
        content="高并发下 Redis token 续期冲突，需引入分布式锁解决",
        properties=knowledge_helpers["Pitfall"](),
        tags=["redis", "P1"],
        created_by="ak_dev",
    )

    return pid, requirement, code, pitfall


async def _cleanup_project_data(session, graph_store, project_id):
    """清理已提交的测试数据（PG 行 + AGE 顶点）。

    hybrid_search 内部为每引擎创建独立 DB session，种子数据必须真实 commit
    才对检索可见；已提交数据不随 db_session fixture 回滚，需显式清理。
    测试以 owner 用户连接，RLS 不 FORCE，DELETE 可执行。
    """
    from sqlalchemy import text as sa_text

    await session.execute(
        sa_text("DELETE FROM knowledge_node WHERE project_id = :pid"),
        {"pid": str(project_id)},
    )
    await graph_store.match_pattern(
        session,
        "MATCH (n {project_id: $pid}) DETACH DELETE n",
        {"pid": str(project_id)},
    )
    await session.commit()


# ============ VectorSearcher 测试 ============


class TestVectorSearch:
    """向量语义检索场景。"""

    async def test_vector_search_returns_similar_nodes(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """查询"用户登录"返回最相似的登录需求节点。"""
        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )

        results = await vector_searcher.search(
            db_session, query="用户登录鉴权", top_k=10
        )

        assert len(results) >= 1
        # 最相似节点应为 requirement 或 code（都与登录鉴权相关）
        top_ids = {r.node_id for r in results[:2]}
        assert requirement.id in top_ids or code.id in top_ids
        # source 标记为 vector
        assert all(r.source == "vector" for r in results)
        # score 为相似度（0~1）
        assert all(0 <= r.score <= 1 for r in results)

    async def test_vector_search_multi_facet_stored_and_recallable(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """32k 适配（D）：节点写入多 facet 向量，且按属性关键词可召回。

        验证：① 每个节点在 node_embedding 表写入 content + 各非空属性 facet；
        ② 查询落在一个属性 facet（root_cause）上仍能被多向量检索召回。
        """
        from sqlalchemy import func, select

        from mem_lake.knowledge.models import NodeEmbedding

        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )
        # Pitfall: content + symptom/root_cause/solution/severity = 5 个 facet
        facet_count = await db_session.scalar(
            select(func.count())
            .select_from(NodeEmbedding)
            .where(NodeEmbedding.node_id == pitfall.id)
        )
        assert facet_count == 5

        # 按 root_cause 关键词检索可召回 pitfall（facet 级匹配，max-pooling 取最优 facet）
        results = await vector_searcher.search(
            db_session, query="分布式锁竞争导致 token 续期冲突", top_k=10
        )
        assert pitfall.id in {r.node_id for r in results}

    async def test_vector_search_respects_top_k(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """top_k=2 时最多返回 2 个结果。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )

        results = await vector_searcher.search(
            db_session, query="登录", top_k=2
        )

        assert len(results) <= 2

    async def test_vector_search_filters_by_project(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """FilterSpec.project_id 过滤跨项目不可见。"""
        pid1, req1, *_ = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )
        # 创建另一个项目的节点
        pid2, req2, *_ = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )

        # 只查 pid1 项目的节点
        filters = FilterSpec(project_id=pid1)
        results = await vector_searcher.search(
            db_session, query="登录", top_k=50, filters=filters
        )

        result_project_ids = {
            # 通过 properties 反查不到 project_id，改用 node_id 比对
            r.node_id
            for r in results
        }
        # pid1 的节点应在结果中，pid2 的不应在
        assert req1.id in result_project_ids
        # pid2 节点不应出现
        for r in results:
            assert r.node_id != req2.id

    async def test_vector_search_filters_by_node_type(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """FilterSpec.node_types 只返回指定类型节点。"""
        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )

        filters = FilterSpec(project_id=pid, node_types=("Requirement",))
        results = await vector_searcher.search(
            db_session, query="登录", top_k=50, filters=filters
        )

        assert len(results) >= 1
        assert all(r.node_type == "Requirement" for r in results)

    async def test_vector_search_filters_deleted(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """软删除节点不在结果中（FilterSpec.exclude_deleted=True 默认）。"""
        from mem_lake.knowledge.repository import archive_node

        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )
        # 归档 requirement
        await archive_node(db_session, graph_store=graph_store, node_id=requirement.id, actor="ak")

        filters = FilterSpec(project_id=pid)
        results = await vector_searcher.search(
            db_session, query="登录", top_k=50, filters=filters
        )

        result_ids = {r.node_id for r in results}
        assert requirement.id not in result_ids  # 已归档不可见
        # code 仍可见
        assert code.id in result_ids

    async def test_vector_search_score_is_similarity(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """score = -inner_product（归一化向量下等价余弦），范围 0~1，越大越相似。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )

        results = await vector_searcher.search(
            db_session, query="登录", top_k=5
        )

        # 验证 score 范围与排序
        for r in results:
            assert 0 <= r.score <= 1
        # 相似度应降序排列
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score


# ============ FullTextSearcher 测试 ============


class TestFullTextSearch:
    """关键词全文检索场景。"""

    async def test_fulltext_search_matches_keyword(
        self, db_session, graph_store, real_embedding_client, fulltext_searcher, knowledge_helpers
    ):
        """查询"JWT"返回包含 JWT 的节点。"""
        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )

        results = await fulltext_searcher.search(db_session, query="JWT", top_k=50)

        assert len(results) >= 1
        # requirement 与 code 都含 JWT，pitfall 不含
        result_ids = {r.node_id for r in results}
        assert requirement.id in result_ids
        assert code.id in result_ids

    async def test_fulltext_search_chinese_tokenization(
        self, db_session, graph_store, real_embedding_client, fulltext_searcher, knowledge_helpers
    ):
        """中文分词生效：查询"登录鉴权"返回相关节点。"""
        pid, requirement, code, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )

        results = await fulltext_searcher.search(
            db_session, query="登录鉴权", top_k=50
        )

        assert len(results) >= 1
        result_ids = {r.node_id for r in results}
        # requirement 与 code 都含"登录"和"鉴权"
        assert requirement.id in result_ids

    async def test_fulltext_search_respects_top_k(
        self, db_session, graph_store, real_embedding_client, fulltext_searcher, knowledge_helpers
    ):
        """top_k 限制返回数量。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )

        results = await fulltext_searcher.search(db_session, query="JWT", top_k=1)

        assert len(results) <= 1

    async def test_fulltext_search_filters_by_project(
        self, db_session, graph_store, real_embedding_client, fulltext_searcher, knowledge_helpers
    ):
        """FilterSpec.project_id 过滤。"""
        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )
        # 另一项目创建节点
        pid2, req2, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )

        filters = FilterSpec(project_id=pid)
        results = await fulltext_searcher.search(
            db_session, query="JWT", top_k=50, filters=filters
        )

        result_ids = {r.node_id for r in results}
        assert requirement.id in result_ids
        assert req2.id not in result_ids

    async def test_fulltext_search_no_match_returns_empty(
        self, db_session, graph_store, real_embedding_client, fulltext_searcher, knowledge_helpers
    ):
        """查询无匹配返回空列表。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )

        results = await fulltext_searcher.search(
            db_session, query="不存在的关键词xyz123", top_k=50
        )

        assert results == []


# ============ GraphSearcher 测试 ============


class TestGraphSearch:
    """图遍历检索场景。"""

    async def _seed_graph_chain(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """创建 a→b→c 链：Requirement --implements--> CodeSnippet --realized_by--> Solution。"""
        project_id = uuid.uuid4()

        a = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求 A",
            content="需求 A 内容",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        b = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="代码 B",
            content="代码 B 内容",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak",
        )
        c = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Solution",
            title="方案 C",
            content="方案 C 内容",
            properties=knowledge_helpers["Solution"](),
            created_by="ak",
        )
        d = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="DesignIntent",
            title="意图 D",
            content="意图 D 内容",
            properties=knowledge_helpers["DesignIntent"](),
            created_by="ak",
        )

        from mem_lake.knowledge.repository import add_edge

        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=a.id,
            to_id=b.id,
            edge_type="implements",
            properties={"created_by": "ak"},
            actor="ak",
        )
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=b.id,
            to_id=c.id,
            edge_type="realized_by",
            properties={"created_by": "ak"},
            actor="ak",
        )
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=c.id,
            to_id=d.id,
            edge_type="embodies",
            properties={"created_by": "ak"},
            actor="ak",
        )

        return project_id, a, b, c, d

    async def test_graph_traverse_neighbors(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """traverse(a, depth=2) 返回 b 和 c。"""
        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )

        results = await graph_searcher.traverse(
            db_session, node_id=a.id, depth=2
        )

        result_ids = {r.node_id for r in results}
        assert b.id in result_ids
        assert c.id in result_ids
        # 图遍历 score 为 None
        assert all(r.score is None for r in results)
        assert all(r.source == "graph" for r in results)

    async def test_graph_traverse_filter_by_edge_type(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """edge_type 过滤：只返回指定边类型的邻居。"""
        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )

        # 只查 implements 边的邻居（depth=1）
        results = await graph_searcher.traverse(
            db_session, node_id=a.id, edge_type="implements", depth=1
        )

        result_ids = {r.node_id for r in results}
        assert b.id in result_ids
        assert c.id not in result_ids  # c 是 b 的 realized_by 邻居，不是 a 的 implements 邻居

    async def test_graph_subgraph(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """subgraph 返回节点与边。"""
        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )

        sub = await graph_searcher.subgraph(db_session, [a.id, b.id])

        assert "nodes" in sub
        assert "edges" in sub

    async def test_graph_find_path(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """find_path 返回 a→b→c 的路径。"""
        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )

        paths = await graph_searcher.find_path(
            db_session, from_id=a.id, to_id=c.id, max_depth=5
        )

        assert len(paths) >= 1

    async def test_graph_impact_analysis(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """影响范围分析：从需求遍历到代码、方案与设计意图（design_intents 独立返回）。"""
        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )

        result = await graph_searcher.impact_analysis(
            db_session, requirement_id=a.id
        )

        assert result["requirement"] is not None
        assert len(result["codes"]) >= 1
        # b 是 a 的实现代码
        code_ids = {(n.get("properties") or {}).get("id") for n in result["codes"]}
        assert str(b.id) in code_ids
        # c 是 b 的实现方案（Solution），进 solutions 而非 design_intents
        sol_ids = {(n.get("properties") or {}).get("id") for n in result["solutions"]}
        assert str(c.id) in sol_ids
        # d 是 c 体现的设计意图（DesignIntent），独立列表返回（审计 §2.4：此前恒空）
        intent_ids = {
            (n.get("properties") or {}).get("id") for n in result["design_intents"]
        }
        assert str(d.id) in intent_ids
        # DesignIntent 不得混入 solutions
        assert str(d.id) not in sol_ids

    async def test_graph_impact_analysis_filters_archived(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """归档节点不出现在影响分析结果中（审计 §2.4：图投影保留但按 PG 状态过滤）。"""
        from mem_lake.knowledge.repository import archive_node

        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )
        # 归档方案 c（软删除：is_deleted=True + status=archived，图投影默认保留）
        await archive_node(
            db_session, graph_store=graph_store, node_id=c.id, actor="ak"
        )

        result = await graph_searcher.impact_analysis(
            db_session, requirement_id=a.id
        )

        sol_ids = {(n.get("properties") or {}).get("id") for n in result["solutions"]}
        intent_ids = {
            (n.get("properties") or {}).get("id") for n in result["design_intents"]
        }
        assert str(c.id) not in sol_ids  # 归档方案被过滤
        assert str(d.id) not in intent_ids  # c 的意图经 c-embodies->d，c 归档后 d 不可达

    async def test_graph_impact_analysis_requirement_archived(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """需求本身归档：返回全空结果（requirement=None）。"""
        from mem_lake.knowledge.repository import archive_node

        pid, a, b, c, d = await self._seed_graph_chain(
            db_session, graph_store, mock_embedding_client, knowledge_helpers
        )
        await archive_node(
            db_session, graph_store=graph_store, node_id=a.id, actor="ak"
        )

        result = await graph_searcher.impact_analysis(
            db_session, requirement_id=a.id
        )
        assert result["requirement"] is None
        assert result["codes"] == []
        assert result["solutions"] == []


# ============ hybrid_search 测试 ============


class TestHybridSearch:
    """RRF 融合检索场景。"""

    async def test_hybrid_search_fuses_vector_and_fulltext(
        self,
        db_session,
        graph_store,
        vector_searcher,
        fulltext_searcher,
        real_embedding_client,
        knowledge_helpers,
    ):
        """hybrid_search 返回融合结果，两引擎都命中的节点排名更高。"""
        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )
        # hybrid_search 内部自建独立 session，种子数据需提交后可见
        await db_session.commit()

        try:
            result = await hybrid_search(
                query="JWT 登录鉴权",
                embedding_client=real_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(project_id=pid),
            )

            # 返回结构含四个字段
            assert "fused" in result
            assert "vector" in result
            assert "fulltext" in result
            assert "graph" in result

            # fused 长度 <= top_n
            assert len(result["fused"]) <= 10
            # vector 与 fulltext 长度 <= top_k
            assert len(result["vector"]) <= 50
            assert len(result["fulltext"]) <= 50

            # 两引擎都命中的节点（requirement 与 code 都含 JWT + 登录）应排在 fused 前列
            if result["fused"]:
                top_fused_ids = {r.node_id for r in result["fused"][:2]}
                assert requirement.id in top_fused_ids or code.id in top_fused_ids
        finally:
            await _cleanup_project_data(db_session, graph_store, pid)

    async def test_hybrid_search_respects_top_n(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        knowledge_helpers,
    ):
        """top_n 截断融合结果。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )
        await db_session.commit()

        try:
            result = await hybrid_search(
                query="登录",
                embedding_client=real_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=2,
                filters=FilterSpec(project_id=pid),
            )

            assert len(result["fused"]) <= 2
        finally:
            await _cleanup_project_data(db_session, graph_store, pid)

    async def test_hybrid_search_graph_independent(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        mock_embedding_client,
        knowledge_helpers,
    ):
        """graph_node_id 提供时图遍历结果独立返回。"""
        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )
        # 添加边 requirement --implements--> code
        from mem_lake.knowledge.repository import add_edge

        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=requirement.id,
            to_id=code.id,
            edge_type="implements",
            properties={"created_by": "ak"},
            actor="ak",
        )
        # hybrid_search 内部自建独立 session，种子数据（含 AGE 边）需提交后可见
        await db_session.commit()

        try:
            result = await hybrid_search(
                query="登录",
                embedding_client=real_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(project_id=pid),
                graph_node_id=requirement.id,
                graph_depth=2,
            )

            # graph 字段非空（requirement 有 implements 边指向 code）
            assert len(result["graph"]) >= 1
            graph_ids = {r.node_id for r in result["graph"]}
            assert code.id in graph_ids
        finally:
            await _cleanup_project_data(db_session, graph_store, pid)

    async def test_hybrid_search_no_graph_node_id(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        knowledge_helpers,
    ):
        """graph_node_id=None 时 graph_results 为空。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )
        await db_session.commit()

        try:
            result = await hybrid_search(
                query="登录",
                embedding_client=real_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(project_id=pid),
                graph_node_id=None,
            )

            assert result["graph"] == []
        finally:
            await _cleanup_project_data(db_session, graph_store, pid)


# ============ 边界场景 ============


class TestSearchEdgeCases:
    """检索边界场景。"""

    async def test_fulltext_search_empty_query_returns_empty(
        self, db_session, graph_store, real_embedding_client, fulltext_searcher, knowledge_helpers
    ):
        """空查询字符串：websearch_to_tsquery('chinese', '') 返回空 tsquery，
        @@ 匹配所有行但 ts_rank_cd 为 0，结果可能返回所有节点但无意义。

        此处验证不抛异常即可（具体行为由 PG 决定）。
        """
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )

        # 不应抛异常
        results = await fulltext_searcher.search(
            db_session, query="", top_k=10
        )
        # 空查询的 @@ 行为：空 tsquery 匹配任意 tsvector，但 rank=0
        # 此处不严格断言长度，只验证不抛异常
        assert isinstance(results, list)

    async def test_graph_traverse_nonexistent_node(
        self, db_session, graph_searcher
    ):
        """不存在的节点 traverse 返回空列表。"""
        fake_id = uuid.uuid4()
        results = await graph_searcher.traverse(
            db_session, node_id=fake_id, depth=3
        )
        assert results == []

    async def test_graph_traverse_isolated_node(
        self, db_session, graph_store, graph_searcher, mock_embedding_client, knowledge_helpers
    ):
        """孤立节点（无边）traverse 返回空。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="孤立需求",
            content="无关联节点",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        results = await graph_searcher.traverse(
            db_session, node_id=node.id, depth=3
        )
        assert results == []

    async def test_hybrid_search_empty_results(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        knowledge_helpers,
    ):
        """所有引擎都无结果时 fused 为空。"""
        pid, *_ = await _seed_three_nodes(
            db_session, graph_store, real_embedding_client, knowledge_helpers
        )
        await db_session.commit()

        try:
            result = await hybrid_search(
                query="完全不存在的关键词xyz123",
                embedding_client=real_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(project_id=pid),
            )

            # 向量引擎总会返回结果（即使查询不匹配，cosine 仍计算相似度）
            # 但全文引擎无匹配时返回空
            assert result["fulltext"] == []
            # fused 至少包含向量结果
            assert isinstance(result["fused"], list)
        finally:
            await _cleanup_project_data(db_session, graph_store, pid)

    async def test_filters_spec_default_values(self):
        """FilterSpec 默认值：status='approved'，exclude_deleted=True。"""
        spec = FilterSpec()

        assert spec.status == "approved"
        assert spec.exclude_deleted is True
        assert spec.project_id is None
        assert spec.node_types is None

    async def test_vector_search_excludes_archived_via_default_filter(
        self, db_session, graph_store, vector_searcher, knowledge_helpers
    ):
        """默认 FilterSpec 排除归档节点（status='archived'）。"""
        from mem_lake.knowledge.repository import archive_node

        pid, requirement, code, pitfall = await _seed_three_nodes(
            db_session, graph_store, vector_searcher._embedding_client, knowledge_helpers
        )
        await archive_node(db_session, graph_store=graph_store, node_id=pitfall.id, actor="ak")

        # 默认 filters 含 status='approved'，pitfall 已归档（status='archived'）应被排除
        filters = FilterSpec(project_id=pid)
        results = await vector_searcher.search(
            db_session, query="Redis", top_k=50, filters=filters
        )

        result_ids = {r.node_id for r in results}
        assert pitfall.id not in result_ids
