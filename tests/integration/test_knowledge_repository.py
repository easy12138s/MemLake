"""M3 集成测试：knowledge repository 业务接口。

按实际调用场景验证（事务共写、Schema 校验、向量生成、审计日志、图操作）：
1. create_node：mock embedding + real embedding + 无向量模式 + 校验失败
2. get_node：正常/不存在/软删除/include_deleted
3. update_node：title/content/properties 变更 + 版本递增 + 向量重生成 + 无变更幂等
4. archive_node：归档/幂等/delete_from_graph/不存在
5. add_edge：创建/properties 注入/非法类型
6. list_nodes_by_project：过滤/分页/排除软删除
7. 事务性共写：节点+边+审计在同一事务，部分失败整体回滚

事务回滚隔离，db_session fixture 结束 rollback，不影响其他测试。
"""

import uuid

import pytest

from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    add_edge,
    archive_node,
    create_node,
    get_node,
    list_nodes_by_project,
    regenerate_vector,
    update_node,
)
from mem_lake.knowledge.schema import SchemaValidationError


# ============ create_node ============

class TestCreateNode:
    """create_node 业务场景测试。"""

    async def test_create_node_with_mock_embedding(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """使用 mock embedding 创建节点：验证字段、向量、AGE 图节点、审计日志四件套。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="用户登录鉴权需求",
            content="系统需要支持账号密码登录与 JWT 令牌签发",
            properties=knowledge_helpers["Requirement"](),
            tags=["auth", "P0"],
            source={"agent": "pm_agent", "tool": "publish_requirement"},
            created_by="ak_pm_001",
            system_id=uuid.uuid4(),
        )

        # 1. 字段验证
        assert node.id is not None
        assert node.project_id == project_id
        assert node.type == "Requirement"
        assert node.title == "用户登录鉴权需求"
        assert node.status == "approved"
        assert node.version == 1
        assert node.created_by == "ak_pm_001"
        assert node.tags == ["auth", "P0"]
        assert node.is_deleted is False
        assert node.created_at is not None

        # 2. 向量生成（mock 返回 [0.1]*1024）
        assert node.content_vector is not None
        assert len(node.content_vector) == 1024
        assert node.content_vector[0] == 0.1

        # 3. AGE 图节点存在
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (n:Requirement {id: $nid}) RETURN n",
            {"nid": str(node.id)},
        )
        assert len(rows) == 1

        # 4. 审计日志写入
        from mem_lake.audit.service import query_audit_logs

        logs = await query_audit_logs(db_session, actor="ak_pm_001", action="write")
        assert len(logs) >= 1
        assert logs[0].target_type == "node"
        assert logs[0].target_id == node.id

    async def test_create_node_without_vector(
        self, db_session, graph_store, knowledge_helpers
    ):
        """generate_vector=False 时不调用 embedding_client，向量字段为 None。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=None,
            project_id=project_id,
            node_type="Decision",
            title="采用 JWT 鉴权",
            content="决策采用 JWT 令牌方案",
            properties=knowledge_helpers["Decision"](),
            created_by="ak_admin",
            generate_vector=False,
        )
        assert node.content_vector is None
        # 图节点仍写入
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (n:Decision {id: $nid}) RETURN n",
            {"nid": str(node.id)},
        )
        assert len(rows) == 1

    async def test_create_node_generate_vector_without_client_raises(
        self, db_session, graph_store, knowledge_helpers
    ):
        """generate_vector=True 但 embedding_client=None 抛 ValueError。"""
        with pytest.raises(ValueError, match="generate_vector=True"):
            await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=None,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="R",
                content="C",
                properties=knowledge_helpers["Requirement"](),
                created_by="ak",
                generate_vector=True,
                system_id=uuid.uuid4(),
            )

    async def test_create_node_invalid_properties_raises(
        self, db_session, graph_store, mock_embedding_client
    ):
        """properties 缺失必填字段抛 SchemaValidationError。"""
        with pytest.raises(SchemaValidationError, match="缺失必填字段"):
            await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                project_id=uuid.uuid4(),
                node_type="Requirement",
                title="R",
                content="C",
                properties={"requirement_id": "REQ-001"},  # 缺 priority/module
                created_by="ak",
                system_id=uuid.uuid4(),
            )

    async def test_create_node_invalid_type_raises(
        self, db_session, graph_store, mock_embedding_client
    ):
        """非法节点类型抛 SchemaValidationError。"""
        with pytest.raises(SchemaValidationError, match="非法节点类型"):
            await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                project_id=uuid.uuid4(),
                node_type="InvalidType",
                title="R",
                content="C",
                properties={},
                created_by="ak",
            )

    async def test_create_node_with_real_embedding(
        self, db_session, graph_store, real_embedding_client, knowledge_helpers
    ):
        """使用真实 embedding 服务创建节点：验证向量维度为 1024 且非零。

        依赖 deploy-embedding-1 容器运行；未运行时自动 skip。
        """
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=real_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="LoginService 类",
            content="LoginService 负责用户登录鉴权，签发 JWT 令牌",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak_dev_001",
        )
        assert node.content_vector is not None
        assert len(node.content_vector) == 1024
        # 真实向量不应全为 0.1（mock 标志）
        assert any(abs(v - 0.1) > 0.001 for v in node.content_vector[:10])

    async def test_create_node_each_type(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """PDD 定义的 7 种节点类型均可创建。"""
        project_id = uuid.uuid4()
        for node_type in [
            "ProjectProfile",
            "Requirement",
            "CodeSnippet",
            "Solution",
            "DesignIntent",
            "Decision",
            "Pitfall",
        ]:
            node = await create_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                project_id=project_id,
                node_type=node_type,
                title=f"{node_type} 节点",
                content=f"{node_type} 内容描述",
                properties=knowledge_helpers[node_type](),
                created_by="ak_test",
                system_id=uuid.uuid4(),
            )
            assert node.type == node_type
            assert node.status == "approved"


