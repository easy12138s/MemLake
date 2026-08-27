"""审批流集成测试：按实际调用场景验证 submit_batch → review_approve/review_reject。

覆盖 PDD 3.4 审批工作流核心场景：
1. submit_batch：节点/边批次提交、items 持久化、summary 自动生成、幂等键重放
2. review_approve：原子写入 knowledge_node + AGE 图、向量延迟生成、target_id 回填、
   conflict_hint 生成、状态转换、审计日志、事务回滚
3. review_reject：不写入正式存储、items 保留、状态转换、审计日志
4. 冲突检测：标题相似、标签匹配、跨项目/跨类型隔离、阈值边界
5. 查询场景：list_pending_batches、get_batch_detail、不存在批次抛错
6. 边界场景：已审批批次重复操作、空批次、update action、edge from_id 不存在

事务回滚隔离：db_session fixture 结束 rollback，不影响其他测试。
依赖真实 embedding 容器（localhost:8001）：conflict 检测需真实向量相似度对比。
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from mem_lake.approval.service import (
    BATCH_TYPES,
    STATUS_APPROVED,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    BatchNotFoundError,
    BatchStatusError,
    PayloadValidationError,
    get_batch_detail,
    list_pending_batches,
    review_approve,
    review_reject,
    submit_batch,
)
from mem_lake.audit.service import query_audit_logs
from mem_lake.knowledge.models import KnowledgeNode, System
from mem_lake.knowledge.repository import create_node

# ============ 辅助函数 ============


async def _create_approved_node_for_conflict(
    db_session,
    graph_store,
    real_embedding_client,
    knowledge_helpers,
    *,
    project_id,
    node_type="Requirement",
    title="用户登录鉴权需求",
    content="系统需要支持账号密码登录与 JWT 令牌签发",
    tags=None,
    system_id=None,
):
    """创建一个已 approved 的节点，用作冲突检测的"已有知识"。

    直接调用 knowledge.repository.create_node 跳过审批流，
    仅用于为冲突检测测试准备数据。
    """
    from mem_lake.knowledge.repository import create_node

    return await create_node(
        db_session,
        graph_store=graph_store,
        embedding_client=real_embedding_client,
        project_id=project_id,
        node_type=node_type,
        title=title,
        content=content,
        properties=knowledge_helpers[node_type](),
        tags=tags or ["auth", "P0"],
        source={"agent": "pm_agent", "tool": "publish_requirement"},
        system_id=system_id or uuid.uuid4(),
        created_by="ak_pm_existing",
    )


# ============ submit_batch 场景 ============


class TestSubmitBatch:
    """submit_batch 业务场景测试。"""

    async def test_submit_batch_with_node_item(
        self, db_session, sample_batch_payloads
    ):
        """提交含 1 个 node item 的批次：验证 batch 创建、items 持久化、status=pending_review。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        # 1. batch 字段
        assert batch.id is not None
        assert batch.project_id == project_id
        assert batch.batch_type == "publish_requirement"
        assert batch.submitted_by == "ak_pm_001"
        assert batch.submitter_role == "pm"
        assert batch.status == STATUS_PENDING_REVIEW
        assert batch.submitted_at is not None
        assert batch.reviewed_by is None
        assert batch.reviewed_at is None
        assert batch.conflict_hint is None

        # 2. summary 自动生成（1 个节点 + 0 个关系）
        assert batch.summary == "1 个节点 + 0 个关系"

        # 3. items 持久化（需重新查询避免懒加载问题）
        batch_detail = await get_batch_detail(db_session, batch.id)
        assert len(batch_detail.items) == 1
        item = batch_detail.items[0]
        assert item.seq == 1
        assert item.item_type == "node"
        assert item.action == "create"
        assert item.entity_type == "Requirement"
        assert item.target_id is None  # 审批前无 target_id
        assert "title" in item.payload
        assert "properties" in item.payload

    async def test_submit_batch_with_edge_item(
        self, db_session, sample_batch_payloads
    ):
        """提交含 edge item 的批次：验证 payload 含 from_id/to_id。"""
        project_id = uuid.uuid4()
        from_id = uuid.uuid4()
        to_id = uuid.uuid4()
        items = sample_batch_payloads["update_requirement_relations"](
            from_id, to_id
        )

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="update_requirement_relations",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        assert batch.summary == "0 个节点 + 1 个关系"
        detail = await get_batch_detail(db_session, batch.id)
        assert len(detail.items) == 1
        edge_item = detail.items[0]
        assert edge_item.item_type == "edge"
        assert edge_item.entity_type == "conflicts_with"
        assert edge_item.payload["from_id"] == str(from_id)
        assert edge_item.payload["to_id"] == str(to_id)

    async def test_submit_batch_summary_mixed_items(
        self, db_session, sample_batch_payloads
    ):
        """提交含 2 个 node + 1 个 edge 的批次：summary 为"2 个节点 + 1 个关系"。"""
        project_id = uuid.uuid4()
        from_id = uuid.uuid4()
        to_id = uuid.uuid4()
        items = sample_batch_payloads["submit_dev_artifacts"](
            project_id, from_id, to_id
        )

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev_001",
            submitter_role="dev",
            items=items,
        )

        assert batch.summary == "2 个节点 + 1 个关系"

    async def test_submit_batch_idempotency_replay(
        self, db_session, sample_batch_payloads
    ):
        """同 operation_id 重复提交返回相同 batch_id（幂等重放）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)
        operation_id = "op-20260802-001"

        batch1 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
            operation_id=operation_id,
        )

        # 第二次提交同 operation_id（即使 items 不同也返回首次 batch）
        items2 = sample_batch_payloads["publish_requirement"](project_id)
        batch2 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items2,
            operation_id=operation_id,
        )

        assert batch1.id == batch2.id

    async def test_submit_batch_invalid_batch_type_raises(
        self, db_session, sample_batch_payloads
    ):
        """非法 batch_type 抛 PayloadValidationError。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        with pytest.raises(PayloadValidationError, match="非法批次类型"):
            await submit_batch(
                db_session,
                project_id=project_id,
                batch_type="invalid_type",
                submitted_by="ak_pm_001",
                submitter_role="pm",
                items=items,
            )

    async def test_submit_batch_empty_items_raises(
        self, db_session
    ):
        """items 为空列表抛 PayloadValidationError。"""
        with pytest.raises(PayloadValidationError, match="items 不能为空"):
            await submit_batch(
                db_session,
                project_id=uuid.uuid4(),
                batch_type="publish_requirement",
                submitted_by="ak_pm_001",
                submitter_role="pm",
                items=[],
            )

    async def test_submit_batch_invalid_payload_missing_properties(
        self, db_session
    ):
        """node+create payload 缺 properties 抛 PayloadValidationError。"""
        project_id = uuid.uuid4()
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Requirement",
                    "system_id": str(uuid.uuid4()),
                    "title": "x",
                    "content": "y",
                    # 缺 properties
                    "created_by": "ak_pm",
                },
            }
        ]
        with pytest.raises(PayloadValidationError, match="缺 properties"):
            await submit_batch(
                db_session,
                project_id=project_id,
                batch_type="publish_requirement",
                submitted_by="ak_pm_001",
                submitter_role="pm",
                items=items,
            )

    async def test_submit_batch_invalid_node_type_raises(
        self, db_session
    ):
        """node+create 的 entity_type 不在 NODE_TYPES 抛 PayloadValidationError。"""
        project_id = uuid.uuid4()
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "InvalidType",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "InvalidType",
                    "title": "x",
                    "content": "y",
                    "properties": {"any": "thing"},
                    "created_by": "ak_pm",
                },
            }
        ]
        with pytest.raises(PayloadValidationError, match="node\\+create 校验失败"):
            await submit_batch(
                db_session,
                project_id=project_id,
                batch_type="publish_requirement",
                submitted_by="ak_pm_001",
                submitter_role="pm",
                items=items,
            )

    async def test_submit_batch_invalid_edge_type_raises(
        self, db_session
    ):
        """edge+create 的 entity_type 不在 EDGE_TYPES 抛 PayloadValidationError。"""
        project_id = uuid.uuid4()
        items = [
            {
                "item_type": "edge",
                "action": "create",
                "entity_type": "invalid_edge",
                "payload": {
                    "from_id": str(uuid.uuid4()),
                    "to_id": str(uuid.uuid4()),
                },
            }
        ]
        with pytest.raises(PayloadValidationError, match="edge\\+create 校验失败"):
            await submit_batch(
                db_session,
                project_id=project_id,
                batch_type="update_requirement_relations",
                submitted_by="ak_pm_001",
                submitter_role="pm",
                items=items,
            )

    async def test_submit_batch_records_audit_log(
        self, db_session, sample_batch_payloads
    ):
        """submit_batch 写审计日志（action=submit, target_type=batch）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
            operation_id="op-audit-001",
        )

        logs = await query_audit_logs(
            db_session, actor="ak_pm_001", action="submit", target_id=batch.id
        )
        assert len(logs) >= 1
        log = logs[0]
        assert log.target_type == "batch"
        assert log.operation_id == "op-audit-001"
        assert log.detail["batch_type"] == "publish_requirement"
        assert log.detail["item_count"] == 1


# ============ review_approve 场景 ============


class TestReviewApprove:
    """review_approve 业务场景测试：原子写入 + 向量生成 + target_id 回填 + conflict_hint。"""

    async def test_review_approve_writes_node_to_knowledge_table(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """审批通过后 node item 写入 knowledge_node 表（status=approved）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        approved_batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        # 1. 批次状态转换
        assert approved_batch.status == STATUS_APPROVED
        assert approved_batch.reviewed_by == "ak_admin_001"
        assert approved_batch.reviewed_at is not None

        # 2. knowledge_node 表写入
        stmt = select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        result = await db_session.execute(stmt)
        nodes = list(result.scalars().all())
        assert len(nodes) == 1
        node = nodes[0]
        assert node.type == "Requirement"
        assert node.title == "用户登录鉴权需求"
        assert node.status == "approved"
        assert node.is_deleted is False

        # 3. approval_item.target_id 回填
        detail = await get_batch_detail(db_session, batch.id)
        assert detail.items[0].target_id == node.id

    async def test_review_approve_defers_vector(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """审批通过时向量延迟生成：节点先以 NULL 向量落库，由后台异步补向量。

        同步审批路径不再阻塞于 embedding（避免大批次超时）；content_vector 在
        审批返回时为 NULL，搜索已能安全跳过 NULL，后台 worker 随后填充。
        """
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        stmt = select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        result = await db_session.execute(stmt)
        node = result.scalar_one()
        # 同步审批路径不再 embed：向量暂为 NULL（异步补向量在审批返回之后）
        assert node.content_vector is None

    async def test_review_approve_writes_edge_to_age_graph(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
        knowledge_helpers,
    ):
        """审批通过后 edge item 写入 AGE 图。"""
        project_id = uuid.uuid4()
        # 先创建两个已 approved 节点（from_id / to_id）
        from mem_lake.knowledge.repository import create_node

        node1 = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求A",
            content="需求A 内容",
            properties=knowledge_helpers["Requirement"](),
            tags=["auth"],
            system_id=uuid.uuid4(),
            created_by="ak_pm",
        )
        node2 = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="需求B",
            content="需求B 内容",
            properties=knowledge_helpers["Requirement"](),
            tags=["auth"],
            system_id=uuid.uuid4(),
            created_by="ak_pm",
        )

        items = sample_batch_payloads["update_requirement_relations"](
            node1.id, node2.id
        )
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="update_requirement_relations",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        # 验证 AGE 图中存在 conflicts_with 边
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (a)-[r:conflicts_with]->(b) "
            "WHERE a.id = $from_id AND b.id = $to_id "
            "RETURN r",
            {"from_id": str(node1.id), "to_id": str(node2.id)},
        )
        assert len(rows) == 1

    async def test_review_approve_atomic_transaction_rollback(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """部分失败整体回滚：edge from_id 不存在抛 NodeNotFoundError，触发事务回滚。

        PDD 3.4 硬约束：edge item 的 from_id/to_id 必须存在，否则抛异常触发
        事务回滚。AGE CREATE edge 在 MATCH 失败时静默跳过不抛错（Cypher 标准
        行为），因此 service 层必须显式校验节点存在性。
        """
        from mem_lake.knowledge.repository import NodeNotFoundError

        project_id = uuid.uuid4()
        # from_id 与 to_id 都是不存在的 UUID（未写入 knowledge_node）
        items = sample_batch_payloads["update_requirement_relations"](
            uuid.uuid4(), uuid.uuid4()
        )

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="update_requirement_relations",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        # 期望：review_approve 抛 NodeNotFoundError（from_id 节点不存在）
        with pytest.raises(NodeNotFoundError):
            await review_approve(
                db_session,
                batch_id=batch.id,
                reviewed_by="ak_admin_001",
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                vector_searcher=vector_searcher_mock,
            )

        # 由于 SQLAlchemy session 在异常后状态可能不一致，db_session fixture
        # 的事务隔离会在测试结束时整体 rollback，这里主要验证抛异常即可。

    async def test_review_approve_already_approved_raises(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """已 approved 的批次再次审批抛 BatchStatusError。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        # 第一次审批通过
        await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        # 第二次审批抛 BatchStatusError
        with pytest.raises(BatchStatusError, match="状态不允许审批通过"):
            await review_approve(
                db_session,
                batch_id=batch.id,
                reviewed_by="ak_admin_001",
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                vector_searcher=vector_searcher_mock,
            )

    async def test_review_approve_records_audit_log(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """审批通过记录审计日志（action=approve, target_type=batch）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        logs = await query_audit_logs(
            db_session, actor="ak_admin_001", action="approve", target_id=batch.id
        )
        assert len(logs) >= 1
        log = logs[0]
        assert log.target_type == "batch"
        assert log.detail["batch_type"] == "publish_requirement"
        assert "conflict_detected" in log.detail

    async def test_review_approve_conflict_hint_generated(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """审批通过时生成 conflict_hint（含 has_conflict/suggestion/details 结构）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        approved_batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        # conflict_hint 必须生成（即使无冲突也有默认结构）
        assert approved_batch.conflict_hint is not None
        assert "has_conflict" in approved_batch.conflict_hint
        assert "nodes_with_conflict" in approved_batch.conflict_hint
        assert "details" in approved_batch.conflict_hint
        assert "suggestion" in approved_batch.conflict_hint

    async def test_review_approve_nonexistent_batch_raises(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
    ):
        """审批不存在的批次抛 BatchNotFoundError。"""
        with pytest.raises(BatchNotFoundError, match="批次不存在"):
            await review_approve(
                db_session,
                batch_id=uuid.uuid4(),
                reviewed_by="ak_admin_001",
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                vector_searcher=vector_searcher_mock,
            )


# ============ review_reject 场景 ============


class TestReviewReject:
    """review_reject 业务场景测试。"""

    async def test_review_reject_no_write_to_knowledge(
        self,
        db_session,
        sample_batch_payloads,
    ):
        """拒绝后不写入 knowledge_node 表。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            review_comment="需求描述不清晰，请补充验收标准",
        )

        # knowledge_node 表无写入
        stmt = select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        result = await db_session.execute(stmt)
        nodes = list(result.scalars().all())
        assert len(nodes) == 0

    async def test_review_reject_status_transition(
        self,
        db_session,
        sample_batch_payloads,
    ):
        """拒绝后 status=rejected，reviewed_by/reviewed_at/review_comment 填充。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        rejected_batch = await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            review_comment="需求描述不清晰",
        )

        assert rejected_batch.status == STATUS_REJECTED
        assert rejected_batch.reviewed_by == "ak_admin_001"
        assert rejected_batch.reviewed_at is not None
        assert rejected_batch.review_comment == "需求描述不清晰"

    async def test_review_reject_preserves_items(
        self,
        db_session,
        sample_batch_payloads,
    ):
        """拒绝后 approval_items 保留用于追溯（PDD 3.4）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            review_comment="不接受",
        )

        detail = await get_batch_detail(db_session, batch.id)
        assert len(detail.items) == 1
        assert detail.items[0].payload["title"] == "用户登录鉴权需求"

    async def test_review_reject_already_rejected_raises(
        self,
        db_session,
        sample_batch_payloads,
    ):
        """已 rejected 的批次再次操作抛 BatchStatusError。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            review_comment="第一次拒绝",
        )

        with pytest.raises(BatchStatusError, match="状态不允许审批拒绝"):
            await review_reject(
                db_session,
                batch_id=batch.id,
                reviewed_by="ak_admin_001",
                review_comment="第二次拒绝",
            )

    async def test_review_reject_approved_batch_raises(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """已 approved 的批次不能 reject（终态不可逆）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
        )

        with pytest.raises(BatchStatusError, match="状态不允许审批拒绝"):
            await review_reject(
                db_session,
                batch_id=batch.id,
                reviewed_by="ak_admin_001",
                review_comment="尝试拒绝已通过批次",
            )

    async def test_review_reject_records_audit_log(
        self,
        db_session,
        sample_batch_payloads,
    ):
        """拒绝记录审计日志（action=reject, target_type=batch）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            review_comment="拒绝原因",
        )

        logs = await query_audit_logs(
            db_session, actor="ak_admin_001", action="reject", target_id=batch.id
        )
        assert len(logs) >= 1
        log = logs[0]
        assert log.target_type == "batch"
        assert log.detail["review_comment"] == "拒绝原因"

    async def test_review_reject_nonexistent_batch_raises(self, db_session):
        """拒绝不存在的批次抛 BatchNotFoundError。"""
        with pytest.raises(BatchNotFoundError, match="批次不存在"):
            await review_reject(
                db_session,
                batch_id=uuid.uuid4(),
                reviewed_by="ak_admin_001",
                review_comment="x",
            )


# ============ 查询场景 ============


class TestQueryBatches:
    """list_pending_batches + get_batch_detail 查询接口测试。"""

    async def test_list_pending_batches_returns_pending_only(
        self, db_session, sample_batch_payloads
    ):
        """list_pending_batches 仅返回 status=pending_review 的批次。"""
        project_id = uuid.uuid4()

        # 创建 2 个 pending 批次
        items1 = sample_batch_payloads["publish_requirement"](project_id)
        batch1 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items1,
        )

        items2 = sample_batch_payloads["publish_requirement"](project_id)
        batch2 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_002",
            submitter_role="pm",
            items=items2,
        )

        # 创建 1 个 rejected 批次（不应出现在 pending 列表）
        items3 = sample_batch_payloads["publish_requirement"](project_id)
        batch3 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_003",
            submitter_role="pm",
            items=items3,
        )
        await review_reject(
            db_session,
            batch_id=batch3.id,
            reviewed_by="ak_admin",
            review_comment="x",
        )

        pending = await list_pending_batches(db_session, project_id=project_id)
        pending_ids = {b.id for b in pending}
        assert batch1.id in pending_ids
        assert batch2.id in pending_ids
        assert batch3.id not in pending_ids

    async def test_list_pending_batches_filter_by_project(
        self, db_session, sample_batch_payloads
    ):
        """按 project_id 过滤待审批批次。"""
        project1 = uuid.uuid4()
        project2 = uuid.uuid4()

        items1 = sample_batch_payloads["publish_requirement"](project1)
        await submit_batch(
            db_session,
            project_id=project1,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items1,
        )

        items2 = sample_batch_payloads["publish_requirement"](project2)
        batch_p2 = await submit_batch(
            db_session,
            project_id=project2,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items2,
        )

        # 仅查 project2 的待审批
        pending_p2 = await list_pending_batches(db_session, project_id=project2)
        assert len(pending_p2) == 1
        assert pending_p2[0].id == batch_p2.id

    async def test_list_pending_batches_empty(self, db_session):
        """无待审批批次时返回空列表。"""
        pending = await list_pending_batches(db_session, project_id=uuid.uuid4())
        assert pending == []

    async def test_get_batch_detail_includes_items(
        self, db_session, sample_batch_payloads
    ):
        """get_batch_detail 返回批次含 items（预加载）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["submit_dev_artifacts"](
            project_id, uuid.uuid4(), uuid.uuid4()
        )
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev_001",
            submitter_role="dev",
            items=items,
        )

        detail = await get_batch_detail(db_session, batch.id)
        assert detail.id == batch.id
        assert len(detail.items) == 3  # 2 node + 1 edge
        # items 按 seq 排序
        seqs = [item.seq for item in detail.items]
        assert seqs == [1, 2, 3]

    async def test_get_batch_detail_nonexistent_raises(self, db_session):
        """查询不存在批次抛 BatchNotFoundError。"""
        with pytest.raises(BatchNotFoundError, match="批次不存在"):
            await get_batch_detail(db_session, uuid.uuid4())


