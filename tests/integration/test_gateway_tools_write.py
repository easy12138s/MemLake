"""M6 集成测试：write_tools items 构造 → submit_batch → review_approve 端到端。

真实 PG + mock embedding（流程验证，不依赖真实向量相似度）+ AGEGraphStore。
覆盖：
- _build_publish_items：Requirement 节点 + supersedes/relates_to 关系构造
- _build_dev_items：CodeSnippet/Solution/DesignIntent/Pitfall 节点 + 自动 implements 关系 + 显式 relations
- 完整流程：items 构造 → submit_batch → review_approve → 验证节点/边写入 + target_id 回填
- 临时引用解析：from_ref/to_ref 在审批通过时解析为实际节点 ID
- 幂等：同 operation_id 重复提交返回首次 batch_id

测试事务回滚隔离，不污染 DB。
"""

import uuid

import pytest

from mem_lake.approval.service import (
    BatchNotFoundError,
    get_batch_detail,
    list_pending_batches,
    review_approve,
    review_reject,
    submit_batch,
)
from mem_lake.gateway.tools.write_tools import (
    ArtifactsInput,
    CodeSnippetInput,
    DesignIntentInput,
    PitfallInput,
    RelatedInput,
    RequirementInput,
    SolutionInput,
    _build_dev_items,
    _build_publish_items,
)
from mem_lake.knowledge.repository import get_node


# ============================================================================
# _build_publish_items 构造测试
# ============================================================================


class TestBuildPublishItems:
    """_build_publish_items 构造逻辑测试（需真实 PG 校验 schema）。"""

    async def test_build_node_only(self, knowledge_helpers):
        """仅 Requirement 节点，无关联关系。"""
        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="用户登录鉴权需求",
            content="系统需要支持账号密码登录",
            properties=req_props,
            tags=["auth"],
        )
        project_id = uuid.uuid4()

        items = _build_publish_items(project_id, requirement, None, "ak_pm")

        assert len(items) == 1
        assert items[0]["item_type"] == "node"
        assert items[0]["entity_type"] == "Requirement"
        assert items[0]["payload"]["ref"] == "requirement"
        assert items[0]["payload"]["project_id"] == str(project_id)
        assert items[0]["payload"]["created_by"] == "ak_pm"

    async def test_build_with_supersedes_and_relates_to(self, knowledge_helpers):
        """含 supersedes + relates_to 关系。"""
        req_props = knowledge_helpers["Requirement"]()
        old_req_id = uuid.uuid4()
        related_req_id = uuid.uuid4()
        requirement = RequirementInput(
            title="新版登录需求",
            content="替代旧版登录",
            properties=req_props,
        )
        related = RelatedInput(
            supersedes=[str(old_req_id)],
            relates_to=[str(related_req_id)],
        )

        items = _build_publish_items(
            uuid.uuid4(), requirement, related, "ak_pm"
        )

        # 1 节点 + 2 边
        assert len(items) == 3
        assert items[0]["item_type"] == "node"
        # supersedes 边
        assert items[1]["item_type"] == "edge"
        assert items[1]["entity_type"] == "supersedes"
        assert items[1]["payload"]["from_ref"] == "requirement"
        assert items[1]["payload"]["to_ref"] == str(old_req_id)
        # relates_to 边
        assert items[2]["item_type"] == "edge"
        assert items[2]["entity_type"] == "relates_to"
        assert items[2]["payload"]["to_ref"] == str(related_req_id)


# ============================================================================
# _build_dev_items 构造测试
# ============================================================================


