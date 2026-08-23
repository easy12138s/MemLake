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
    PayloadValidationError,
    get_batch_detail,
    list_pending_batches,
    review_approve,
    review_reject,
    submit_batch,
)
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.gateway.tools.write_tools import (
    ArtifactRelationInput,
    ArtifactsInput,
    CodeSnippetInput,
    PitfallInput,
    RelatedInput,
    RequirementInput,
    SolutionInput,
    _build_dev_items,
    _build_publish_items,
    _get_project_profile_id,
    _validate_dev_artifacts,
)
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import create_node, get_node

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

        from unittest.mock import MagicMock

        from mem_lake.search.vector import VectorSearcher
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


# ============================================================================
# C 项：submit_dev_artifacts 提交前 ref / requirement_id 一致性与归属校验
# ============================================================================


class TestValidateDevArtifacts:
    """_validate_dev_artifacts 提前拦截批内 ref / requirement_id 不一致。"""

    @staticmethod
    def _artifacts(ref: str = "CS1") -> ArtifactsInput:
        return ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref=ref,
                    title="登录服务",
                    content="x",
                    properties={
                        "name": "LoginService",
                        "type": "class",
                        "responsibility": "登录",
                        "file_path": "a.py",
                    },
                )
            ]
        )

    @staticmethod
    async def _seed_node(project_id, node_type) -> KnowledgeNode:
        """插入一个已提交的节点（独立 session），返回供测试引用。"""
        node = KnowledgeNode(
            id=uuid.uuid4(),
            project_id=project_id,
            type=node_type,
            title="seed",
            content="seed",
            created_by="ak_dev",
        )
        async with AsyncSessionLocal() as s:
            s.add(node)
            await s.commit()
        return node

    @staticmethod
    async def _cleanup(node) -> None:
        async with AsyncSessionLocal() as s:
            await s.delete(node)
            await s.commit()

    async def test_duplicate_ref_rejected(self, db_session):
        """重复 ref 触发 PayloadValidationError（批内必须唯一）。"""
        arts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="CS1",
                    title="a",
                    content="x",
                    properties={
                        "name": "A",
                        "type": "class",
                        "responsibility": "r",
                        "file_path": "a.py",
                    },
                ),
                CodeSnippetInput(
                    ref="CS1",
                    title="b",
                    content="x",
                    properties={
                        "name": "B",
                        "type": "class",
                        "responsibility": "r",
                        "file_path": "b.py",
                    },
                ),
            ]
        )
        with pytest.raises(PayloadValidationError, match="ref 重复"):
            await _validate_dev_artifacts(
                project_id=uuid.uuid4(),
                requirement_id=uuid.uuid4(),
                artifacts=arts,
                relations=[],
            )

    async def test_unknown_ref_name_rejected(self, db_session):
        """relation 引用未在 artifacts 声明的 ref 名 → 拦截。"""
        project_id = uuid.uuid4()
        req = await self._seed_node(project_id, "Requirement")
        relations = [
            ArtifactRelationInput(
                from_ref="CS1", to_ref="NOPE", relation_type="depends_on"
            )
        ]
        try:
            with pytest.raises(PayloadValidationError, match="未知 ref 名"):
                await _validate_dev_artifacts(
                    project_id=project_id,
                    requirement_id=req.id,
                    artifacts=self._artifacts(),
                    relations=relations,
                )
        finally:
            await self._cleanup(req)

    async def test_self_reference_rejected(self, db_session):
        """relation 的 from_ref == to_ref 视为自引用 → 拦截。"""
        project_id = uuid.uuid4()
        req = await self._seed_node(project_id, "Requirement")
        relations = [
            ArtifactRelationInput(
                from_ref="CS1", to_ref="CS1", relation_type="depends_on"
            )
        ]
        try:
            with pytest.raises(PayloadValidationError, match="自引用"):
                await _validate_dev_artifacts(
                    project_id=project_id,
                    requirement_id=req.id,
                    artifacts=self._artifacts(),
                    relations=relations,
                )
        finally:
            await self._cleanup(req)

    async def test_dangling_uuid_rejected(self, db_session):
        """relation 引用不存在的 UUID → 拦截。"""
        project_id = uuid.uuid4()
        req = await self._seed_node(project_id, "Requirement")
        dangling = uuid.uuid4()
        relations = [
            ArtifactRelationInput(
                from_ref="CS1", to_ref=str(dangling), relation_type="depends_on"
            )
        ]
        try:
            with pytest.raises(PayloadValidationError, match="引用 UUID 不存在"):
                await _validate_dev_artifacts(
                    project_id=project_id,
                    requirement_id=req.id,
                    artifacts=self._artifacts(),
                    relations=relations,
                )
        finally:
            await self._cleanup(req)

    async def test_requirement_id_not_found_rejected(self, db_session):
        """requirement_id 不存在 → 拦截。"""
        with pytest.raises(PayloadValidationError, match="requirement_id 不存在"):
            await _validate_dev_artifacts(
                project_id=uuid.uuid4(),
                requirement_id=uuid.uuid4(),
                artifacts=self._artifacts(),
                relations=[],
            )

    async def test_requirement_id_wrong_type_rejected(self, db_session):
        """requirement_id 指向非 Requirement 节点 → 拦截。"""
        project_id = uuid.uuid4()
        node = await self._seed_node(project_id, "CodeSnippet")
        try:
            with pytest.raises(PayloadValidationError, match="类型非 Requirement"):
                await _validate_dev_artifacts(
                    project_id=project_id,
                    requirement_id=node.id,
                    artifacts=self._artifacts(),
                    relations=[],
                )
        finally:
            await self._cleanup(node)

    async def test_valid_passes(self, db_session):
        """合法 requirement_id + 内部引用 → 通过校验不抛异常。"""
        project_id = uuid.uuid4()
        req = await self._seed_node(project_id, "Requirement")
        try:
            await _validate_dev_artifacts(
                project_id=project_id,
                requirement_id=req.id,
                artifacts=self._artifacts(),
                relations=[
                    ArtifactRelationInput(
                        from_ref="CS1",
                        to_ref=str(req.id),
                        relation_type="implements",
                    )
                ],
            )
        finally:
            await self._cleanup(req)