# ============ 冲突检测场景（依赖真实 embedding） ============


class TestConflictDetection:
    """冲突检测集成测试。

    依赖真实 embedding 容器（localhost:8001）以生成真实向量对比相似度。
    """

    async def test_conflict_detection_duplicate_content(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        vector_searcher,
        knowledge_helpers,
        sample_batch_payloads,
    ):
        """内容级重复（相同内容 + 相同 requirement_id）的两节点，conflict_hint 含 conflicting_nodes。"""
        project_id = uuid.uuid4()
        system_id = uuid.uuid4()

        # 先创建一个已 approved 的"用户登录鉴权需求"节点
        await _create_approved_node_for_conflict(
            db_session,
            graph_store,
            real_embedding_client,
            knowledge_helpers,
            project_id=project_id,
            system_id=system_id,
            title="用户登录鉴权需求",
            content="系统需要支持账号密码登录与 JWT 令牌签发",
        )

        # 提交一个内容完全相同的批次（内容级重复）
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Requirement",
                    "system_id": str(system_id),
                    "title": "用户登录鉴权需求",  # 完全相同标题
                    "content": "系统需要支持账号密码登录与 JWT 令牌签发",
                    "properties": knowledge_helpers["Requirement"](),
                    "tags": ["auth", "P0"],
                    "source": {"agent": "pm_agent", "tool": "publish_requirement"},
                    "created_by": "ak_pm_001",
                },
            }
        ]

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        approved_batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=real_embedding_client,
            vector_searcher=vector_searcher,
        )

        # 内容级重复 → 由 L3 语义相似度判为冲突（Requirement 无关键属性硬键）
        assert approved_batch.conflict_hint is not None
        assert approved_batch.conflict_hint["has_conflict"] is True
        details = approved_batch.conflict_hint["details"]
        assert len(details) == 1
        conflict = details[0]["conflict"]
        assert len(conflict["conflicting_nodes"]) == 1
        assert conflict["conflicting_nodes"][0]["conflict_type"] == "duplicate"
        assert (
            conflict["conflicting_nodes"][0]["matched_key_attrs"] == {}
        )

    async def test_conflict_detection_tag_overlap_only_no_conflict(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        vector_searcher,
        knowledge_helpers,
        sample_batch_payloads,
    ):
        """仅标签有交集、内容不同的两节点，不构成冲突（标签共享≠内容重复）。"""
        project_id = uuid.uuid4()

        # 创建一个带 ["auth", "P0"] 标签的节点
        await _create_approved_node_for_conflict(
            db_session,
            graph_store,
            real_embedding_client,
            knowledge_helpers,
            project_id=project_id,
            title="用户登录鉴权需求-原始",
            content="原始版本",
            tags=["auth", "P0"],
        )

        # 提交一个标题不同但标签有交集的批次
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Requirement",
                    "system_id": str(uuid.uuid4()),
                    "title": "权限管理需求-新版",
                    "content": "完全不同的内容，关于角色权限分配",
                    "properties": {
                        **knowledge_helpers["Requirement"](),
                        "requirement_id": "REQ-2026-TAG-002",
                    },
                    "tags": ["auth", "rbac"],  # 与已有节点的 auth 标签有交集
                    "source": {"agent": "pm_agent", "tool": "publish_requirement"},
                    "created_by": "ak_pm_001",
                },
            }
        ]

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        approved_batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=real_embedding_client,
            vector_searcher=vector_searcher,
        )

        # 标签交集但内容不同 → 不构成冲突（v2 语义：标签共享只说明主题相关）
        assert approved_batch.conflict_hint is not None
        assert approved_batch.conflict_hint["has_conflict"] is False
        assert approved_batch.conflict_hint["suggestion"] is None

    async def test_conflict_detection_no_conflict(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        vector_searcher,
        knowledge_helpers,
        sample_batch_payloads,
    ):
        """无相似无标签匹配，conflict_hint.has_conflict=False。"""
        project_id = uuid.uuid4()

        # 创建一个带 ["auth"] 标签的 CodeSnippet 节点
        await _create_approved_node_for_conflict(
            db_session,
            graph_store,
            real_embedding_client,
            knowledge_helpers,
            project_id=project_id,
            node_type="CodeSnippet",
            title="LoginService 类",
            content="LoginService 负责用户登录鉴权",
            tags=["auth", "service"],
        )

        # 提交一个完全不同类型、不同标签、不同主题的节点
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Pitfall",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Pitfall",
                    "title": "数据库连接池泄漏陷阱",
                    "content": "高并发下未释放数据库连接导致连接池耗尽",
                    "properties": knowledge_helpers["Pitfall"](),
                    "tags": ["database", "concurrency"],
                    "source": {"agent": "dev_agent", "tool": "submit_dev_artifacts"},
                    "created_by": "ak_dev_001",
                },
            }
        ]

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev_001",
            submitter_role="dev",
            items=items,
        )

        approved_batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=real_embedding_client,
            vector_searcher=vector_searcher,
        )

        # 不同类型、不同标签、不同主题 → 无冲突
        assert approved_batch.conflict_hint is not None
        assert approved_batch.conflict_hint["has_conflict"] is False
        assert approved_batch.conflict_hint["suggestion"] is None

    async def test_conflict_detection_cross_project_ignored(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        vector_searcher,
        knowledge_helpers,
    ):
        """跨项目节点不参与冲突检测（不同项目的相同标题不构成冲突）。"""
        project1 = uuid.uuid4()
        project2 = uuid.uuid4()

        # 在 project1 创建一个"用户登录需求"节点
        await _create_approved_node_for_conflict(
            db_session,
            graph_store,
            real_embedding_client,
            knowledge_helpers,
            project_id=project1,
            title="用户登录鉴权需求",
            content="系统需要支持账号密码登录与 JWT 令牌签发",
            tags=["auth"],
        )

        # 在 project2 提交相同标题的节点（跨项目）
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": str(project2),
                    "node_type": "Requirement",
                    "system_id": str(uuid.uuid4()),
                    "title": "用户登录鉴权需求",
                    "content": "系统需要支持账号密码登录与 JWT 令牌签发",
                    "properties": knowledge_helpers["Requirement"](),
                    "tags": ["auth"],
                    "source": {"agent": "pm_agent", "tool": "publish_requirement"},
                    "created_by": "ak_pm_001",
                },
            }
        ]

        batch = await submit_batch(
            db_session,
            project_id=project2,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        approved_batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=real_embedding_client,
            vector_searcher=vector_searcher,
        )

        # 跨项目不参与冲突检测，无冲突
        assert approved_batch.conflict_hint is not None
        assert approved_batch.conflict_hint["has_conflict"] is False