class TestBuildDevItems:
    """_build_dev_items 构造逻辑测试。"""

    async def test_build_code_snippet_with_auto_implements(self, knowledge_helpers):
        """1 个 CodeSnippet → 1 node + 1 自动 implements 边。"""
        code_props = knowledge_helpers["CodeSnippet"]()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="LoginService",
                    title="LoginService 类",
                    content="负责用户登录",
                    properties=code_props,
                )
            ]
        )
        project_id = uuid.uuid4()
        requirement_id = uuid.uuid4()

        items = _build_dev_items(
            project_id=project_id,
            requirement_id=requirement_id,
            artifacts=artifacts,
            relations=[],
            created_by="ak_dev",
        )

        # 1 node + 1 自动 implements 边
        assert len(items) == 2
        assert items[0]["item_type"] == "node"
        assert items[0]["payload"]["ref"] == "LoginService"
        assert items[1]["item_type"] == "edge"
        assert items[1]["entity_type"] == "implements"
        assert items[1]["payload"]["from_ref"] == str(requirement_id)
        assert items[1]["payload"]["to_ref"] == "LoginService"

    async def test_build_mixed_artifacts_and_relations(self, knowledge_helpers):
        """混合产物 + 显式 relations（含 ref 引用）。"""
        code_props = knowledge_helpers["CodeSnippet"]()
        sol_props = knowledge_helpers["Solution"]()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="LoginService",
                    title="LoginService",
                    content="登录服务",
                    properties=code_props,
                )
            ],
            solutions=[
                SolutionInput(
                    ref="JwtSolution",
                    title="JWT 方案",
                    content="JWT 鉴权",
                    properties=sol_props,
                )
            ],
        )
        from mem_lake.gateway.tools.write_tools import ArtifactRelationInput
        relations = [
            ArtifactRelationInput(
                from_ref="LoginService",
                to_ref="JwtSolution",
                relation_type="realized_by",
            )
        ]

        items = _build_dev_items(
            project_id=uuid.uuid4(),
            requirement_id=uuid.uuid4(),
            artifacts=artifacts,
            relations=relations,
            created_by="ak_dev",
        )

        # 2 node + 1 自动 implements + 1 显式 realized_by
        assert len(items) == 4
        node_refs = [i["payload"]["ref"] for i in items if i["item_type"] == "node"]
        assert "LoginService" in node_refs
        assert "JwtSolution" in node_refs
        edge_types = [i["entity_type"] for i in items if i["item_type"] == "edge"]
        assert "implements" in edge_types
        assert "realized_by" in edge_types


# ============================================================================
# 完整流程：items 构造 → submit_batch → review_approve 端到端
# ============================================================================


class TestPublishRequirementEndToEnd:
    """publish_requirement 完整流程端到端测试。"""

    async def test_publish_and_approve_writes_node(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """publish_requirement → submit_batch → review_approve → 节点写入 + target_id 回填。"""
        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="端到端测试需求",
            content="验证 publish_requirement 完整流程",
            properties=req_props,
            tags=["e2e"],
        )
        project_id = uuid.uuid4()

        items = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        # 审批通过
        from mem_lake.search.vector import VectorSearcher
        vector_searcher = VectorSearcher(mock_embedding_client)
        batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher,
        )
        assert batch.status == "approved"
        assert len(batch.items) == 1
        assert batch.items[0].target_id is not None

        # 验证节点写入
        node = await get_node(db_session, batch.items[0].target_id)
        assert node.type == "Requirement"
        assert node.title == "端到端测试需求"
        assert node.status == "approved"
        assert node.project_id == project_id

    async def test_publish_with_relations_and_approve(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """publish_requirement 含 supersedes 关系 → approve → 节点+边写入。"""
        # 先创建一个旧需求节点供 supersedes 引用
        from mem_lake.knowledge.repository import create_node
        old_req_props = knowledge_helpers["Requirement"]()
        project_id = uuid.uuid4()
        old_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="旧需求",
            content="旧版本",
            properties=old_req_props,
            created_by="ak_pm",
        )

        # 构造新需求 + supersedes 关系
        new_req_props = knowledge_helpers["Requirement"]()
        new_req_props["requirement_id"] = "REQ-2026-002"
        requirement = RequirementInput(
            title="新需求",
            content="替代旧需求",
            properties=new_req_props,
        )
        related = RelatedInput(supersedes=[str(old_node.id)])
        items = _build_publish_items(project_id, requirement, related, "ak_pm")

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        from mem_lake.search.vector import VectorSearcher
        vector_searcher = VectorSearcher(mock_embedding_client)
        batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher,
        )
        assert batch.status == "approved"
        # 1 节点 + 1 边
        assert len(batch.items) == 2
        node_item = next(i for i in batch.items if i.item_type == "node")
        edge_item = next(i for i in batch.items if i.item_type == "edge")
        assert node_item.target_id is not None
        # 边的 target_id 为 None（边不写 target_id）
        assert edge_item.target_id is None