# ============================================================================
# 游离知识点（requirements_id 可选）端到端测试
# ============================================================================


class TestFreeStandingArtifacts:
    """submit_dev_artifacts 省略 requirement_id 的端到端行为。"""

    async def _seed_profile(self, db_session, graph_store, embedding_client, project_id, knowledge_helpers):
        """为项目创建一个 ProjectProfile 节点，返回其 ID。"""
        profile_props = knowledge_helpers["ProjectProfile"]()
        profile_props["name"] = "游离测试项目画像"
        node = await create_node(
            db_session,
            graph_store=graph_store,
            embedding_client=embedding_client,
            project_id=project_id,
            node_type="ProjectProfile",
            title="游离测试项目画像",
            content="项目画像",
            properties=profile_props,
            created_by="ak_admin",
        )
        return node.id

    async def _submit_and_approve(
        self, db_session, graph_store, embedding_client, project_id, requirement_id, profile_id, knowledge_helpers
    ):
        """构造游离/绑定产物批次并提交+审批，返回 (batch, artifact_target_id)。"""
        pit_props = knowledge_helpers["Pitfall"]()
        artifacts = ArtifactsInput(
            pitfalls=[
                PitfallInput(
                    ref="FreePitfall",
                    title="游离坑：配置项拼写",
                    content="yaml 缩进错误导致服务启动失败",
                    properties=pit_props,
                )
            ]
        )
        items = _build_dev_items(
            project_id=project_id,
            requirement_id=requirement_id,
            artifacts=artifacts,
            relations=[],
            created_by="ak_dev",
            profile_id=profile_id,
        )
        batch = await submit_batch(
            db_session,
            project_id=project_id,
            batch_type="submit_dev_artifacts",
            submitted_by="ak_dev",
            submitter_role="dev",
            items=items,
        )
        from mem_lake.search.vector import VectorSearcher

        vector_searcher = VectorSearcher(embedding_client)
        batch = await review_approve(
            db_session,
            batch_id=batch.id,
            reviewed_by="ak_admin",
            graph_store=graph_store,
            embedding_client=embedding_client,
            vector_searcher=vector_searcher,
        )
        artifact_item = next(
            i for i in batch.items if i.payload.get("ref") == "FreePitfall"
        )
        return batch, artifact_item.target_id

    async def test_free_standing_with_profile_links_to_profile(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """有 ProjectProfile：游离提交审批后，节点写入且自动生成 references 边。"""
        project_id = uuid.uuid4()
        profile_id = await self._seed_profile(
            db_session, graph_store, mock_embedding_client, project_id, knowledge_helpers
        )
        # 确认 ProjectProfile 已入库（同会话内可见）
        profile_node = await get_node(db_session, profile_id)
        assert profile_node.type == "ProjectProfile"

        batch, artifact_id = await self._submit_and_approve(
            db_session,
            graph_store,
            mock_embedding_client,
            project_id,
            requirement_id=None,
            profile_id=profile_id,
            knowledge_helpers=knowledge_helpers,
        )
        assert batch.status == "approved"
        assert artifact_id is not None

        # 节点确实写入
        node = await get_node(db_session, artifact_id)
        assert node.type == "Pitfall"

        # 自动生成 ProjectProfile --references--> Pitfall 边
        edges = await graph_store.match_pattern(
            db_session,
            "MATCH (p:ProjectProfile)-[r:references]->(a) "
            "WHERE p.id = $pid AND a.id = $aid RETURN r",
            {"pid": str(profile_id), "aid": str(artifact_id)},
        )
        assert len(edges) >= 1

    async def test_free_standing_without_profile_no_edge(
        self, db_session, graph_store, mock_embedding_client, knowledge_helpers
    ):
        """无 ProjectProfile：游离提交审批后，节点写入但无 references 边。"""
        project_id = uuid.uuid4()
        resolved = await _get_project_profile_id(project_id)
        assert resolved is None

        batch, artifact_id = await self._submit_and_approve(
            db_session,
            graph_store,
            mock_embedding_client,
            project_id,
            requirement_id=None,
            profile_id=None,
            knowledge_helpers=knowledge_helpers,
        )
        assert batch.status == "approved"
        assert artifact_id is not None

        node = await get_node(db_session, artifact_id)
        assert node.type == "Pitfall"

        # 无任何 references 边指向该产物
        edges = await graph_store.match_pattern(
            db_session,
            "MATCH (p:ProjectProfile)-[r:references]->(a) "
            "WHERE a.id = $aid RETURN r",
            {"aid": str(artifact_id)},
        )
        assert len(edges) == 0
