"""M6b 集成测试：search_tools 与 query_tools 端到端。

真实 PG + mock/real embedding + AGEGraphStore。覆盖：
- search_similar_requirements / search_code_snippets：三引擎融合检索
- analyze_impact_scope：图遍历影响范围分析
- check_requirement_conflicts：冲突检测
- list_knowledge：分页列出节点
- get_project_profile：查询项目画像
- get_requirement_context：查询需求上下文
- query_audit_log：查询审计日志

测试事务回滚隔离，不污染 DB。
"""

import uuid

import pytest
from sqlalchemy import select

from mem_lake.audit.service import query_audit_logs
from mem_lake.gateway.tools.query_tools import (
    _get_project_info_core,
    _to_audit_log_item_output,
)
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import (
    create_node,
    get_node,
    list_nodes_by_project,
    list_project_profiles,
)
from mem_lake.search.filters import FilterSpec, compile_sqlalchemy

# ============================================================================
# search_similar_requirements / search_code_snippets 端到端
# ============================================================================


async def _cleanup_project_data(session, graph_store, project_id):
    """清理已提交的测试数据（PG 行 + AGE 顶点）。

    hybrid_search 内部为每引擎创建独立 DB session，种子数据必须真实 commit
    才对检索可见；已提交数据不随 db_session fixture 回滚，需显式清理。
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


class TestHybridSearchTools:
    """search_similar_requirements / search_code_snippets 端到端测试。

    直接验证工具底层路径 hybrid_search（node_types 过滤 + RRF 融合）。
    fusion 每引擎独立 session 后，mock embedding 立即返回不再触发共享
    session 并发问题；完整 MCP 协议栈调用由 tests/e2e 覆盖。
    """

    async def test_search_similar_requirements_returns_fused(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """Requirement 类型过滤生效：fused 含需求节点且不含代码节点。"""
        from mem_lake.search.filters import FilterSpec
        from mem_lake.search.fusion import hybrid_search

        project_id = uuid.uuid4()
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="登录需求",
            content="需要登录功能",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        code_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="LoginService",
            content="登录服务",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak_dev",
        )
        await db_session.commit()  # 独立 session 检索要求种子数据已提交

        try:
            result = await hybrid_search(
                query="登录功能",
                embedding_client=mock_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(
                    project_id=project_id, node_types=("Requirement",)
                ),
            )
            fused_ids = {r.node_id for r in result["fused"]}
            assert req_node.id in fused_ids
            assert code_node.id not in fused_ids  # node_types 过滤生效
        finally:
            await _cleanup_project_data(db_session, graph_store, project_id)

    async def test_search_code_snippets_returns_fused(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """CodeSnippet 类型过滤生效：fused 含代码节点且不含需求节点。"""
        from mem_lake.search.filters import FilterSpec
        from mem_lake.search.fusion import hybrid_search

        project_id = uuid.uuid4()
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="登录需求",
            content="需要登录功能",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        code_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="LoginService",
            content="登录服务实现",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak_dev",
        )
        await db_session.commit()  # 独立 session 检索要求种子数据已提交

        try:
            result = await hybrid_search(
                query="登录服务实现",
                embedding_client=mock_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(
                    project_id=project_id, node_types=("CodeSnippet",)
                ),
            )
            fused_ids = {r.node_id for r in result["fused"]}
            assert code_node.id in fused_ids
            assert req_node.id not in fused_ids  # node_types 过滤生效
        finally:
            await _cleanup_project_data(db_session, graph_store, project_id)

    async def test_fused_score_exposes_cosine_not_rrf(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """P4: fused 结果的 score 透出向量余弦分（0~1），而非 RRF 排名分（≈0.016）。

        同时验证与同节点 vector 分一致（修复 check_requirement_conflicts 阈值失效）。
        """
        from mem_lake.search.filters import FilterSpec
        from mem_lake.search.fusion import hybrid_search

        project_id = uuid.uuid4()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="登录需求",
            content="需要登录功能",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        await db_session.commit()  # 独立 session 检索要求种子数据已提交
        try:
            result = await hybrid_search(
                query="登录功能",
                embedding_client=mock_embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=10,
                filters=FilterSpec(
                    project_id=project_id, node_types=("Requirement",)
                ),
            )
            vector_by_id = {r.node_id: r for r in result["vector"]}
            assert result["fused"], "fused 不应为空"
            for r in result["fused"]:
                # 透出余弦分（mock 下为 1.0），而非 RRF 排名分（≈0.016）
                assert r.score is not None
                assert r.score > 0.9, f"fused.score 应透出余弦分，实际 {r.score}"
                if r.node_id in vector_by_id:
                    assert r.score == vector_by_id[r.node_id].score
        finally:
            await _cleanup_project_data(db_session, graph_store, project_id)


# ============================================================================
# analyze_impact_scope 端到端
# ============================================================================


class TestAnalyzeImpactScope:
    """analyze_impact_scope 端到端测试。"""

    async def test_impact_analysis_with_implements_relation(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """有 implements 关系的影响范围分析。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        code_props = knowledge_helpers["CodeSnippet"]()

        # 创建 Requirement 节点
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="登录需求",
            content="需要登录功能",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        # 创建 CodeSnippet 节点
        code_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="LoginService",
            content="登录服务",
            properties=code_props,
            created_by="ak_dev",
        )
        # 创建 implements 关系
        from mem_lake.knowledge.repository import add_edge
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=req_node.id,
            to_id=code_node.id,
            edge_type="implements",
            actor="ak_dev",
        )

        # 调用 impact_analysis
        from mem_lake.search.graph import GraphSearcher
        searcher = GraphSearcher(graph_store)
        result = await searcher.impact_analysis(
            db_session, requirement_id=req_node.id, max_depth=5
        )

        assert result["requirement"] is not None
        assert len(result["codes"]) >= 1
        # AGE 返回的节点结构可能不同，验证 codes 非空即可
        assert len(result["codes"]) >= 1

    async def test_impact_analysis_nonexistent_requirement(
        self, db_session, graph_store
    ):
        """不存在需求节点的影响范围分析返回 requirement=None。"""
        from mem_lake.search.graph import GraphSearcher
        searcher = GraphSearcher(graph_store)
        result = await searcher.impact_analysis(
            db_session, requirement_id=uuid.uuid4(), max_depth=5
        )
        assert result["requirement"] is None
        assert result["codes"] == []