# ============================================================================
# 临时引用解析端到端测试
# ============================================================================


class TestTempRefResolution:
    """submit_dev_artifacts 临时引用解析端到端测试。"""

    async def test_dev_artifacts_ref_resolution_on_approve(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """submit_dev_artifacts 的 from_ref/to_ref 在 approve 时解析为实际节点 ID。"""
        # 先创建一个 Requirement 节点供 implements 引用
        from mem_lake.knowledge.repository import create_node
        req_props = knowledge_helpers["Requirement"]()
        project_id = uuid.uuid4()
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="原始需求",
            content="需要登录功能",
            properties=req_props,
            created_by="ak_pm",
        )

        # 构造 dev artifacts：1 CodeSnippet + 1 Solution + realized_by 关系（用 ref）
        code_props = knowledge_helpers["CodeSnippet"]()
        sol_props = knowledge_helpers["Solution"]()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="LoginService",
                    title="LoginService",
                    content="登录服务实现",
                    properties=code_props,
                )
            ],
            solutions=[
                SolutionInput(
                    ref="JwtSolution",
                    title="JWT 方案",
                    content="JWT 鉴权方案",
                    properties=sol_props,
                )
            ],
        )
        from mem_lake.gateway.tools.write_tools import ArtifactRelationInput
        relations = [
            ArtifactRelationInput(
                from_ref="LoginService",
                to_ref="JwtSolution",
                relation_type="realized_by",
            )
        ]

        items = _build_dev_items(
            project_id=project_id,
            requirement_id=req_node.id,
            artifacts=artifacts,
            relations=relations,
            created_by="ak_dev",
        )

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev",
            submitter_role="dev",
            items=items,
        )

        # 审批前：ref 为字符串，target_id 为 None
        detail = await get_batch_detail(db_session, batch.id)
        code_item = next(
            i for i in detail.items if i.payload.get("ref") == "LoginService"
        )
        assert code_item.target_id is None

        # 审批通过：ref 解析为实际节点 ID
        from mem_lake.search.vector import VectorSearcher
        vector_searcher = VectorSearcher(mock_embedding_client)
        batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            vector_searcher=vector_searcher,
        )
        assert batch.status == "approved"

        # 验证 target_id 回填
        detail = await get_batch_detail(db_session, batch.id)
        code_item = next(
            i for i in detail.items if i.payload.get("ref") == "LoginService"
        )
        sol_item = next(
            i for i in detail.items if i.payload.get("ref") == "JwtSolution"
        )
        assert code_item.target_id is not None
        assert sol_item.target_id is not None

        # 验证节点实际写入
        code_node = await get_node(db_session, code_item.target_id)
        assert code_node.type == "CodeSnippet"
        assert code_node.title == "LoginService"

    async def test_unresolvable_ref_rejects_on_approve(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """无法解析的 ref 在 approve 时触发回滚（PayloadValidationError）。"""
        # 先创建 Requirement 节点，让自动 implements 边能通过校验
        from mem_lake.knowledge.repository import create_node
        req_props = knowledge_helpers["Requirement"]()
        project_id = uuid.uuid4()
        req_node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=project_id,
            node_type="Requirement",
            title="原始需求",
            content="需要登录功能",
            properties=req_props,
            created_by="ak_pm",
        )

        code_props = knowledge_helpers["CodeSnippet"]()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="LoginService",
                    title="LoginService",
                    content="登录服务",
                    properties=code_props,
                )
            ]
        )
        from mem_lake.gateway.tools.write_tools import ArtifactRelationInput
        # 引用不存在的 ref
        relations = [
            ArtifactRelationInput(
                from_ref="LoginService",
                to_ref="NonExistent",
                relation_type="realized_by",
            )
        ]

        items = _build_dev_items(
            project_id=project_id,
            requirement_id=req_node.id,
            artifacts=artifacts,
            relations=relations,
            created_by="ak_dev",
        )

        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev",
            submitter_role="dev",
            items=items,
        )

        from mem_lake.approval.service import PayloadValidationError
        from mem_lake.search.vector import VectorSearcher
        vector_searcher = VectorSearcher(mock_embedding_client)
        with pytest.raises(PayloadValidationError, match="无法解析临时引用"):
            await review_approve(
                db_session,
                batch_id=batch.id,
                reviewed_by="ak_admin",
                graph_store=graph_store,
                embedding_client=mock_embedding_client,
                vector_searcher=vector_searcher,
            )


