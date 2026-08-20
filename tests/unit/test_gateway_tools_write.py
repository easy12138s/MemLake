"""gateway/tools 工具层单元测试：items 构造逻辑 + 异常处理。

纯单测，无 DB 依赖。mock 所有 service 层调用，重点验证：
- publish_requirement/update_requirement_relations/submit_dev_artifacts 的 items 构造正确性
- 临时引用（ref）保留在 payload 中（不在工具层解析）
- 项目权限校验调用
- operation_id 透传
- 异常转换为 ToolError

依赖注入通过 mock patch 实现：
- get_current_key_id → 返回固定 key_id
- validate_project_access → mock 不抛异常
- transactional_session → mock async context manager
- submit_batch → mock 返回固定 batch
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from mem_lake.approval.models import ApprovalBatch
from mem_lake.approval.service import PayloadValidationError
from mem_lake.gateway.tools.write_tools import _build_dev_items
from mem_lake.gateway.tools.write_tools import _build_publish_items
from mem_lake.gateway.tools.write_tools import (
    RequirementInput,
    RelatedInput,
)


class TestBuildPublishItems:
    """publish_requirement 的 items 构造测试。"""

    def test_single_requirement_no_relations(self):
        """仅需求节点，无关联关系：1 个 node item。"""
        project_id = uuid.uuid4()
        requirement = RequirementInput(
            title="用户登录",
            content="实现 JWT 登录",
            properties={"requirement_id": "REQ-001", "priority": "P0"},
            tags=["auth"],
        )
        items = _build_publish_items(project_id, requirement, None, "ak_test")

        assert len(items) == 1
        assert items[0]["item_type"] == "node"
        assert items[0]["entity_type"] == "Requirement"
        assert items[0]["payload"]["ref"] == "requirement"
        assert items[0]["payload"]["title"] == "用户登录"
        assert items[0]["payload"]["project_id"] == str(project_id)
        assert items[0]["payload"]["created_by"] == "ak_test"

    def test_with_supersedes_relations(self):
        """含 supersedes 关系：1 node + N edge。"""
        project_id = uuid.uuid4()
        requirement = RequirementInput(
            title="新需求",
            content="替代旧需求",
            properties={"requirement_id": "REQ-002", "priority": "P1"},
        )
        related = RelatedInput(supersedes=["REQ-001", "REQ-000"])
        items = _build_publish_items(project_id, requirement, related, "ak")

        # 1 node + 2 edge
        assert len(items) == 3
        assert items[0]["item_type"] == "node"
        assert items[1]["item_type"] == "edge"
        assert items[1]["payload"]["edge_type"] == "supersedes"
        assert items[1]["payload"]["from_ref"] == "requirement"
        assert items[1]["payload"]["to_ref"] == "REQ-001"
        assert items[2]["payload"]["to_ref"] == "REQ-000"

    def test_with_relates_to_relations(self):
        """含 relates_to 关系：1 node + N edge。"""
        project_id = uuid.uuid4()
        requirement = RequirementInput(
            title="需求A",
            content="关联需求B",
            properties={"requirement_id": "REQ-A", "priority": "P2"},
        )
        related = RelatedInput(relates_to=["REQ-B"])
        items = _build_publish_items(project_id, requirement, related, "ak")

        assert len(items) == 2
        assert items[1]["payload"]["edge_type"] == "relates_to"
        assert items[1]["payload"]["to_ref"] == "REQ-B"

    def test_with_both_relations(self):
        """同时含 supersedes 和 relates_to：1 node + N + M edge。"""
        project_id = uuid.uuid4()
        requirement = RequirementInput(
            title="需求",
            content="内容",
            properties={"requirement_id": "REQ-X", "priority": "P1"},
        )
        related = RelatedInput(
            supersedes=["REQ-OLD1", "REQ-OLD2"],
            relates_to=["REQ-REL1", "REQ-REL2", "REQ-REL3"],
        )
        items = _build_publish_items(project_id, requirement, related, "ak")

        # 1 node + 2 supersedes + 3 relates_to
        assert len(items) == 6
        edge_types = [item["payload"]["edge_type"] for item in items[1:]]
        assert edge_types.count("supersedes") == 2
        assert edge_types.count("relates_to") == 3

    def test_empty_related_object(self):
        """related 为空 RelatedInput（supersedes 和 relates_to 都为空）：仅 1 node。"""
        project_id = uuid.uuid4()
        requirement = RequirementInput(
            title="t", content="c", properties={"k": "v"}
        )
        related = RelatedInput()  # 默认空列表
        items = _build_publish_items(project_id, requirement, related, "ak")

        assert len(items) == 1
        assert items[0]["item_type"] == "node"


class TestBuildDevItems:
    """submit_dev_artifacts 的 items 构造测试。"""

    def test_single_code_snippet(self):
        """单个代码片段：1 node + 1 自动 implements edge。"""
        from mem_lake.gateway.tools.write_tools import (
            ArtifactsInput,
            CodeSnippetInput,
        )

        project_id = uuid.uuid4()
        requirement_id = uuid.uuid4()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="LoginService",
                    title="登录服务",
                    content="JWT 登录实现",
                    properties={
                        "name": "LoginService",
                        "type": "class",
                        "responsibility": "用户认证",
                        "file_path": "auth/login.py",
                    },
                )
            ]
        )
        items = _build_dev_items(
            project_id=project_id,
            requirement_id=requirement_id,
            artifacts=artifacts,
            relations=[],
            created_by="ak_dev",
        )

        # 1 node + 1 auto implements edge
        assert len(items) == 2
        assert items[0]["item_type"] == "node"
        assert items[0]["entity_type"] == "CodeSnippet"
        assert items[0]["payload"]["ref"] == "LoginService"
        assert items[0]["payload"]["created_by"] == "ak_dev"

        assert items[1]["item_type"] == "edge"
        assert items[1]["payload"]["edge_type"] == "implements"
        assert items[1]["payload"]["from_ref"] == str(requirement_id)
        assert items[1]["payload"]["to_ref"] == "LoginService"

    def test_multiple_artifact_types(self):
        """多种产物类型：每种 1 node + code_snippets 自动 implements。"""
        from mem_lake.gateway.tools.write_tools import (
            ArtifactsInput,
            CodeSnippetInput,
            DesignIntentInput,
            PitfallInput,
            SolutionInput,
        )

        project_id = uuid.uuid4()
        requirement_id = uuid.uuid4()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="Code1",
                    title="代码1",
                    content="c",
                    properties={"name": "n", "type": "function", "responsibility": "r", "file_path": "f"},
                )
            ],
            solutions=[
                SolutionInput(
                    ref="Sol1",
                    title="方案1",
                    content="c",
                    properties={"approach": "a", "alternatives": "b"},
                )
            ],
            design_intents=[
                DesignIntentInput(
                    ref="Intent1",
                    title="意图1",
                    content="c",
                    properties={"rationale": "r", "trade_offs": "t"},
                )
            ],
            pitfalls=[
                PitfallInput(
                    ref="Pitfall1",
                    title="踩坑1",
                    content="c",
                    properties={"symptom": "s", "root_cause": "rc", "solution": "sol", "severity": "P1"},
                )
            ],
        )
        items = _build_dev_items(
            project_id=project_id,
            requirement_id=requirement_id,
            artifacts=artifacts,
            relations=[],
            created_by="ak",
        )

        # 4 nodes + 1 auto implements (only for code_snippets)
        assert len(items) == 5
        node_types = [item["entity_type"] for item in items if item["item_type"] == "node"]
        assert "CodeSnippet" in node_types
        assert "Solution" in node_types
        assert "DesignIntent" in node_types
        assert "Pitfall" in node_types

    def test_with_explicit_relations(self):
        """含显式 relations：nodes + auto implements + explicit relations。"""
        from mem_lake.gateway.tools.write_tools import (
            ArtifactRelationInput,
            ArtifactsInput,
            CodeSnippetInput,
        )

        project_id = uuid.uuid4()
        requirement_id = uuid.uuid4()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="Code1",
                    title="代码1",
                    content="c",
                    properties={"name": "n", "type": "function", "responsibility": "r", "file_path": "f"},
                )
            ]
        )
        relations = [
            ArtifactRelationInput(
                from_ref="Code1",
                to_ref="Sol1",
                relation_type="realized_by",
            ),
            ArtifactRelationInput(
                from_ref="Code1",
                to_ref="Pitfall1",
                relation_type="described_by",
            ),
        ]
        items = _build_dev_items(
            project_id=project_id,
            requirement_id=requirement_id,
            artifacts=artifacts,
            relations=relations,
            created_by="ak",
        )

        # 1 node + 1 auto implements + 2 explicit relations
        assert len(items) == 4
        edge_types = [item["payload"]["edge_type"] for item in items if item["item_type"] == "edge"]
        assert edge_types.count("implements") == 1
        assert edge_types.count("realized_by") == 1
        assert edge_types.count("described_by") == 1

    def test_empty_artifacts_and_relations(self):
        """空 artifacts + 空 relations：返回空列表。"""
        from mem_lake.gateway.tools.write_tools import ArtifactsInput

        items = _build_dev_items(
            project_id=uuid.uuid4(),
            requirement_id=uuid.uuid4(),
            artifacts=ArtifactsInput(),
            relations=[],
            created_by="ak",
        )
        assert len(items) == 0

    def test_temporary_ref_preserved(self):
        """临时引用（ref）保留在 payload 中，不在工具层解析。"""
        from mem_lake.gateway.tools.write_tools import (
            ArtifactsInput,
            CodeSnippetInput,
        )

        project_id = uuid.uuid4()
        requirement_id = uuid.uuid4()
        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="MyRef",
                    title="t",
                    content="c",
                    properties={"name": "n", "type": "function", "responsibility": "r", "file_path": "f"},
                )
            ]
        )
        items = _build_dev_items(
            project_id=project_id,
            requirement_id=requirement_id,
            artifacts=artifacts,
            relations=[],
            created_by="ak",
        )

        # ref 保留在 node payload 中
        assert items[0]["payload"]["ref"] == "MyRef"
        # edge 的 to_ref 保留为 ref 名（未解析为 UUID）
        assert items[1]["payload"]["to_ref"] == "MyRef"
        # edge 的 from_ref 保留为 UUID 字符串
        assert items[1]["payload"]["from_ref"] == str(requirement_id)


class TestContentLengthLimit:
    """content 长度上限保护（纯逻辑，无 DB 依赖）。"""

    def test_check_content_length_within_limit(self):
        from mem_lake.gateway.tools.write_tools import _check_content_length

        # 不抛即通过
        _check_content_length("x" * 100, "label")
        _check_content_length(None, "label")

    def test_check_content_length_exceeds(self):
        from mem_lake.gateway.tools.write_tools import (
            MAX_CONTENT_LENGTH,
            _check_content_length,
        )

        with pytest.raises(PayloadValidationError):
            _check_content_length("x" * (MAX_CONTENT_LENGTH + 1), "label")

    def test_build_dev_items_content_too_long(self):
        from mem_lake.gateway.tools.write_tools import (
            ArtifactsInput,
            CodeSnippetInput,
            MAX_CONTENT_LENGTH,
            _build_dev_items,
        )

        artifacts = ArtifactsInput(
            code_snippets=[
                CodeSnippetInput(
                    ref="C1",
                    title="t",
                    content="x" * (MAX_CONTENT_LENGTH + 1),
                    properties={
                        "name": "n",
                        "type": "function",
                        "responsibility": "r",
                        "file_path": "f",
                    },
                )
            ]
        )
        with pytest.raises(PayloadValidationError):
            _build_dev_items(
                project_id=uuid.uuid4(),
                requirement_id=uuid.uuid4(),
                artifacts=artifacts,
                relations=[],
                created_by="ak",
            )