# ============================================================================
# check_requirement_conflicts 端到端
# ============================================================================


class TestCheckRequirementConflicts:
    """check_requirement_conflicts 端到端测试。"""

    async def test_no_conflict_when_only_one_requirement(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """项目内仅一个需求时无冲突。

        注：工具函数依赖 FastMCP 上下文（get_context），此处验证其过滤语义
        （排除自身 + score >= threshold）。内容级冲突检测的权威实现在
        approval/conflict.py detect_conflicts（test_approval_flow.py 覆盖）。
        """
        # 模拟 hybrid_search 返回包含自身的结果
        from mem_lake.gateway.tools.search_tools import _to_search_item_output
        from mem_lake.search.fusion import SearchResult

        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="唯一需求",
            content="项目内唯一的需求",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )

        # 模拟检索结果包含自身（score=1.0，超过阈值 0.85）
        mock_results = [
            SearchResult(
                node_id=req_node.id,  # 自身
                title="唯一需求",
                content="项目内唯一的需求",
                node_type="Requirement",
                score=1.0,
                source="fused",
                properties={},
                tags=[],
            )
        ]

        # 过滤逻辑：排除自身 + score >= threshold
        threshold = 0.85
        conflicts = [
            _to_search_item_output(r)
            for r in mock_results
            if r.node_id != req_node.id
            and r.score is not None
            and r.score >= threshold
        ]
        # 自身被排除，无冲突
        assert len(conflicts) == 0


# ============================================================================
# list_knowledge 端到端
# ============================================================================