# ============================================================================
# 幂等性端到端测试
# ============================================================================


class TestIdempotencyEndToEnd:
    """operation_id 幂等端到端测试。"""

    async def test_same_operation_id_returns_first_batch(
        self, db_session, knowledge_helpers
    ):
        """同 operation_id 重复提交返回首次 batch_id。"""
        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="幂等测试需求",
            content="验证 operation_id 幂等",
            properties=req_props,
        )
        project_id = uuid.uuid4()
        operation_id = "op_test_001"

        items1 = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch1 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items1,
            operation_id=operation_id,
        )

        items2 = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch2 = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items2,
            operation_id=operation_id,
        )

        assert batch1.id == batch2.id
        assert len(batch1.items) == len(batch2.items)


# ============================================================================
# review_reject 端到端测试
# ============================================================================


class TestReviewRejectEndToEnd:
    """review_reject 端到端测试。"""

    async def test_reject_does_not_write_graph(
        self, db_session, knowledge_helpers
    ):
        """reject 后批次状态 rejected，不写入知识图谱。"""
        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="被拒绝的需求",
            content="这个需求会被拒绝",
            properties=req_props,
        )
        project_id = uuid.uuid4()
        items = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        batch = await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            review_comment="需求描述不清晰",
        )
        assert batch.status == "rejected"
        assert batch.review_comment == "需求描述不清晰"
        # target_id 未回填（未写入图谱）
        assert all(i.target_id is None for i in batch.items)

    async def test_reject_then_approve_raises(
        self, db_session, knowledge_helpers
    ):
        """rejected 终态，再次 approve 抛 BatchStatusError。"""
        from mem_lake.approval.service import BatchStatusError

        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="测试需求",
            content="内容",
            properties=req_props,
        )
        project_id = uuid.uuid4()
        items = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        await review_reject(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            review_comment="拒绝",
        )

        from mem_lake.search.vector import VectorSearcher
        from unittest.mock import MagicMock
        mock_embedding = MagicMock()
        mock_embedding.embed_one = MagicMock(return_value=[0.1] * 1024)
        with pytest.raises(BatchStatusError):
            await review_approve(
                db_session,
                batch_id=batch.id,
                reviewed_by="ak_admin",
                graph_store=None,
                embedding_client=mock_embedding,
                vector_searcher=VectorSearcher(mock_embedding),
            )


# ============================================================================
# 查询端到端测试
# ============================================================================


class TestReviewQueryEndToEnd:
    """review_pending_list + get_batch_detail 端到端测试。"""

    async def test_list_pending_after_submit(self, db_session, knowledge_helpers):
        """submit 后 list_pending_batches 可查到。"""
        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="列表测试需求",
            content="验证 pending 列表",
            properties=req_props,
        )
        project_id = uuid.uuid4()
        items = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        pending = await list_pending_batches(db_session, project_id=project_id)
        assert any(b.id == batch.id for b in pending)
        assert all(b.status == "pending_review" for b in pending)

    async def test_get_detail_after_submit(self, db_session, knowledge_helpers):
        """submit 后 get_batch_detail 返回完整 items。"""
        req_props = knowledge_helpers["Requirement"]()
        requirement = RequirementInput(
            title="详情测试需求",
            content="验证批次详情",
            properties=req_props,
        )
        project_id = uuid.uuid4()
        items = _build_publish_items(project_id, requirement, None, "ak_pm")
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="publish_requirement",
            submitted_by="ak_pm",
            submitter_role="pm",
            items=items,
        )

        detail = await get_batch_detail(db_session, batch.id)
        assert detail.id == batch.id
        assert len(detail.items) == 1
        assert detail.items[0].payload["title"] == "详情测试需求"

    async def test_get_detail_nonexistent_raises(self, db_session):
        """查询不存在批次抛 BatchNotFoundError。"""
        with pytest.raises(BatchNotFoundError):
            await get_batch_detail(db_session, uuid.uuid4())