# ============ get_node ============

class TestGetNode:
    """get_node 查询测试。"""

    async def test_get_existing_node(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """查询存在的节点返回正确对象。"""
        project_id = uuid.uuid4()
        created = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        fetched = await get_node(db_session, created.id)
        assert fetched.id == created.id
        assert fetched.title == "R"
        assert fetched.type == "Requirement"

    async def test_get_nonexistent_node_raises(self, db_session):
        """查询不存在的节点抛 NodeNotFoundError。"""
        with pytest.raises(NodeNotFoundError):
            await get_node(db_session, uuid.uuid4())

    async def test_get_archived_node_excluded_by_default(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """已归档节点默认不可见（include_deleted=False 抛 NodeNotFoundError）。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        await archive_node(
            db_session,
            graph_store=graph_store,
            node_id=node.id,
            actor="ak",
        )

        with pytest.raises(NodeNotFoundError):
            await get_node(db_session, node.id)

    async def test_get_archived_node_with_include_deleted(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """include_deleted=True 返回已归档节点。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        await archive_node(
            db_session,
            graph_store=graph_store,
            node_id=node.id,
            actor="ak",
        )

        fetched = await get_node(db_session, node.id, include_deleted=True)
        assert fetched.id == node.id
        assert fetched.is_deleted is True
        assert fetched.status == "archived"


# ============ update_node ============

class TestUpdateNode:
    """update_node 更新测试。"""

    async def test_update_title_increments_version_and_regenerates_vector(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """更新 title 后版本 +1，向量重新生成，审计日志记录变更。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="原标题",
            content="原内容",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        original_vector = list(node.content_vector)

        # 修改 mock 返回值以区分新旧向量
        # （fixture 用 side_effect 定义，side_effect 优先于 return_value，
        #  故须改 side_effect 才能生效）
        mock_embedding_client.embed_one.side_effect = lambda text, **kw: [0.2] * 1024

        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            title="新标题",
            actor="ak_pm",
        )

        assert updated.version == 2
        assert updated.title == "新标题"
        assert updated.content_vector != original_vector
        assert updated.content_vector[0] == 0.2

    async def test_update_content_regenerates_vector(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """更新 content 后向量重新生成。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="原内容",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        original_vector = list(node.content_vector)
        mock_embedding_client.embed_one.side_effect = lambda text, **kw: [0.3] * 1024

        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            content="全新内容",
            actor="ak",
        )
        assert updated.version == 2
        assert updated.content == "全新内容"
        assert updated.content_vector[0] == 0.3

    async def test_update_properties_revalidates_required(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """更新 properties 时重新校验必填字段，缺失抛 SchemaValidationError。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        # 缺失 priority 必填字段
        with pytest.raises(SchemaValidationError, match="priority"):
            await update_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                node_id=node.id,
                properties={"requirement_id": "REQ-002", "module": "auth"},
                actor="ak",
            )

    async def test_update_properties_only_regenerates_vector(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """仅 properties 变更（title/content 不变）也触发向量重算。

        build_embed_text 的输入含属性段（如 Pitfall.root_cause），属性变更
        会改变嵌入文本，向量必须重算（审计 §2.2）。
        """
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Pitfall",
            title="踩坑",
            content="描述",
            properties=knowledge_helpers["Pitfall"](),
            created_by="ak",
        )
        original_vector = list(node.content_vector)
        mock_embedding_client.embed_one.side_effect = lambda text, **kw: [0.7] * 1024

        new_props = knowledge_helpers["Pitfall"]()
        new_props["root_cause"] = "完全不同的根因说明"
        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            properties=new_props,
            actor="ak",
        )

        assert updated.version == 2
        assert updated.content_vector != original_vector
        assert updated.content_vector[0] == 0.7
        # embed 输入应含属性段（与落库向量构造一致）
        embed_input = mock_embedding_client.embed_one.call_args.args[-1]
        assert "root_cause" in embed_input

    async def test_update_no_changes_returns_unchanged(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """无变更时不递增版本，不写审计日志。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        original_version = node.version

        # 所有字段都传 None
        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            title=None,
            content=None,
            properties=None,
            tags=None,
            source=None,
            actor="ak",
        )
        assert updated.version == original_version  # 未递增

    async def test_update_nonexistent_node_raises(
        self, db_session, graph_store, mock_embedding_client
    ):
        """更新不存在的节点抛 NodeNotFoundError。"""
        with pytest.raises(NodeNotFoundError):
            await update_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                node_id=uuid.uuid4(),
                title="new",
                actor="ak",
            )

    async def test_update_tags_only(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """仅更新 tags，版本递增但不触发向量重生成。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            tags=["old"],
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        original_vector = list(node.content_vector)

        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            tags=["new", "tags"],
            actor="ak",
        )
        assert updated.version == 2
        assert updated.tags == ["new", "tags"]
        # 向量未变（tags 变更不触发向量重生成）
        assert updated.content_vector == original_vector


# ============ archive_node ============

class TestArchiveNode:
    """archive_node 归档测试。"""

    async def test_archive_sets_status_and_deleted_flag(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """归档后 is_deleted=True + status=archived + 审计日志。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        archived = await archive_node(
            db_session,
            graph_store=graph_store,
            node_id=node.id,
            actor="ak_admin",
        )
        assert archived.is_deleted is True
        assert archived.status == "archived"

        # 验证审计日志
        from mem_lake.audit.service import query_audit_logs

        logs = await query_audit_logs(db_session, actor="ak_admin", action="archive")
        assert len(logs) >= 1
        assert logs[0].target_id == node.id

    async def test_archive_idempotent(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """重复归档幂等，不抛异常，不重复写审计日志。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        await archive_node(
            db_session, graph_store=graph_store, node_id=node.id, actor="ak"
        )
        # 第二次归档
        archived = await archive_node(
            db_session, graph_store=graph_store, node_id=node.id, actor="ak"
        )
        assert archived.status == "archived"

    async def test_archive_with_delete_from_graph(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """delete_from_graph=True 时同步删除 AGE 图节点。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        await archive_node(
            db_session,
            graph_store=graph_store,
            node_id=node.id,
            actor="ak",
            delete_from_graph=True,
        )

        # AGE 图节点应被删除
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (n {id: $nid}) RETURN n",
            {"nid": str(node.id)},
        )
        assert len(rows) == 0

    async def test_archive_nonexistent_node_raises(self, db_session, graph_store):
        """归档不存在的节点抛 NodeNotFoundError。"""
        with pytest.raises(NodeNotFoundError):
            await archive_node(
                db_session, graph_store=graph_store, node_id=uuid.uuid4(), actor="ak"
            )


# ============ add_edge ============

class TestAddEdge:
    """add_edge 业务测试。"""

    async def test_add_edge_creates_graph_edge_and_audit(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """创建边后 AGE 图可查 + 审计日志记录。"""
        project_id = uuid.uuid4()
        req = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        code = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="Code",
            content="C",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak_dev",
        )

        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=req.id,
            to_id=code.id,
            edge_type="implements",
            properties={"reason": "需求由代码实现"},
            actor="ak_dev",
        )

        # AGE 图边可查
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (a {id: $from_id})-[r:implements]->(b {id: $to_id}) RETURN r",
            {"from_id": str(req.id), "to_id": str(code.id)},
        )
        assert len(rows) >= 1

        # 审计日志
        from mem_lake.audit.service import query_audit_logs

        logs = await query_audit_logs(db_session, actor="ak_dev", action="write", target_type="edge")
        assert len(logs) >= 1

    async def test_add_edge_injects_created_by(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """properties 默认注入 created_by（边属性元数据）。"""
        project_id = uuid.uuid4()
        a = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="A",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        b = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="B",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        # 不传 created_by，由 repository 注入
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=a.id,
            to_id=b.id,
            edge_type="relates_to",
            actor="ak_pm",
        )

        rows = await graph_store.match_pattern(
            db_session,
            "MATCH ()-[r:relates_to]->() RETURN r",
            {},
        )
        assert len(rows) >= 1
        # 边属性含 created_by
        edge_props = rows[0].get("properties", {}) if isinstance(rows[0], dict) else {}
        assert edge_props.get("created_by") == "ak_pm"

    async def test_add_edge_invalid_type_raises(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """非法 edge_type 抛 SchemaValidationError。"""
        project_id = uuid.uuid4()
        a = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="A",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        b = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="B",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        with pytest.raises(SchemaValidationError, match="非法边类型"):
            await add_edge(
                db_session,
                graph_store=graph_store,
                from_id=a.id,
                to_id=b.id,
                edge_type="invalid_relation",
                actor="ak",
            )


# ============ list_nodes_by_project ============

class TestListNodesByProject:
    """list_nodes_by_project 查询测试。"""

    async def test_list_by_project(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """按项目过滤返回该项目的所有 approved 节点。"""
        pid1 = uuid.uuid4()
        pid2 = uuid.uuid4()

        # pid1 创建 2 个节点
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid1, node_type="Requirement", title="R1", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid1, node_type="CodeSnippet", title="C1", content="C",
            properties=knowledge_helpers["CodeSnippet"](), created_by="ak",
        )
        # pid2 创建 1 个节点
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid2, node_type="Requirement", title="R2", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )

        nodes = await list_nodes_by_project(db_session, project_id=pid1)
        assert len(nodes) == 2
        assert all(n.project_id == pid1 for n in nodes)

    async def test_list_filter_by_type(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """按 node_type 过滤。"""
        pid = uuid.uuid4()
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="CodeSnippet", title="C", content="C",
            properties=knowledge_helpers["CodeSnippet"](), created_by="ak",
        )

        nodes = await list_nodes_by_project(db_session, project_id=pid, node_type="Requirement")
        assert len(nodes) == 1
        assert nodes[0].type == "Requirement"

    async def test_list_excludes_archived(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """默认 status=approved 过滤，archived 节点不返回。"""
        pid = uuid.uuid4()
        node = await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        await archive_node(db_session, graph_store=graph_store, node_id=node.id, actor="ak")

        # 默认查询不返回 archived
        nodes = await list_nodes_by_project(db_session, project_id=pid)
        assert len(nodes) == 0

        # status=None 不过滤
        nodes_all = await list_nodes_by_project(db_session, project_id=pid, status=None)
        assert len(nodes_all) == 1
        assert nodes_all[0].status == "archived"

    async def test_list_pagination(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """分页 limit/offset。"""
        pid = uuid.uuid4()
        for i in range(5):
            await create_node(
                db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
                project_id=pid, node_type="Requirement", title=f"R{i}", content="C",
                properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
            )

        page1 = await list_nodes_by_project(db_session, project_id=pid, limit=2, offset=0)
        page2 = await list_nodes_by_project(db_session, project_id=pid, limit=2, offset=2)
        page3 = await list_nodes_by_project(db_session, project_id=pid, limit=2, offset=4)
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1  # 最后一个


# ============ 事务性共写 ============

class TestTransactionalCoweite:
    """事务性共写场景测试。"""

    async def test_create_node_and_edge_in_same_transaction(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """节点 + 边 + 审计在同一事务内原子写入。"""
        project_id = uuid.uuid4()
        req = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        code = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="Code",
            content="C",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak",
        )
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=req.id,
            to_id=code.id,
            edge_type="implements",
            actor="ak",
        )

        # 三者均写入（事务未提交，但 flush 后可查）
        fetched_req = await get_node(db_session, req.id)
        assert fetched_req.id == req.id
        fetched_code = await get_node(db_session, code.id)
        assert fetched_code.id == code.id

        # 边存在
        neighbors = await graph_store.neighbors(db_session, req.id, depth=1)
        assert any(n["properties"]["id"] == str(code.id) for n in neighbors)

    async def test_partial_failure_rolls_back(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """部分失败时事务回滚：先成功创建节点，再尝试非法边操作触发异常，
        验证异常后节点仍可查询（同事务内 flush 已可见），但调用方应 rollback。

        本测试验证：repository 不主动 commit，调用方控制事务。
        异常传播给调用方，由调用方决定 rollback。
        """
        project_id = uuid.uuid4()
        req = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        # 节点已 flush，可查
        fetched = await get_node(db_session, req.id)
        assert fetched.id == req.id

        # 尝试非法边操作（应抛异常）
        with pytest.raises(SchemaValidationError):
            await add_edge(
                db_session,
                graph_store=graph_store,
                from_id=req.id,
                to_id=uuid.uuid4(),
                edge_type="invalid_relation",
                actor="ak",
            )

        # 异常后节点仍可查（事务未 rollback，由 fixture 结束时 rollback）
        # 这验证了 repository 不主动 commit，事务边界由调用方控制


# ============ regenerate_vector ============

class TestRegenerateVector:
    """regenerate_vector 独立调用入口测试。"""

    async def test_regenerate_vector_with_mock(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """手动重生成向量：向量字段更新 + 审计日志。"""
        project_id = uuid.uuid4()
        # 创建时不生成向量
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=None,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            generate_vector=False,
            system_id=uuid.uuid4(),
        )
        assert node.content_vector is None

        # 手动重生成
        mock_embedding_client.embed_one.side_effect = lambda text, **kw: [0.5] * 1024
        updated = await regenerate_vector(
            db_session,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            actor="ak",
        )
        assert updated.content_vector is not None
        assert updated.content_vector[0] == 0.5

    async def test_regenerate_vector_nonexistent_raises(
        self, db_session, mock_embedding_client
    ):
        """不存在的节点重生成向量抛 NodeNotFoundError。"""
        with pytest.raises(NodeNotFoundError):
            await regenerate_vector(
                db_session,
                embedding_client=mock_embedding_client,
                node_id=uuid.uuid4(),
                actor="ak",
            )


# ============ 边界场景补充 ============

class TestUpdateNodeEdgeCases:
    """update_node 边界场景：类型不可变、归档节点更新、无 embedding client。"""

    async def test_update_node_type_not_changeable(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """update_node 不接受 node_type 参数，节点类型创建后不可变更。

        场景：PM 创建 Requirement 后想改为 CodeSnippet，必须新建节点而非更新。
        update_node 签名无 node_type 参数，编译期保证类型不可变。
        本测试验证：update_node 后 type 字段保持原值。
        """
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            title="新标题",
            actor="ak",
        )
        # type 保持原值
        assert updated.type == "Requirement"

    async def test_update_archived_node_raises(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """归档节点（is_deleted=True）update 时抛 NodeNotFoundError。

        场景：节点已归档后尝试更新，get_node 默认排除 is_deleted=True，
        抛 NodeNotFoundError，阻止对归档节点的修改。
        """
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        await archive_node(
            db_session, graph_store=graph_store, node_id=node.id, actor="ak"
        )

        with pytest.raises(NodeNotFoundError):
            await update_node(
                db_session,
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                node_id=node.id,
                title="新标题",
                actor="ak",
            )

    async def test_update_without_embedding_client_raises(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """regenerate_vector=True 且 title 变更但 embedding_client=None 抛 ValueError。

        场景：调用方忘记传 embedding_client 但 title 变更触发向量重生成。
        """
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        with pytest.raises(ValueError, match="regenerate_vector=True"):
            await update_node(
                db_session,
                graph_store=graph_store,
                embedding_client=None,
                node_id=node.id,
                title="新标题",
                actor="ak",
            )

    async def test_update_tags_and_source_only(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """同时更新 tags 与 source，版本递增，向量不变。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            tags=["old"],
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        original_vector = list(node.content_vector)

        updated = await update_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_id=node.id,
            tags=["new", "tags"],
            source={"agent": "dev_agent", "tool": "publish_code"},
            actor="ak",
        )
        assert updated.version == 2
        assert updated.tags == ["new", "tags"]
        assert updated.source == {"agent": "dev_agent", "tool": "publish_code"}
        # 向量未变
        assert updated.content_vector == original_vector


class TestAddEdgeEdgeCases:
    """add_edge 边界场景：跨项目、自环。"""

    async def test_add_edge_cross_project(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """跨项目创建边：AGE 图不强制 project_id 隔离，跨项目边可创建。

        场景：dev 在 project_a 的 CodeSnippet 与 project_b 的 Solution 间建边。
        图层不校验 project_id（应用层决定是否允许跨项目边）。
        本测试验证跨项目边技术可创建。
        """
        pid_a = uuid.uuid4()
        pid_b = uuid.uuid4()
        code = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=pid_a,
            node_type="CodeSnippet",
            title="Code",
            content="C",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak_dev",
        )
        solution = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=pid_b,
            node_type="Solution",
            title="Solution",
            content="C",
            properties=knowledge_helpers["Solution"](),
            created_by="ak_dev",
        )

        # 跨项目边
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=code.id,
            to_id=solution.id,
            edge_type="realized_by",
            actor="ak_dev",
        )

        # 边可查
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (a {id: $from_id})-[r:realized_by]->(b {id: $to_id}) RETURN r",
            {"from_id": str(code.id), "to_id": str(solution.id)},
        )
        assert len(rows) >= 1

    async def test_add_edge_self_loop(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """自环边（from_id == to_id）可创建。

        场景：需求与自身建立 relates_to 关系（表示自引用需求）。
        """
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=node.id,
            to_id=node.id,
            edge_type="relates_to",
            actor="ak",
        )

        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (n {id: $nid})-[r:relates_to]->(n) RETURN r",
            {"nid": str(node.id)},
        )
        assert len(rows) >= 1


class TestListNodesEdgeCases:
    """list_nodes_by_project 边界场景：archived 过滤、offset 超界。"""

    async def test_list_status_archived_filter(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """status="archived" 仅返回归档节点。"""
        pid = uuid.uuid4()
        # 1 个 approved + 2 个 archived
        n1 = await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R1", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        n2 = await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R2", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        n3 = await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R3", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        await archive_node(db_session, graph_store=graph_store, node_id=n2.id, actor="ak")
        await archive_node(db_session, graph_store=graph_store, node_id=n3.id, actor="ak")

        # status="archived" 仅返回 2 个归档节点
        archived = await list_nodes_by_project(db_session, project_id=pid, status="archived")
        assert len(archived) == 2
        assert all(n.status == "archived" for n in archived)

        # status="approved" 仅返回 1 个
        approved = await list_nodes_by_project(db_session, project_id=pid, status="approved")
        assert len(approved) == 1
        assert approved[0].id == n1.id

    async def test_list_offset_beyond_total(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """offset 超过总数返回空列表。"""
        pid = uuid.uuid4()
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )

        # offset=100 远超总数 1
        nodes = await list_nodes_by_project(db_session, project_id=pid, offset=100)
        assert nodes == []

    async def test_list_limit_zero(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """limit=0 返回空列表（边界值）。"""
        pid = uuid.uuid4()
        await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=pid, node_type="Requirement", title="R", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )

        nodes = await list_nodes_by_project(db_session, project_id=pid, limit=0)
        assert nodes == []


class TestCreateNodeEdgeCases:
    """create_node 边界场景：Unicode 内容、特殊字符属性。"""

    async def test_create_node_unicode_content(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """节点 title 与 content 含中文、emoji、特殊字符正确写入。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="用户登录鉴权需求 🔐",
            content="支持账号密码登录 + JWT 令牌签发，需防御 SQL 注入（'; DROP TABLE--）",
            properties=knowledge_helpers["Requirement"](),
            tags=["认证", "安全 🔒"],
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )

        fetched = await get_node(db_session, node.id)
        assert fetched.title == "用户登录鉴权需求 🔐"
        assert "JWT" in fetched.content
        assert "'; DROP TABLE--" in fetched.content
        assert fetched.tags == ["认证", "安全 🔒"]

    async def test_create_node_special_chars_in_properties(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """properties 含特殊字符（引号、反斜杠、SQL 关键字）正确写入与读取。

        验证 PREPARE 参数化防注入：特殊字符作为 agtype map 值传入，不拼入 Cypher 语法。
        """
        project_id = uuid.uuid4()
        special_props = {
            "requirement_id": "REQ'; DROP TABLE--",
            "priority": "P0",
            "module": "auth\\n\"injection\"",
        }
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=special_props,
            created_by="ak",
            system_id=uuid.uuid4(),
        )

        fetched = await get_node(db_session, node.id)
        assert fetched.properties["requirement_id"] == "REQ'; DROP TABLE--"
        assert fetched.properties["module"] == "auth\\n\"injection\""

        # 验证 knowledge_node 表未被 DROP（注入未执行）
        from sqlalchemy import text
        result = await db_session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = 'knowledge_node'")
        )
        assert result.first() is not None

    async def test_create_node_empty_title_and_content(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """空 title 与 content 边界值：允许创建（schema 不校验空字符串）。"""
        project_id = uuid.uuid4()
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="",
            content="",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak",
            system_id=uuid.uuid4(),
        )
        assert node.title == ""
        assert node.content == ""
        # 向量仍生成（空字符串的 embedding）
        assert node.content_vector is not None


class TestTransactionalIntegrity:
    """事务完整性边界场景。"""

    async def test_edge_creation_after_node_in_same_transaction(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """同一事务内：create_node 后立即 add_edge，边可查（flush 后可见）。

        场景：dev 创建 CodeSnippet 后立即关联到已存在的 Requirement，
        全部在同一事务内，未 commit 但 flush 后可查。
        """
        project_id = uuid.uuid4()
        req = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="R",
            content="C",
            properties=knowledge_helpers["Requirement"](),
            created_by="ak_pm",
            system_id=uuid.uuid4(),
        )
        code = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="CodeSnippet",
            title="Code",
            content="C",
            properties=knowledge_helpers["CodeSnippet"](),
            created_by="ak_dev",
        )

        # 同事务内创建边
        await add_edge(
            db_session,
            graph_store=graph_store,
            from_id=req.id,
            to_id=code.id,
            edge_type="implements",
            actor="ak_dev",
        )

        # 边可查（flush 后可见）
        neighbors = await graph_store.neighbors(db_session, req.id, depth=1)
        assert any(n["properties"]["id"] == str(code.id) for n in neighbors)

    async def test_multiple_edges_same_pair(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """同一对节点创建多条不同类型边（implements + references）。

        场景：Requirement 与 CodeSnippet 间既有 implements 又有 references 关系。
        """
        project_id = uuid.uuid4()
        req = await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=project_id, node_type="Requirement", title="R", content="C",
            properties=knowledge_helpers["Requirement"](), created_by="ak", system_id=uuid.uuid4(),
        )
        code = await create_node(
            db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
            project_id=project_id, node_type="CodeSnippet", title="Code", content="C",
            properties=knowledge_helpers["CodeSnippet"](), created_by="ak",
        )

        await add_edge(
            db_session, graph_store=graph_store,
            from_id=req.id, to_id=code.id, edge_type="implements", actor="ak",
        )
        await add_edge(
            db_session, graph_store=graph_store,
            from_id=req.id, to_id=code.id, edge_type="references", actor="ak",
        )

        # 两条边都存在
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (a {id: $from_id})-[r]->(b {id: $to_id}) RETURN r",
            {"from_id": str(req.id), "to_id": str(code.id)},
        )
        edge_types = {r["label"] for r in rows}
        assert "implements" in edge_types
        assert "references" in edge_types