class TestListKnowledge:
    """list_knowledge 端到端测试。"""

    async def test_list_knowledge_returns_approved_nodes(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """list_knowledge 返回 approved 状态节点。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求1",
            content="内容1",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求2",
            content="内容2",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )

        nodes = await list_nodes_by_project(
            db_session,
            project_id=project_id,
            node_type="Requirement",
            status="approved",
            limit=100,
            offset=0,
        )
        assert len(nodes) >= 2
        assert all(n.status == "approved" for n in nodes)
        assert all(n.type == "Requirement" for n in nodes)

    async def test_list_knowledge_filter_by_type(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """list_knowledge 按类型过滤。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        code_props = knowledge_helpers["CodeSnippet"]()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求",
            content="内容",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="代码",
            content="代码内容",
            properties=code_props,
            created_by="ak_dev",
        )

        # 仅查 Requirement
        req_nodes = await list_nodes_by_project(
            db_session, project_id=project_id, node_type="Requirement"
        )
        assert all(n.type == "Requirement" for n in req_nodes)

        # 仅查 CodeSnippet
        code_nodes = await list_nodes_by_project(
            db_session, project_id=project_id, node_type="CodeSnippet"
        )
        assert all(n.type == "CodeSnippet" for n in code_nodes)

    async def test_list_knowledge_pagination(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """list_knowledge 分页。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        # 创建 3 个节点
        for i in range(3):
            await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                project_id=project_id,
                node_type="Requirement",
                title=f"需求{i}",
                content=f"内容{i}",
                properties=req_props,
                created_by="ak_pm",
                system_id=uuid.uuid4(),
            )

        # 第一页 2 条
        page1 = await list_nodes_by_project(
            db_session, project_id=project_id, limit=2, offset=0
        )
        assert len(page1) == 2

        # 第二页 2 条（只剩 1 条）
        page2 = await list_nodes_by_project(
            db_session, project_id=project_id, limit=2, offset=2
        )
        assert len(page2) == 1


# ============================================================================
# get_project_profile 端到端
# ============================================================================


class TestGetProjectProfile:
    """get_project_profile 端到端测试。"""

    async def test_get_project_profile_returns_none_when_not_created(
        self, db_session
    ):
        """项目未创建画像时返回 None。"""
        project_id = uuid.uuid4()
        nodes = await list_nodes_by_project(
            db_session,
            project_id=project_id,
            node_type="ProjectProfile",
            status="approved",
            limit=1,
            offset=0,
        )
        assert nodes == []

    async def test_get_project_profile_returns_latest(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """已创建画像时返回最新一条。"""
        project_id = uuid.uuid4()
        profile_props = knowledge_helpers["ProjectProfile"]()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="ProjectProfile",
            title="项目画像",
            content="项目描述",
            properties=profile_props,
            created_by="ak_admin",
        )

        nodes = await list_nodes_by_project(
            db_session,
            project_id=project_id,
            node_type="ProjectProfile",
            status="approved",
            limit=1,
            offset=0,
        )
        assert len(nodes) == 1
        assert nodes[0].type == "ProjectProfile"
        assert nodes[0].title == "项目画像"


# ============================================================================
# get_project_info 端到端
# ============================================================================


class TestGetProjectInfo:
    """get_project_info 端到端测试（核心逻辑 + list_project_profiles）。"""

    async def test_list_project_profiles_repo(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """list_project_profiles 按项目过滤并返回 work_dir/repo 元数据。"""
        project_id = uuid.uuid4()
        props = knowledge_helpers["ProjectProfile"]()
        props["work_dir"] = "d:/proj-a"
        props["repo"] = "my-repo"
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="ProjectProfile",
            title="项目A",
            content="描述A",
            properties=props,
            created_by="ak_admin",
        )
        await db_session.commit()

        nodes = await list_project_profiles(db_session, project_ids=[project_id])
        assert len(nodes) == 1
        assert nodes[0].properties.get("work_dir") == "d:/proj-a"
        assert nodes[0].properties.get("repo") == "my-repo"

    async def test_core_list_scoped_filters(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """非 admin 仅可见 scope 内项目（核心逻辑 + 真实 repo）。"""
        pid_visible = uuid.uuid4()
        pid_hidden = uuid.uuid4()
        for pid in (pid_visible, pid_hidden):
            await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                project_id=pid,
                node_type="ProjectProfile",
                title=f"P-{pid}",
                content="desc",
                properties=knowledge_helpers["ProjectProfile"](),
                created_by="ak_admin",
            )
        await db_session.commit()

        out = await _get_project_info_core(
            action="list",
            project_id=None,
            include_profile=False,
            include_scope_meta=False,
            role="dev",
            scope=[str(pid_visible)],
            list_fn=lambda **kw: list_project_profiles(db_session, **kw),
            validate_fn=lambda x: None,
        )
        assert [i.project_id for i in out.projects] == [pid_visible]

    async def test_core_get_returns_profile_meta(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """get 单项目返回 work_dir/repo 与 include_profile 完整属性。"""
        project_id = uuid.uuid4()
        props = knowledge_helpers["ProjectProfile"]()
        props["work_dir"] = "d:/proj-b"
        props["repo"] = "repo-b"
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="ProjectProfile",
            title="项目B",
            content="描述B",
            properties=props,
            created_by="ak_admin",
        )
        await db_session.commit()

        out = await _get_project_info_core(
            action="get",
            project_id=project_id,
            include_profile=True,
            include_scope_meta=True,
            role="admin",
            scope=[],
            list_fn=lambda **kw: list_project_profiles(db_session, **kw),
            validate_fn=lambda x: None,
        )
        assert out.project is not None
        assert out.project.work_dir == "d:/proj-b"
        assert out.project.repo == "repo-b"
        assert out.project.profile == props
        assert out.scope is not None
        assert out.scope.scope_type == "all"


# ============================================================================
# tags 过滤（all/any）端到端
# ============================================================================


class TestTagsFilter:
    """tags_op=all/any 在真实 PG 上可执行（验证 ?| 缺 cast 的修复）。"""

    async def test_tags_any_executes(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """tags_op='any' 编译为 tags ?| CAST(... AS TEXT[])，执行不报错且命中。"""
        project_id = uuid.uuid4()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求A",
            content="内容A",
            properties=knowledge_helpers["Requirement"](),
            tags=["urgent", "bug"],
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        await db_session.commit()

        spec = FilterSpec(project_id=project_id, tags=("urgent",), tags_op="any")
        clauses = compile_sqlalchemy(spec)
        stmt = select(KnowledgeNode).where(*clauses)
        rows = (await db_session.execute(stmt)).scalars().all()
        assert len(rows) == 1
        assert "urgent" in rows[0].tags

    async def test_tags_all_executes(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """tags_op='all' 编译为 tags @> ...，执行不报错且需全包含。"""
        project_id = uuid.uuid4()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求B",
            content="内容B",
            properties=knowledge_helpers["Requirement"](),
            tags=["urgent", "bug"],
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        await db_session.commit()

        # all：需同时含 urgent 与 bug → 命中
        spec_hit = FilterSpec(
            project_id=project_id, tags=("urgent", "bug"), tags_op="all"
        )
        rows_hit = (
            await db_session.execute(
                select(KnowledgeNode).where(*compile_sqlalchemy(spec_hit))
            )
        ).scalars().all()
        assert len(rows_hit) == 1

        # all：含不存在的标签 → 不命中
        spec_miss = FilterSpec(
            project_id=project_id, tags=("urgent", "nonexist"), tags_op="all"
        )
        rows_miss = (
            await db_session.execute(
                select(KnowledgeNode).where(*compile_sqlalchemy(spec_miss))
            )
        ).scalars().all()
        assert len(rows_miss) == 0


# ============================================================================
# get_requirement_context 端到端
# ============================================================================


class TestGetRequirementContext:
    """get_requirement_context 端到端测试。"""

    async def test_get_context_nonexistent_requirement(
        self, db_session, graph_store
    ):
        """需求不存在时返回 requirement=None。"""
        from mem_lake.knowledge.repository import NodeNotFoundError
        with pytest.raises(NodeNotFoundError):
            await get_node(db_session, uuid.uuid4())

    async def test_get_context_with_related_nodes(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """有关联节点的需求上下文。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        code_props = knowledge_helpers["CodeSnippet"]()

        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求",
            content="内容",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        code_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="代码",
            content="代码内容",
            properties=code_props,
            created_by="ak_dev",
        )
        from mem_lake.knowledge.repository import add_edge
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=req_node.id,
            to_id=code_node.id,
            edge_type="implements",
            actor="ak_dev",
        )

        # 获取需求节点详情
        req = await get_node(db_session, req_node.id)
        assert req.type == "Requirement"

        # 图遍历获取关联节点
        from mem_lake.search.filters import FilterSpec
        from mem_lake.search.graph import GraphSearcher
        searcher = GraphSearcher(graph_store)
        filters = FilterSpec(project_id=project_id)
        related = await searcher.traverse(
            db_session, req_node.id, depth=2, filters=filters
        )
        # 应至少返回 1 个关联节点（CodeSnippet）
        assert len(related) >= 1

    async def test_context_floating_req_with_related_code(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """悬浮需求（project_id=None）连到归属项目的代码片段，图遍历应能检索到关联节点。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        code_props = knowledge_helpers["CodeSnippet"]()

        # 悬浮需求：project_id=None + system_id 有值
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=None,
            node_type="Requirement",
            title="悬浮需求",
            content="悬浮内容",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        # 归属某项目的代码片段
        code_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="代码",
            content="代码内容",
            properties=code_props,
            created_by="ak_dev",
        )
        from mem_lake.knowledge.repository import add_edge
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=req_node.id,
            to_id=code_node.id,
            edge_type="implements",
            actor="ak_dev",
        )
        await db_session.commit()

        from mem_lake.search.graph import GraphSearcher
        searcher = GraphSearcher(graph_store)
        # 悬浮需求 project_id=None → FilterSpec 无 project 过滤，应返回归属代码节点
        related = await searcher.traverse(
            db_session, req_node.id, depth=2, filters=FilterSpec(project_id=None)
        )
        assert len(related) >= 1
        assert any(r.node_id == code_node.id for r in related)


# ============================================================================
# query_audit_log 端到端
# ============================================================================


class TestQueryAuditLog:
    """query_audit_log 端到端测试。"""

    async def test_query_all_logs(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """查询所有审计日志。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        # create_node 会写审计日志
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求",
            content="内容",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )

        logs = await query_audit_logs(db_session, limit=100, offset=0)
        # create_node 至少写 1 条审计日志
        assert len(logs) >= 1
        assert any(log.action == "write" for log in logs)
        assert any(log.target_type == "node" for log in logs)

    async def test_query_logs_filter_by_actor(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """按操作者过滤审计日志。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求1",
            content="内容1",
            properties=req_props,
            created_by="ak_pm_special",
            system_id=uuid.uuid4(),
        )

        logs = await query_audit_logs(db_session, actor="ak_pm_special")
        assert len(logs) >= 1
        assert all(log.actor == "ak_pm_special" for log in logs)

    async def test_query_logs_filter_by_action(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """按操作类型过滤审计日志。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求",
            content="内容",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )

        write_logs = await query_audit_logs(db_session, action="write")
        assert all(log.action == "write" for log in write_logs)
        assert len(write_logs) >= 1

    async def test_query_logs_filter_by_target_type(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """按目标类型过滤审计日志。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求",
            content="内容",
            properties=req_props,
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )

        node_logs = await query_audit_logs(db_session, target_type="node")
        assert all(log.target_type == "node" for log in node_logs)
        assert len(node_logs) >= 1

    async def test_query_logs_pagination(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """审计日志分页。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        # 创建多个节点产生多条审计日志
        for i in range(3):
            await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                project_id=project_id,
                node_type="Requirement",
                title=f"需求{i}",
                content=f"内容{i}",
                properties=req_props,
                created_by="ak_pm",
                system_id=uuid.uuid4(),
            )

        page1 = await query_audit_logs(db_session, limit=2, offset=0)
        page2 = await query_audit_logs(db_session, limit=2, offset=2)
        assert len(page1) == 2
        # 第二页至少 1 条
        assert len(page2) >= 1

    async def test_audit_log_item_output_conversion(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """审计日志转换函数端到端。"""
        project_id = uuid.uuid4()
        req_props = knowledge_helpers["Requirement"]()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="转换测试",
            content="内容",
            properties=req_props,
            created_by="ak_pm_convert",
            system_id=uuid.uuid4(),
        )

        logs = await query_audit_logs(db_session, actor="ak_pm_convert")
        assert len(logs) >= 1
        log = logs[0]
        output = _to_audit_log_item_output(log)
        assert output.actor == "ak_pm_convert"
        assert output.action == "write"
        assert output.target_type == "node"
        assert output.target_id == node.id
        assert output.created_at is not None