# ============ 边界场景 ============


class TestApprovalEdgeCases:
    """审批流边界场景测试。"""

    async def test_submit_batch_without_operation_id(
        self, db_session, sample_batch_payloads
    ):
        """不提供 operation_id 时正常提交，无幂等校验。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch1 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
            # 不传 operation_id
        )

        items2 = sample_batch_payloads["publish_requirement"](project_id)
        batch2 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items2,
            # 不传 operation_id
        )

        # 两个独立批次（无幂等键约束）
        assert batch1.id != batch2.id

    async def test_submit_batch_with_update_action(
        self, db_session, sample_batch_payloads, knowledge_helpers
    ):
        """update action 的批次提交：payload 含 node_id 即可，不强校验。"""
        project_id = uuid.uuid4()
        node_id = uuid.uuid4()
        items = [
            {
                "item_type": "node",
                "action": "update",
                "entity_type": "Requirement",
                "payload": {
                    "node_id": str(node_id),
                    "title": "更新后的标题",
                    "content": "更新后的内容",
                },
            }
        ]

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        assert batch.id is not None
        detail = await get_batch_detail(db_session, batch.id)
        assert detail.items[0].action == "update"

    async def test_review_approve_empty_batch_items(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
    ):
        """空 items 批次提交被 PayloadValidationError 阻断（不允许空批次）。"""
        with pytest.raises(PayloadValidationError, match="items 不能为空"):
            await submit_batch(
                db_session,
                project_id=uuid.uuid4(),
                batch_type="publish_requirement",
                submitted_by="ak_pm_001",
                submitter_role="pm",
                items=[],
            )

    async def test_submit_batch_with_mixed_node_and_edge(
        self, db_session, sample_batch_payloads
    ):
        """提交混合 node + edge 的批次（submit_dev_artifacts 场景）。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["submit_dev_artifacts"](
            project_id, uuid.uuid4(), uuid.uuid4()
        )

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev_001",
            submitter_role="dev",
            items=items,
        )

        detail = await get_batch_detail(db_session, batch.id)
        item_types = [(item.item_type, item.action) for item in detail.items]
        assert ("node", "create") in item_types
        assert ("edge", "create") in item_types

    async def test_batch_type_whitelist_enforced_in_service(
        self, db_session, sample_batch_payloads
    ):
        """service 层 batch_type 白名单与 BATCH_TYPES 常量一致。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        # 验证所有合法 batch_type 都能提交
        for batch_type in BATCH_TYPES:
            # 每次用不同的 submitted_by 避免幂等键冲突
            await submit_batch(
                db_session,
                project_id=project_id,
                batch_type=batch_type,
                submitted_by=f"ak_test_{batch_type}",
                submitter_role="pm",
                items=items,
            )

    async def test_review_approve_with_review_comment(
        self,
        db_session,
        graph_store,
        mock_embedding_client,
        vector_searcher_mock,
        sample_batch_payloads,
    ):
        """审批通过时可附加 review_comment。"""
        project_id = uuid.uuid4()
        items = sample_batch_payloads["publish_requirement"](project_id)

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
        )

        approved = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher_mock,
            review_comment="需求清晰，审批通过",
        )

        assert approved.review_comment == "需求清晰，审批通过"

    async def test_idempotency_with_different_submitted_by(
        self, db_session, sample_batch_payloads
    ):
        """同 operation_id 但不同 submitted_by 不算幂等冲突（联合唯一键含 submitted_by）。"""
        project_id = uuid.uuid4()
        items1 = sample_batch_payloads["publish_requirement"](project_id)
        items2 = sample_batch_payloads["publish_requirement"](project_id)
        operation_id = "op-shared-001"

        batch1 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items1,
            operation_id=operation_id,
        )

        # 不同 submitted_by + 同 operation_id → 不同批次（不冲突）
        batch2 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_002",
            submitter_role="pm",
            items=items2,
            operation_id=operation_id,
        )

        assert batch1.id != batch2.id

    async def test_idempotency_with_different_batch_type(
        self, db_session, sample_batch_payloads
    ):
        """同 operation_id 但不同 batch_type 不算幂等冲突。"""
        project_id = uuid.uuid4()
        operation_id = "op-shared-002"

        items1 = sample_batch_payloads["publish_requirement"](project_id)
        batch1 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items1,
            operation_id=operation_id,
        )

        # 不同 batch_type + 同 operation_id → 不同批次
        items2 = sample_batch_payloads["publish_requirement"](project_id)
        batch2 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items2,
            operation_id=operation_id,
        )

        assert batch1.id != batch2.id


# ============ 端到端完整流程 ============


class TestApprovalEndToEnd:
    """端到端完整审批流程：PM 提交 → admin 审批通过 → 节点写入 + 向量生成 + 冲突检测。"""

    async def test_pm_publish_requirement_approved_by_admin(
        self,
        db_session,
        graph_store,
        real_embedding_client,
        vector_searcher,
        sample_batch_payloads,
        knowledge_helpers,
    ):
        """PM 提交需求 → admin 审批通过 → 验证完整闭环。"""
        project_id = uuid.uuid4()

        # 1. PM 提交 publish_requirement 批次
        items = sample_batch_payloads["publish_requirement"](project_id)
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm_001",
            submitter_role="pm",
            items=items,
            operation_id="op-e2e-001",
        )

        assert batch.status == STATUS_PENDING_REVIEW

        # 2. admin 查询待审批列表
        pending = await list_pending_batches(db_session, project_id=project_id)
        assert any(b.id == batch.id for b in pending)

        # 3. admin 查看批次详情
        detail = await get_batch_detail(db_session, batch.id)
        assert len(detail.items) == 1
        assert detail.items[0].entity_type == "Requirement"

        # 4. admin 审批通过
        approved = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            graph_store=graph_store,
            embedding_client=real_embedding_client,
            vector_searcher=vector_searcher,
            review_comment="E2E 验证通过",
        )

        # 5. 验证完整闭环
        assert approved.status == STATUS_APPROVED
        assert approved.conflict_hint is not None

        # knowledge_node 表有写入
        stmt = select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        result = await db_session.execute(stmt)
        nodes = list(result.scalars().all())
        assert len(nodes) == 1
        assert nodes[0].status == "approved"
        # 同步审批路径延迟向量化：审批返回时向量暂为 NULL（搜索已能安全跳过 NULL）；
        # 异步补向量由后台 worker（start_embed_nodes_task）完成，见 unit 测试
        # test_start_embed_nodes_task_schedules_node_scope_worker 等。
        assert nodes[0].content_vector is None

        # approval_item.target_id 已回填
        detail_after = await get_batch_detail(db_session, batch.id)
        assert detail_after.items[0].target_id == nodes[0].id

        # AGE 图节点存在
        rows = await graph_store.match_pattern(
            db_session,
            "MATCH (n:Requirement {id: $nid}) RETURN n",
            {"nid": str(nodes[0].id)},
        )
        assert len(rows) == 1

        # 审计日志记录 submit + approve 两条
        submit_logs = await query_audit_logs(
            db_session, actor="ak_pm_001", action="submit", target_id=batch.id
        )
        assert len(submit_logs) >= 1

        approve_logs = await query_audit_logs(
            db_session, actor="ak_admin_001", action="approve", target_id=batch.id
        )
        assert len(approve_logs) >= 1

    async def test_pm_submit_dev_artifacts_rejected_by_admin(
        self,
        db_session,
        sample_batch_payloads,
    ):
        """Dev 提交开发产物 → admin 审批拒绝 → 不写入正式存储。"""
        project_id = uuid.uuid4()

        items = sample_batch_payloads["submit_dev_artifacts"](
            project_id, uuid.uuid4(), uuid.uuid4()
        )
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev_001",
            submitter_role="dev",
            items=items,
        )

        rejected = await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin_001",
            review_comment="代码示例不符合规范",
        )

        assert rejected.status == STATUS_REJECTED

        # knowledge_node 表无写入
        stmt = select(KnowledgeNode).where(KnowledgeNode.project_id == project_id)
        result = await db_session.execute(stmt)
        nodes = list(result.scalars().all())
        assert len(nodes) == 0

        # items 保留
        detail = await get_batch_detail(db_session, batch.id)
        assert len(detail.items) == 3

        # 待审批列表无此批次
        pending = await list_pending_batches(db_session, project_id=project_id)
        assert all(b.id != batch.id for b in pending)


# ============ L0 硬判定冲突检测（问题 3 修复） ============


class TestExactKeyConflict:
    """Requirement 已无业务关键标识字段，判重纯靠 L3 内容语义相似度。

    覆盖：
    - 相同 requirement_id（遗留属性）但内容不同且无 L3 相似候选 → 不判冲突
    - 不同 requirement_id → 不判冲突
    - 内容相似（L3 ≥ 阈值）时 auto_process_batch 路由到 needs_human_review
    """

    async def test_same_requirement_id_different_content_no_conflict(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        from mem_lake.approval.conflict import detect_conflicts
        from mem_lake.knowledge.repository import create_node
        from mem_lake.search.vector import VectorSearcher

        project_id = uuid.uuid4()
        props = knowledge_helpers["Requirement"]()

        # 已存在的 approved 节点
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="用户登录需求 v1",
            content="系统需要支持账号密码登录",
            properties=props,
            system_id=uuid.uuid4(),
            created_by="ak_pm",
        )

        # requirement_id 已废弃、不再作为硬键；内容明显不同且无 L3 相似候选 → 不冲突
        vector_searcher = VectorSearcher(mock_embedding_client)
        vector_searcher.search = AsyncMock(return_value=[])

        result = await detect_conflicts(
            db_session,
            vector_searcher=vector_searcher,
            project_id=project_id,
            node_type="Requirement",
            title="登录模块重构需求（完全不同表述）",
            content="将登录鉴权逻辑拆分为独立微服务并引入 OAuth2",
            properties={},
            tags=[],
        )

        assert result["has_conflict"] is False
        assert result["conflicting_nodes"] == []

    async def test_different_requirement_id_no_conflict(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        from mem_lake.approval.conflict import detect_conflicts
        from mem_lake.knowledge.repository import create_node
        from mem_lake.search.vector import VectorSearcher

        project_id = uuid.uuid4()
        props = knowledge_helpers["Requirement"]()
        props["requirement_id"] = "REQ-2026-0818-1"
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="用户登录需求 v1",
            content="系统需要支持账号密码登录",
            properties=props,
            system_id=uuid.uuid4(),
            created_by="ak_pm",
        )

        vector_searcher = VectorSearcher(mock_embedding_client)
        vector_searcher.search = AsyncMock(return_value=[])

        result = await detect_conflicts(
            db_session,
            vector_searcher=vector_searcher,
            project_id=project_id,
            node_type="Requirement",
            title="用户登录需求",
            content="系统需要支持账号密码登录与 JWT 令牌签发",
            properties={"requirement_id": "REQ-2026-0818-2"},
            tags=[],
        )

        assert result["has_conflict"] is False

    async def test_auto_process_routes_to_human_review(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        from mem_lake.approval.service import auto_process_batch
        from mem_lake.knowledge.repository import create_node
        from mem_lake.search.vector import VectorSearcher

        project_id = uuid.uuid4()
        system_id = uuid.uuid4()
        props = knowledge_helpers["Requirement"]()
        props["requirement_id"] = "REQ-2026-0818-1"
        await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="用户登录需求 v1",
            content="系统需要支持账号密码登录",
            properties=props,
            system_id=system_id,
            created_by="ak_pm",
        )

        # 新批次：与已有需求内容相同（L3 语义相似度 ≥ 阈值）→ 判冲突并升级人工
        new_props = knowledge_helpers["Requirement"]()
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": str(project_id),
                    "node_type": "Requirement",
                    "system_id": str(system_id),
                    "title": "用户登录需求 v1",
                    "content": "系统需要支持账号密码登录",
                    "properties": new_props,
                    "tags": ["auth"],
                    "source": {"agent": "pm_agent", "tool": "publish_requirement"},
                    "created_by": "ak_pm",
                },
            }
        ]
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        vector_searcher = VectorSearcher(mock_embedding_client)
        result = await auto_process_batch(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher,
        )

        assert result["decision"] == "needs_human_review"
        assert result["conflict_hint"]["has_conflict"] is True

        # 正向审批路径：人工确认后 review_approve 通过
        approved = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher,
            review_comment="确认通过",
        )
        assert approved.status == STATUS_APPROVED


class TestRequirementKeyAllocation:
    """需求主键 requirement_key 由服务端按 system 分配可读序号（如 HIS-0001）。"""

    async def test_prefix_from_system_code_and_sequence(
        self, db_session, graph_store, mock_embedding_client
    ):
        sys_id = uuid.uuid4()
        db_session.add(
            System(id=sys_id, name=f"支付系统-{uuid.uuid4().hex[:6]}", code="PAY")
        )
        await db_session.flush()

        n1 = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_type="Requirement",
            title="需求1",
            content="内容1",
            properties={"priority": "P0", "module": "pay"},
            project_id=uuid.uuid4(),
            system_id=sys_id,
            created_by="ak",
        )
        n2 = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_type="Requirement",
            title="需求2",
            content="内容2",
            properties={"priority": "P1", "module": "pay"},
            project_id=uuid.uuid4(),
            system_id=sys_id,
            created_by="ak",
        )
        assert n1.requirement_key == "PAY-0001"
        assert n2.requirement_key == "PAY-0002"

    async def test_prefix_fallback_to_name_then_sys(
        self, db_session, graph_store, mock_embedding_client
    ):
        sys_a = uuid.uuid4()
        db_session.add(
            System(id=sys_a, name=f"PaymentSystem-{uuid.uuid4().hex[:6]}", code=None)
        )
        await db_session.flush()
        na = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_type="Requirement",
            title="a",
            content="a",
            properties={"priority": "P0", "module": "m"},
            project_id=uuid.uuid4(),
            system_id=sys_a,
            created_by="ak",
        )
        assert na.requirement_key == "PAYMEN-0001"

        sys_b = uuid.uuid4()
        db_session.add(System(id=sys_b, name="结算中心", code=None))
        await db_session.flush()
        nb = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            node_type="Requirement",
            title="b",
            content="b",
            properties={"priority": "P0", "module": "m"},
            project_id=uuid.uuid4(),
            system_id=sys_b,
            created_by="ak",
        )
        assert nb.requirement_key == "SYS-0001"


class TestAutoProcessFloating:
    """悬浮 system 需求自动审批回归：project_id=None 时冲突检测不崩溃。"""

    async def test_auto_process_floating_requirement_approved(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """悬浮需求（project_id=None + system_id）auto_process 不再因 _to_uuid(None) 崩溃。

        无冲突时 decision=auto_approved，需求以 project_id=NULL 写入图谱。
        """
        from mem_lake.approval.service import auto_process_batch
        from mem_lake.search.vector import VectorSearcher

        sys_id = uuid.uuid4()
        props = knowledge_helpers["Requirement"]()
        props["requirement_id"] = "REQ-2026-FLOAT-1"
        items = [
            {
                "item_type": "node",
                "action": "create",
                "entity_type": "Requirement",
                "payload": {
                    "project_id": None,
                    "node_type": "Requirement",
                    "system_id": str(sys_id),
                    "title": "悬浮自动审批需求",
                    "content": "跨系统能力沉淀，无归属项目",
                    "properties": props,
                    "tags": ["system"],
                    "source": {"agent": "pm_agent", "tool": "publish_requirement"},
                    "created_by": "ak_pm",
                },
            }
        ]
        batch = await submit_batch(
            db_session,
            project_id=None,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        vector_searcher = VectorSearcher(mock_embedding_client)
        result = await auto_process_batch(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher,
        )
        assert result["decision"] == "auto_approved"
        assert result["batch"].status == STATUS_APPROVED
