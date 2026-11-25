"""写入类工具：产生审批批次（默认等待 admin 审批；宽松模式提交即自动处理）。

工具职责：构造审批项（node/edge items）并提交批次，不在工具层写入知识图谱。
默认所有写操作经 admin 审批通过后由 approval/service 原子写入图谱；若提交方
Access Key 为宽松模式（lax_mode=true 且全局 LAX_MODE_ENABLED 开启），则在提交时
同一事务内自动处理（无冲突直接 approved 入库，有冲突升级人工），复用三层冲突检测。

包含工具（PDD 6.1）：
- publish_requirement（PM）：发布需求节点 + 关联关系
- update_requirement_relations（PM）：更新需求间关系边
- submit_dev_artifacts（Dev）：批量提交开发产物（代码/方案/意图/踩坑）
- update_node（PM/Dev）：更新已通过节点的内容

设计要点：
- 工具参数采用 flat params 模式，复杂嵌套用 Pydantic 模型
- 临时引用（ref）在工具层保留为字符串，审批通过时由 approval 层解析为节点 ID
- 工具层不写业务逻辑，仅参数解析 + items 构造 + 转发 submit_write_batch（_shared）
- 角色 RBAC 由中间件层控制，本文件不区分角色
"""

import logging
import uuid
from typing import Any, cast

from fastmcp import FastMCP
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.approval.service import (
    PayloadValidationError,
)
from mem_lake.auth.rbac import DEV_UPDATABLE_NODE_TYPES
from mem_lake.config import get_settings
from mem_lake.gateway.access import is_requirement_visible
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    get_current_project_scope,
    get_current_role,
    get_current_system_scope,
    readonly_session,
    validate_project_access,
)
from mem_lake.gateway.tools._shared import (
    READ_TOOL_ANNOTATIONS,
    WRITE_TOOL_ANNOTATIONS,
    StrictInputModel,
    WriteToolOutput,
    build_edge_item,
    build_node_item,
    build_update_node_item,
    get_lifespan_context,
    submit_write_batch,
    to_tool_error,
)
from mem_lake.knowledge.embed import build_embed_text
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    find_requirements_by_ref,
    get_node,
    get_node_ids_with_vectors,
    get_nodes_by_ids,
    get_system,
    get_system_project_ids,
    list_project_profiles,
)
from mem_lake.knowledge.schema import SchemaValidationError, validate_node
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import hybrid_search

logger = logging.getLogger("mem_lake.gateway.tools.write")

_ADD_PROJECTS_FIX = "请先 manage_system action=add_projects 挂载后重试"


def _check_content_length(value: str | None, label: str) -> None:
    """校验 content 长度不超过上限，超限抛 PayloadValidationError。

    上限来自 Settings.MAX_CONTENT_LENGTH（可环境变量覆盖）。
    value 为 None（update 不更新 content）时跳过长度校验。
    """
    max_len = get_settings().MAX_CONTENT_LENGTH
    if value is not None and len(value) > max_len:
        raise PayloadValidationError(
            f"{label} 长度 {len(value)} 超过上限 {max_len} 字符"
        )


def _check_update_node_type(node: KnowledgeNode, *, role: str) -> None:
    """update_node 的角色×节点类型闸门。

    需求内容归 PM（Dev 不可改 Requirement）；研发资产内容归 Dev；
    ProjectProfile 归 admin（用 manage_project_profile 直写）。

    Dev 可改的类型集合统一取自 auth.rbac.DEV_UPDATABLE_NODE_TYPES，避免两处口径漂移。
    """
    if node.type == "ProjectProfile":
        raise PayloadValidationError(
            "ProjectProfile 由 admin 通过 manage_project_profile 维护，"
            "不可用 update_node 修改"
        )
    if role == "dev" and node.type not in DEV_UPDATABLE_NODE_TYPES:
        raise PayloadValidationError(
            "Dev 角色不可修改需求节点内容（需求内容由 PM 维护）；"
            "如需把需求落到项目，请用 bind_requirement_to_project"
        )


def _ownership_mismatch_hint(
    node: KnowledgeNode,
    project_id: uuid.UUID | None,
    *,
    system_name: str | None,
    system_member_ids: set[uuid.UUID] | None,
) -> str:
    """节点归属与目标项目不一致的报错文案（ISSUE-13/14 + 悬浮支持）。

    四态：
    - 传了 project_id、节点为悬浮 → 提示「更新悬浮需求请不传 project_id」
    - 传了 project_id、节点归属他项目 → 回实际 project_id
    - 没传 project_id（悬浮路径）、节点在项目内 → 回实际 project_id + 提示「请传 project_id」
    - 目标项目不在节点 system 的归属清单 → 追加 add_projects 修复指引
    """
    msg = f"节点不属于本项目: {node.id}"
    if node.project_id is None:
        if project_id is None:
            msg += "（节点为悬浮需求，无项目归属）"
        else:
            msg += "（节点为悬浮需求，无项目归属；更新悬浮需求请不传 project_id）"
    else:
        msg += f"（节点实际归属项目: {node.project_id}）"
        if project_id is None:
            msg += "；更新项目内节点请传 project_id"
    if system_name is not None and system_member_ids is not None and project_id is not None:
        if project_id not in system_member_ids:
            msg += f"；目标项目不在 system「{system_name}」归属清单，{_ADD_PROJECTS_FIX}"
    return msg


# ============================================================================
# 需求相关 Pydantic 模型（publish_requirement / update_requirement_relations）
# ============================================================================


class RequirementInput(StrictInputModel):
    """需求节点内容。"""

    title: str = Field(description="需求标题")
    content: str = Field(description="需求详细描述")
    properties: dict[str, Any] = Field(
        description=(
            "需求属性。**必填字段**：priority（P0/P1/P2/P3）、module（模块）。"
            "**可选字段**：acceptance_criteria（验收标准）、source_doc、version、"
            "external_id（如 Jira/ADO 原始需求号，仅作记录，不参与判重）。"
            "需求主键（如 HIS-0001）由服务端按 system 域自动分配并存储于节点，"
            "经 list_requirements（requirement_key 直查）或 get_requirement_context 获取；"
            "调用方无需、也不应传入 requirement_id。"
        )
    )
    tags: list[str] = Field(default=[], description="标签数组")


class RelatedInput(StrictInputModel):
    """需求间关联关系。"""

    supersedes: list[str] = Field(
        default=[],
        description="被替代的旧需求节点 ID 列表（自动构造 supersedes 边）",
    )
    relates_to: list[str] = Field(
        default=[],
        description="关联需求节点 ID 列表（自动构造 relates_to 边）",
    )


class RelationInput(StrictInputModel):
    """需求间关系（用于 update_requirement_relations）。

    端点二选一：from_id/to_id（UUID）或 from_ref/to_ref（requirement_key 或
    external_id）。混用同侧字段直接拒绝，避免歧义。
    """

    from_id: uuid.UUID | None = Field(default=None, description="源需求节点 ID")
    to_id: uuid.UUID | None = Field(default=None, description="目标需求节点 ID")
    from_ref: str | None = Field(
        default=None, description="源需求 requirement_key 或 external_id（与 from_id 二选一）"
    )
    to_ref: str | None = Field(
        default=None, description="目标需求 requirement_key 或 external_id（与 to_id 二选一）"
    )
    relation_type: str = Field(
        description="关系类型：conflicts_with/duplicates/relates_to/supersedes/version_of"
    )
    properties: dict[str, Any] = Field(default={}, description="边属性")

    @model_validator(mode="after")  # type: ignore[untyped-decorator]
    def _require_one_endpoint(self) -> "RelationInput":
        if (self.from_id is None) == (self.from_ref is None):
            raise ValueError("from_id 与 from_ref 必须二选一")
        if (self.to_id is None) == (self.to_ref is None):
            raise ValueError("to_id 与 to_ref 必须二选一")
        return self


class BatchRequirementInput(StrictInputModel):
    """批量发布中的单条需求（client_ref 供回执/幂等对账）。"""

    client_ref: str = Field(
        min_length=1,
        max_length=64,
        description="提交内唯一引用名（如 'YB3-001'），回执按它返回 batch_id",
    )
    requirement: RequirementInput
    project_id: uuid.UUID | None = Field(
        default=None, description="单条需求归属项目；None=悬浮（继承 system）"
    )
    related: RelatedInput | None = Field(
        default=None, description="关联关系（仅支持已有 Requirement UUID）"
    )


class BatchPublishItemOutput(BaseModel):  # type: ignore[misc]
    """批量发布单条结果（成功或失败均返回，便于 PM 对账）。"""

    client_ref: str
    batch_id: uuid.UUID | None = None
    status: str | None = None
    mode: str | None = None
    decision: str | None = None
    conflict_hint: dict[str, Any] | None = None
    created: list[dict[str, Any]] | None = None
    edges_created: int | None = None
    error: str | None = None


class PublishRequirementsOutput(BaseModel):  # type: ignore[misc]
    """批量发布出参：同一 submission_id 下每条需求一个审批批次。"""

    submission_id: uuid.UUID
    items: list[BatchPublishItemOutput]


class ValidateRequirementItemOutput(BaseModel):  # type: ignore[misc]
    """dry-run 单条预检结果。"""

    client_ref: str
    ok: bool
    errors: list[str] = Field(default=[])
    warnings: list[str] = Field(default=[])
    conflicts: list[dict[str, Any]] = Field(default=[])
    suggestion: str | None = None


class ValidateRequirementsOutput(BaseModel):  # type: ignore[misc]
    """validate_requirements 出参（不产生任何批次）。"""

    items: list[ValidateRequirementItemOutput]


# ============================================================================
# 开发产物 Pydantic 模型（submit_dev_artifacts）
# ============================================================================


class _ArtifactRefInput(StrictInputModel):
    """产物输入基类：ref 公共字段（批次内引用名）。

    四类产物（code/solution/intent/pitfall）均以 ref 作为批次内唯一引用键，
    供 relations 的 from_ref/to_ref 在审批通过时解析（见 approval/executor._resolve_ref）。
    """

    ref: str = Field(
        description="批次内引用名（如 'LoginService'），供 relations 中 from_ref/to_ref 引用"
    )


class CodeSnippetInput(_ArtifactRefInput):
    """代码片段。"""

    ref: str = Field(
        description="批次内引用名（如 'LoginService'），供 relations 中 from_ref/to_ref 引用"
    )
    title: str = Field(description="代码片段标题")
    content: str = Field(description="代码片段描述/关键代码")
    properties: dict[str, Any] = Field(
        description=(
            "CodeSnippet 属性，必填：name（名称）、type（class/function/module）、"
            "responsibility（职责）、file_path（文件路径）；可选：signature、snippet、language"
        )
    )
    tags: list[str] = Field(default=[], description="标签数组")


class SolutionInput(_ArtifactRefInput):
    """实现方案。"""

    ref: str = Field(description="批次内引用名")
    title: str = Field(description="方案标题")
    content: str = Field(description="方案描述")
    properties: dict[str, Any] = Field(
        description="Solution 属性，必填：version（版本号）、approach（采用的方案）；可选：alternatives（备选方案）"
    )
    tags: list[str] = Field(default=[], description="标签数组")


class DesignIntentInput(_ArtifactRefInput):
    """设计意图。"""

    ref: str = Field(description="批次内引用名")
    title: str = Field(description="意图标题")
    content: str = Field(description="意图描述")
    properties: dict[str, Any] = Field(
        description="DesignIntent 属性，必填：rationale（理由）、trade_offs（权衡）；可选：references"
    )
    tags: list[str] = Field(default=[], description="标签数组")


class PitfallInput(_ArtifactRefInput):
    """踩坑记录。"""

    ref: str = Field(description="批次内引用名")
    title: str = Field(description="踩坑标题")
    content: str = Field(description="踩坑描述")
    properties: dict[str, Any] = Field(
        description=(
            "Pitfall 属性，必填：symptom（症状）、root_cause（根因）、"
            "solution（解决方案）；severity（严重程度，枚举 P0/P1/P2/P3，存在则校验）"
        )
    )
    tags: list[str] = Field(default=[], description="标签数组")


class ArtifactRelationInput(StrictInputModel):
    """产物间关系（含临时引用）。"""

    from_ref: str = Field(
        description="源引用（ref 名 / UUID 字符串）"
    )
    to_ref: str = Field(
        description="目标引用（ref 名 / UUID 字符串）"
    )
    relation_type: str = Field(
        description=(
            "关系类型：implements（需求→代码）、depends_on（代码→代码）、"
            "realized_by（代码→方案）、embodies（方案→意图）、"
            "traces_to（代码→意图）、described_by（代码→踩坑）、references（通用引用）"
        )
    )
    properties: dict[str, Any] = Field(default={}, description="边属性")


class ArtifactsInput(StrictInputModel):
    """开发产物集合。"""

    code_snippets: list[CodeSnippetInput] = Field(
        default=[], description="代码片段列表"
    )
    solutions: list[SolutionInput] = Field(default=[], description="实现方案列表")
    design_intents: list[DesignIntentInput] = Field(
        default=[], description="设计意图列表"
    )
    pitfalls: list[PitfallInput] = Field(default=[], description="踩坑记录列表")


# ============================================================================
# 工具注册
# ============================================================================


async def _publish_requirements_batch(
    *,
    system_id: uuid.UUID,
    requirements: list[BatchRequirementInput],
    submission_id: uuid.UUID | None,
    operation_id: str | None,
) -> PublishRequirementsOutput:
    """批量发布核心：先全量预检，再逐条建批次（同一 submission_id）。

    每条需求仍是一个独立 approval_batch：保留逐条冲突检测/自动通过语义，
    避免单批次 all-or-nothing 与 50 item 上限。任一条预检失败则整次调用拒绝，
    不产生任何批次；运行期单条失败以 error 记录并继续其余条目。
    """
    if not requirements:
        raise PayloadValidationError("requirements 不能为空")
    if len(requirements) > get_settings().MAX_ITEMS_PER_BATCH:
        raise PayloadValidationError(
            f"requirements 数量 {len(requirements)} 超过上限 "
            f"{get_settings().MAX_ITEMS_PER_BATCH}"
        )

    refs = [it.client_ref for it in requirements]
    if len(set(refs)) != len(refs):
        raise PayloadValidationError("client_ref 在同一批提交内必须唯一")

    key_id = get_current_key_id()
    for it in requirements:
        if it.project_id is not None:
            validate_project_access(it.project_id)
        _check_content_length(
            it.requirement.content, f"Requirement.content[{it.client_ref}]"
        )
        validate_node("Requirement", it.requirement.properties)
        if it.related:
            ref_ids = [*(it.related.supersedes or []), *(it.related.relates_to or [])]
            await _validate_requirement_refs(
                it.project_id, ref_ids, f"{it.client_ref} related 引用"
            )
        if operation_id is not None and len(f"{operation_id}:{it.client_ref}") > 128:
            raise PayloadValidationError(
                f"operation_id + client_ref 超过 128 字符: {it.client_ref}"
            )

    effective_submission_id = submission_id or uuid.uuid4()
    results: list[BatchPublishItemOutput] = []
    for it in requirements:
        item_operation_id = (
            f"{operation_id}:{it.client_ref}" if operation_id is not None else None
        )
        try:
            items = _build_publish_items(
                it.project_id, it.requirement, it.related, key_id, system_id
            )
            out = await submit_write_batch(
                project_id=it.project_id,
                batch_type="publish_requirement",
                submitter_role="pm",
                items=items,
                operation_id=item_operation_id,
                submission_id=effective_submission_id,
            )
            if out.submission_id is not None:
                effective_submission_id = out.submission_id
            results.append(
                BatchPublishItemOutput(
                    client_ref=it.client_ref,
                    batch_id=out.batch_id,
                    status=out.status,
                    mode=out.mode,
                    decision=out.decision,
                    conflict_hint=out.conflict_hint,
                    created=out.created,
                    edges_created=out.edges_created,
                )
            )
        except Exception as e:  # noqa: BLE001 - 单条失败不阻断其余条目
            results.append(
                BatchPublishItemOutput(client_ref=it.client_ref, error=str(e))
            )

    return PublishRequirementsOutput(
        submission_id=effective_submission_id,
        items=results,
    )


async def _update_requirements_batch(
    *,
    node_ids: list[uuid.UUID],
    tags_add: list[str] | None,
    tags_remove: list[str] | None,
    properties_patch: dict[str, Any] | None,
    operation_id: str | None,
) -> WriteToolOutput:
    """批量更新需求标签/属性：显式 node_ids，合并后逐节点生成 update item。"""
    if not node_ids:
        raise PayloadValidationError("node_ids 不能为空")
    if len(node_ids) > get_settings().MAX_ITEMS_PER_BATCH:
        raise PayloadValidationError(
            f"node_ids 数量 {len(node_ids)} 超过上限 {get_settings().MAX_ITEMS_PER_BATCH}"
        )
    if len(set(node_ids)) != len(node_ids):
        raise PayloadValidationError("node_ids 不能重复")
    if not tags_add and not tags_remove and properties_patch is None:
        raise PayloadValidationError(
            "至少提供 tags_add/tags_remove/properties_patch 之一"
        )
    if properties_patch is not None and not isinstance(properties_patch, dict):
        raise PayloadValidationError("properties_patch 必须为 JSON 对象")

    await _validate_requirement_refs(
        None, [str(nid) for nid in node_ids], "批量更新引用"
    )
    async with readonly_session() as session:
        nodes = await get_nodes_by_ids(
            session, node_ids=list(node_ids), status="approved"
        )
    if len(nodes) != len(node_ids):
        found = {n.id for n in nodes}
        missing = [str(nid) for nid in node_ids if nid not in found]
        raise PayloadValidationError(f"批量更新目标不存在或未审批: {missing}")

    remove_set = set(tags_remove or [])
    items: list[dict[str, Any]] = []
    for node in nodes:
        original_tags = list(node.tags or [])
        new_tags = list(original_tags)
        for tag in tags_add or []:
            if tag not in new_tags:
                new_tags.append(tag)
        if remove_set:
            new_tags = [t for t in new_tags if t not in remove_set]

        new_props = None
        if properties_patch is not None:
            new_props = dict(node.properties or {})
            new_props.update(properties_patch)
            validate_node("Requirement", new_props)

        tags_arg = new_tags if new_tags != original_tags else None
        if tags_arg is None and new_props is None:
            continue
        items.append(
            build_update_node_item(
                node_id=node.id,
                node_type=node.type,
                properties=new_props,
                tags=tags_arg,
            )
        )

    if not items:
        raise PayloadValidationError("没有实际发生变更的节点")

    return await submit_write_batch(
        project_id=None,
        batch_type="update_node",
        submitter_role=get_current_role(),
        items=items,
        operation_id=operation_id,
    )


def _requirement_lint_warnings(requirement: RequirementInput) -> list[str]:
    """需求内容软 lint：只提示，不阻断提交。"""
    warnings: list[str] = []
    criteria = (requirement.properties or {}).get("acceptance_criteria")
    if not isinstance(criteria, str) or not criteria.strip():
        warnings.append("建议补充 acceptance_criteria（验收标准）")
    if "待确认" in (requirement.tags or []):
        warnings.append("含「待确认」标签：产品确认后用 update_requirements 批量清理")
    markers = [
        m
        for m in ("索引", "幂等键", "SQL", "表名", "接口字段", "数据库表")
        if m in (requirement.content or "")
    ]
    if markers:
        warnings.append(
            "内容含实现细节（"
            + "、".join(markers)
            + "），建议移到 Dev 的 DesignIntent/Solution"
        )
    return warnings


async def _validate_requirements_core(
    *,
    system_id: uuid.UUID,
    requirements: list[BatchRequirementInput],
    threshold: float | None,
    include_conflicts: bool,
    embedding_client: Any,
    graph_store: Any,
) -> ValidateRequirementsOutput:
    """发布前 dry-run：字段/引用预检 + 冲突预览，不写库、不建批次。"""
    if not requirements:
        raise PayloadValidationError("requirements 不能为空")
    if len(requirements) > get_settings().MAX_ITEMS_PER_BATCH:
        raise PayloadValidationError(
            f"requirements 数量 {len(requirements)} 超过上限 "
            f"{get_settings().MAX_ITEMS_PER_BATCH}"
        )
    refs = [it.client_ref for it in requirements]
    if len(set(refs)) != len(refs):
        raise PayloadValidationError("client_ref 在同一批预检内必须唯一")

    if threshold is None:
        threshold = get_settings().CONFLICT_SIMILARITY_THRESHOLD
    suggest_threshold = get_settings().CONFLICT_SUGGEST_PENDING

    results: list[ValidateRequirementItemOutput] = []
    for it in requirements:
        errors: list[str] = []
        if it.project_id is not None:
            try:
                validate_project_access(it.project_id)
            except Exception as e:  # noqa: BLE001 - 预检收集全部错误
                errors.append(str(e))
        try:
            _check_content_length(
                it.requirement.content, f"Requirement.content[{it.client_ref}]"
            )
            validate_node("Requirement", it.requirement.properties)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        if it.related:
            try:
                ref_ids = [
                    *(it.related.supersedes or []),
                    *(it.related.relates_to or []),
                ]
                await _validate_requirement_refs(
                    it.project_id, ref_ids, f"{it.client_ref} related 引用"
                )
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

        conflicts: list[dict[str, Any]] = []
        suggestion = None
        if include_conflicts and not errors:
            query_text = build_embed_text(
                "Requirement",
                it.requirement.title,
                it.requirement.content,
                it.requirement.properties,
            )
            result = await hybrid_search(
                query=query_text,
                embedding_client=embedding_client,
                graph_store=graph_store,
                top_n=10,
                filters=FilterSpec(
                    project_id=it.project_id,
                    system_id=system_id,
                    node_types=("Requirement",),
                ),
            )
            raw = [
                r
                for r in result.get("fused", [])
                if r.score is not None and r.score >= threshold
            ]
            ready_ids: set[uuid.UUID] = set()
            if raw:
                async with readonly_session() as session:
                    ready_ids = await get_node_ids_with_vectors(
                        session, [r.node_id for r in raw]
                    )
            conflicts = [
                {
                    "node_id": str(r.node_id),
                    "title": r.title,
                    "score": r.score,
                    "vector_ready": r.node_id in ready_ids,
                }
                for r in raw
            ]
            if conflicts:
                max_score = max(float(c["score"]) for c in conflicts)
                suggestion = (
                    "manual_merge" if max_score >= suggest_threshold else "review"
                )

        results.append(
            ValidateRequirementItemOutput(
                client_ref=it.client_ref,
                ok=not errors,
                errors=errors,
                warnings=_requirement_lint_warnings(it.requirement),
                conflicts=conflicts,
                suggestion=suggestion,
            )
        )

    return ValidateRequirementsOutput(items=results)


async def _resolve_requirement_ref(ref: str) -> uuid.UUID:
    """把 requirement_key / external_id / UUID 字符串解析为节点 UUID。"""
    try:
        return uuid.UUID(ref)
    except (ValueError, TypeError, AttributeError):
        pass
    async with readonly_session() as session:
        rows = await find_requirements_by_ref(session, ref)
    if not rows:
        raise PayloadValidationError(
            f"需求引用不存在: {ref}（requirement_key 或 external_id）"
        )
    if len(rows) > 1:
        raise PayloadValidationError(
            f"需求引用不唯一: {ref}，命中 {len(rows)} 条，请改用节点 UUID"
        )
    return cast(uuid.UUID, rows[0].id)


async def _submit_requirement_relations(
    *,
    project_id: uuid.UUID | None,
    relations: list[RelationInput],
    operation_id: str | None,
) -> WriteToolOutput:
    """需求关系提交核心：可选项目锚点 + 按两端需求自身归属鉴权。

    project_id 仅为兼容显式项目场景；省略时由 _validate_requirement_refs
    通过 is_requirement_visible 校验 project/system 两级可见性。
    """
    if project_id is not None:
        validate_project_access(project_id)
    if not relations:
        raise PayloadValidationError("relations 不能为空")

    resolved: list[tuple[uuid.UUID, uuid.UUID, RelationInput]] = []
    for r in relations:
        from_id = (
            r.from_id
            if r.from_id is not None
            else await _resolve_requirement_ref(r.from_ref or "")
        )
        to_id = (
            r.to_id
            if r.to_id is not None
            else await _resolve_requirement_ref(r.to_ref or "")
        )
        resolved.append((from_id, to_id, r))

    ref_ids = [
        str(node_id) for from_id, to_id, _ in resolved for node_id in (from_id, to_id)
    ]
    await _validate_requirement_refs(project_id, ref_ids, "需求关系引用")

    items = [
        build_edge_item(
            from_ref=str(from_id),
            to_ref=str(to_id),
            edge_type=r.relation_type,
            properties=r.properties,
        )
        for from_id, to_id, r in resolved
    ]

    return await submit_write_batch(
        project_id=project_id,
        batch_type="update_requirement_relations",
        submitter_role="pm",
        items=items,
        operation_id=operation_id,
    )


def register_write_tools(mcp: FastMCP) -> None:
    """注册写入类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def publish_requirement(
        system_id: uuid.UUID = Field(description="归属 system 域（需求跨项目建模）"),
        requirement: RequirementInput = Field(description="需求节点内容"),
        project_id: uuid.UUID | None = Field(
            default=None, description="归属项目（可选；None=悬浮 system 需求）"
        ),
        related: RelatedInput | None = Field(
            default=None, description="关联关系（supersedes/relates_to）"
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """发布需求节点（含关联关系），产生审批批次等待 admin 审批。

        PM 工具。需求默认按 system 域隔离（system_id 必填）；project_id 可选，None 表示
        "先于实现"的悬浮需求。审批通过后才写入知识图谱并参与检索。
        需求主键（如 HIS-0001）由服务端按 system 域自动分配并存储于节点，经
        list_requirements 或 get_requirement_context 查询；调用方无需、也不应传入
        requirement_id。需求间的重复/矛盾判定基于内容语义相似度（L3）。
        若当前 Access Key 为宽松模式（lax_mode=true 且全局开关开启）：无冲突时提交即自动
        直接入库（返回 status="approved" + decision="auto_approved"），有冲突返回
        decision="needs_human_review" 并停在待审批。
        支持 operation_id 幂等：同 operation_id 重复提交返回首次 batch_id。
        related.supersedes/relates_to 中的 requirement_id 必须为已有 Requirement 节点的 UUID。
        """
        try:
            if project_id is not None:
                validate_project_access(project_id)
            _check_content_length(requirement.content, "Requirement.content")
            key_id = get_current_key_id()
            # 提交前校验引用存在且类型为 Requirement，避免错误引用延迟到审批通过才失败
            if related:
                ref_ids = [
                    *(related.supersedes or []),
                    *(related.relates_to or []),
                ]
                await _validate_requirement_refs(
                    project_id, ref_ids, "related 引用"
                )
            items = _build_publish_items(
                project_id, requirement, related, key_id, system_id
            )

            return await submit_write_batch(
                project_id=project_id,
                batch_type="publish_requirement",
                submitter_role="pm",
                items=items,
                operation_id=operation_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)  # type: ignore[untyped-decorator]
    async def publish_requirements(
        system_id: uuid.UUID = Field(description="归属 system 域（本批需求共用）"),
        requirements: list[BatchRequirementInput] = Field(
            description="需求列表（每条一个审批批次；client_ref 批内唯一）"
        ),
        submission_id: uuid.UUID | None = Field(
            default=None,
            description="批量提交分组 ID；重试时传回首次返回值可保持同一分组",
        ),
        operation_id: str | None = Field(
            default=None,
            description="幂等前缀；每条实际键为 <operation_id>:<client_ref>",
        ),
    ) -> PublishRequirementsOutput:
        """批量发布需求：一个 submission_id 下每条需求一个审批批次。

        PM 工具。先对全部条目做 schema/内容/引用预检，任一失败整次拒绝（不产生
        批次）；预检通过后逐条提交，保留逐条冲突检测与宽松模式自动通过语义。
        返回每条 client_ref 对应的 batch_id/status/mode/decision/conflict_hint，
        便于一次提交后按 client_ref 对账。
        """
        try:
            return await _publish_requirements_batch(
                system_id=system_id,
                requirements=requirements,
                submission_id=submission_id,
                operation_id=operation_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=READ_TOOL_ANNOTATIONS)  # type: ignore[untyped-decorator]
    async def validate_requirements(
        system_id: uuid.UUID = Field(description="归属 system 域"),
        requirements: list[BatchRequirementInput] = Field(
            description="待预检需求列表（与 publish_requirements 同构）"
        ),
        threshold: float | None = Field(
            default=None, description="冲突相似度阈值；None 用 CONFLICT_SIMILARITY_THRESHOLD"
        ),
        include_conflicts: bool = Field(
            default=True, description="是否执行语义冲突预检（false 只做字段/引用校验）"
        ),
    ) -> ValidateRequirementsOutput:
        """发布前 dry-run：字段/引用/重复预检 + 冲突预览，不写库、不建批次。

        PM 工具。逐条返回 ok/errors/conflicts/suggestion；有 errors 的条目
        不会执行冲突检索。用于批量录入前先修正问题，减少无效审批批次。
        """
        try:
            lifespan_ctx = get_lifespan_context()
            return await _validate_requirements_core(
                system_id=system_id,
                requirements=requirements,
                threshold=threshold,
                include_conflicts=include_conflicts,
                embedding_client=lifespan_ctx.embedding_client,
                graph_store=lifespan_ctx.graph_store,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def update_requirement_relations(
        relations: list[RelationInput] = Field(description="需求间关系列表"),
        project_id: uuid.UUID | None = Field(
            default=None,
            description="归属项目 ID（可选；悬浮需求关系可省略，按两端需求自身 "
            "project/system 归属鉴权）",
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """更新需求间关系（冲突/关联/替代），产生审批批次等待 admin 审批。
        若当前 Access Key 为宽松模式（lax_mode=true 且全局开关开启）：无冲突时提交即自动
        直接入库（status="approved" + decision="auto_approved"），有冲突停在待审批。

        PM 工具。批量添加需求节点间的关系边，审批通过后写入知识图谱。
        from_id/to_id 必须为已有 Requirement 节点的 UUID。
        project_id 可省略：两端需求按自身归属（project 或 system）校验可见性，
        悬浮需求不再需要一个无关项目作为权限锚点。
        支持 operation_id 幂等。
        """
        try:
            return await _submit_requirement_relations(
                project_id=project_id,
                relations=relations,
                operation_id=operation_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def submit_dev_artifacts(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        artifacts: ArtifactsInput = Field(
            description="开发产物集合（代码片段/方案/意图/踩坑）"
        ),
        relations: list[ArtifactRelationInput] = Field(
            default=[],
            description="产物间关系（from_ref/to_ref 可用 ref 名或 UUID）",
        ),
        requirement_id: uuid.UUID | None = Field(
            default=None,
            description=(
                "关联的需求 ID。传入时四类产物按类型自动建边：CodeSnippet→implements、"
                "Solution→realized_by、DesignIntent→embodies、Pitfall→described_by；"
                "省略时产物挂本项目 ProjectProfile，relations 照常解析"
            ),
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """批量提交开发产物（代码/方案/意图/踩坑），产生审批批次；宽松模式无冲突时自动入库。

        relations 用 ref 机制引用：artifacts 声明 ref 名，relations 的 from_ref/to_ref
        可用批次内 ref 名或已有节点 UUID。传入 requirement_id 时四类产物按类型自动建边
        （implements/realized_by/embodies/described_by）；产物间其他关系（如 depends_on）
        须在 relations 显式声明。支持 operation_id 幂等。
        """
        try:
            validate_project_access(project_id)
            # 游离知识点（无需求）：解析本项目最新 ProjectProfile 节点 ID
            profile_id = (
                await _get_project_profile_id(project_id)
                if requirement_id is None
                else None
            )
            await _validate_dev_artifacts(
                project_id=project_id,
                requirement_id=requirement_id,
                artifacts=artifacts,
                relations=relations,
            )
            key_id = get_current_key_id()
            items = _build_dev_items(
                project_id=project_id,
                requirement_id=requirement_id,
                artifacts=artifacts,
                relations=relations,
                created_by=key_id,
                profile_id=profile_id,
            )

            if not items:
                raise PayloadValidationError("artifacts 和 relations 不能同时为空")

            return await submit_write_batch(
                project_id=project_id,
                batch_type="submit_dev_artifacts",
                submitter_role="dev",
                items=items,
                operation_id=operation_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def update_node(
        node_id: uuid.UUID = Field(description="要更新的已审批节点 UUID"),
        project_id: uuid.UUID | None = Field(
            default=None,
            description=(
                "节点所属项目 ID；留空 = 更新悬浮需求"
                "（目标节点须无项目归属且对当前 Key 可见）"
            ),
        ),
        title: str | None = Field(default=None, description="新标题；留空则不更新"),
        content: str | None = Field(default=None, description="新正文；留空则不更新"),
        properties: dict[str, Any] | None = Field(
            default=None,
            description=(
                "新属性字典，整体替换原属性（不会深度合并）；留空则不更新。"
                "整体替换后会重新校验该节点类型的必填字段"
            ),
        ),
        tags: list[str] | None = Field(default=None, description="新标签列表；留空则不更新"),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """更新已审批通过节点的内容（标题/正文/属性/标签），产生审批批次等待 admin 审批。
        若当前 Access Key 为宽松模式（lax_mode=true 且全局开关开启）：无冲突时提交即自动
        直接入库（status="approved" + decision="auto_approved"），有冲突停在待审批。

        PM/Dev 工具。用于修正已写入知识图谱的错误节点内容（写错且审批通过的场景）。
        角色×类型闸门：PM 可改需求与研发资产节点；Dev 仅可改研发资产节点
        （CodeSnippet/Solution/DesignIntent/Decision/Pitfall），需求内容归 PM 维护；
        ProjectProfile 由 admin 用 manage_project_profile 维护，本工具不可改。
        project_id 留空 = 更新悬浮需求（目标节点须为无项目归属的 Requirement，
        且对当前 Key 可见）；项目内节点必须传 project_id。
        审批通过后原子落地：版本号 +1、标题变更同步 AGE 图投影、标题/正文/属性任一
        变更均重新生成向量（embed 输入含属性段）、写审计日志。
        节点类型不可变更；Node 不存在/不属于本项目/已归档则拒绝。
        支持 operation_id 幂等。
        """
        try:
            return await _update_node_impl(
                project_id=project_id,
                node_id=node_id,
                title=title,
                content=content,
                properties=properties,
                tags=tags,
                operation_id=operation_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)  # type: ignore[untyped-decorator]
    async def update_requirements(
        node_ids: list[uuid.UUID] = Field(
            min_length=1,
            max_length=get_settings().MAX_ITEMS_PER_BATCH,
            description="待更新的需求节点 ID 列表（显式指定，禁止隐式全量）",
        ),
        tags_add: list[str] | None = Field(
            default=None, description="追加标签（已存在则跳过）"
        ),
        tags_remove: list[str] | None = Field(
            default=None, description="移除标签（不存在则忽略）"
        ),
        properties_patch: dict[str, Any] | None = Field(
            default=None,
            description="属性增量补丁（仅覆盖传入键，合并后按 Requirement schema 重校验）",
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """批量更新需求标签/属性（一个审批批次，逐节点 update item）。

        PM/Dev 共享。用于「11 条待确认需求统一加标签」「产品确认后批量改 module/
        优先级」等治理场景，避免逐条 update_node 产生 N 个批次。
        必须显式传 node_ids；合并后无实际变更的节点会跳过。
        """
        try:
            return await _update_requirements_batch(
                node_ids=node_ids,
                tags_add=tags_add,
                tags_remove=tags_remove,
                properties_patch=properties_patch,
                operation_id=operation_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)  # type: ignore[untyped-decorator]  # 与文件内其他工具同源噪音（9c68be5 同款）
    async def bind_requirement_to_project(
        requirement_ids: list[uuid.UUID] = Field(
            min_length=1,
            max_length=get_settings().MAX_ITEMS_PER_BATCH,
            description="待绑定/解绑的需求节点 ID 列表（保序去重，逐项生成审批项；上限=批次 item 上限）",
        ),
        project_id: uuid.UUID | None = Field(
            default=None,
            description=(
                "目标项目 ID；None=解绑回悬浮。绑定要求目标项目在需求的 system "
                "归属清单内（否则提示 manage_system add_projects）"
            ),
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """悬浮需求 ↔ 项目归属绑定/解绑（ISSUE-15）。

        PM/Admin 治理操作，走审批流但不触发冲突检测；system_id 不变；
        已是目标归属的项幂等跳过；解绑传 project_id=None。回执 affected 为
        绑定确定性依据（底层 node+update 载荷 → 版本+1、审计、图属性同步）。
        """
        try:
            if project_id is not None:
                validate_project_access(project_id)
            uniq = list(dict.fromkeys(requirement_ids))

            affected: list[dict[str, Any]] = []
            async with readonly_session() as session:
                for rid in uniq:
                    try:
                        node = await get_node(session, rid)
                    except NodeNotFoundError:
                        raise PayloadValidationError(f"需求节点不存在: {rid}")
                    # 可见性（含悬浮需求的 system 口径）+ 类型约束
                    await _ensure_requirement_visible(
                        session,
                        node,
                        type_err=f"节点 {rid} 类型为 {node.type}，绑定工具仅支持 Requirement",
                        invisible_err=f"需求节点 {rid} 对当前 Key 不可见",
                    )
                    if node.project_id == project_id:
                        continue  # 幂等：已是目标归属
                    # Dev 边界：仅可把悬浮需求绑定到自己项目；解绑/改绑归 PM
                    _check_bind_dev_boundary(
                        role=get_current_role(),
                        target_project_id=project_id,
                        node_project_id=node.project_id,
                    )
                    # 改绑/解绑：源项目须对调用者可见（防越权操作他人项目节点）
                    if node.project_id is not None:
                        validate_project_access(node.project_id)
                    if project_id is not None:
                        # 归属校验：目标项目必须在需求的 system 清单内
                        if node.system_id is None:
                            raise PayloadValidationError(
                                f"需求 {rid} 无 system 归属，无法判定项目归属清单"
                            )
                        member_ids = await get_system_project_ids(
                            session, system_id=node.system_id
                        )
                        if project_id not in member_ids:
                            sys_row = await get_system(session, node.system_id)
                            sys_name = sys_row.name if sys_row else str(node.system_id)
                            raise PayloadValidationError(
                                f"目标项目不在需求的 system「{sys_name}」归属清单，"
                                f"{_ADD_PROJECTS_FIX}"
                            )
                    affected.append(
                        {
                            "node_id": str(node.id),
                            "node_type": node.type,
                            "title": node.title,
                            "project_id": str(project_id) if project_id else None,
                        }
                    )

            if not affected:
                raise PayloadValidationError("全部需求已在目标归属中，无需变更")

            items = [
                {
                    "item_type": "node",
                    "action": "update",
                    "entity_type": "Requirement",
                    "payload": {
                        "node_id": a["node_id"],
                        "project_id": a["project_id"],
                    },
                }
                for a in affected
            ]
            return await submit_write_batch(
                project_id=project_id,
                batch_type="bind_requirement",
                submitter_role=get_current_role(),
                items=items,
                operation_id=operation_id,
                affected=affected,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e) from e


# ============================================================================
# items 构造辅助
# ============================================================================


async def _ensure_requirement_visible(
    session: AsyncSession, node: KnowledgeNode, *, type_err: str, invisible_err: str
) -> None:
    """单节点 Requirement 类型 + 调用者可见性校验（错误文案由调用方给定）。"""
    if node.type != "Requirement":
        raise PayloadValidationError(type_err)
    if not await is_requirement_visible(
        session,
        req_node=node,
        role=get_current_role(),
        project_scope=get_current_project_scope(),
        system_scope=get_current_system_scope(),
    ):
        raise PayloadValidationError(invisible_err)


async def _validate_requirement_refs(
    project_id: uuid.UUID | None, ref_ids: list[str], label: str
) -> None:
    """提交前校验引用的 Requirement 节点存在、类型正确、且对调用者可见。

    用于 publish_requirement 的 related 与 update_requirement_relations 的
    from_id/to_id（均为节点 UUID）。提交时拦截错误引用（否则延迟到审批通过时
    _execute_edge_create 的 get_node 才失败），审批体验更好。
    决策（同 system 即可引用）：引用的需求须对当前调用者可见（is_requirement_visible）。
    """
    if not ref_ids:
        return
    async with readonly_session() as session:
        try:
            uuids = [uuid.UUID(r) for r in ref_ids]
        except (ValueError, TypeError, AttributeError) as e:
            raise PayloadValidationError(
                f"{label} 含非法 UUID: {e}"
            ) from e

        nodes = await get_nodes_by_ids(
            session, node_ids=list(set(uuids)), status=None
        )
        node_map = {n.id: n for n in nodes}
        missing = [str(u) for u in set(uuids) if u not in node_map]
        if missing:
            raise PayloadValidationError(
                f"{label} 引用不存在: {missing}"
            )
        for n in nodes:
            await _ensure_requirement_visible(
                session,
                n,
                type_err=f"{label} 引用非 Requirement 类型: {n.id} ({n.type})",
                invisible_err=f"{label} 引用不在当前调用者可见范围: {n.id}",
            )


async def _get_project_profile_id(project_id: uuid.UUID) -> uuid.UUID | None:
    """解析本项目最新 ProjectProfile 节点 ID；不存在则返回 None。

    用于游离知识点（requirement_id 为空）时自动挂到项目画像节点。
    list_project_profiles 按 created_at 倒序，取第一条即为最新画像。
    """
    async with readonly_session() as session:
        profiles = await list_project_profiles(
            session, project_ids=[project_id], limit=1
        )
        return profiles[0].id if profiles else None


async def _validate_dev_artifacts(
    *,
    project_id: uuid.UUID,
    requirement_id: uuid.UUID | None,
    artifacts: ArtifactsInput,
    relations: list[ArtifactRelationInput],
) -> None:
    """提交前提前拦截批内 ref / requirement_id 不一致，避免延迟到审批期才失败。

    校验项：
    1. 产物 ref 在批次内必须唯一（重复 ref 会导致关系解析歧义）。
    2. requirement_id 必须存在且类型为 Requirement、归属本项目。
    3. 每条 relation 的 from_ref/to_ref：UUID 须存在（悬挂 UUID 拒）；
       非 UUID 须为批次内已声明的 ref 名；from_ref == to_ref 视为自引用拒。
    """
    declared_refs: list[str] = []
    declared_refs.extend(a.ref for a in artifacts.code_snippets)
    declared_refs.extend(a.ref for a in artifacts.solutions)
    declared_refs.extend(a.ref for a in artifacts.design_intents)
    declared_refs.extend(a.ref for a in artifacts.pitfalls)
    ref_set = set(declared_refs)
    if len(ref_set) != len(declared_refs):
        dup = sorted({r for r in declared_refs if declared_refs.count(r) > 1})
        raise PayloadValidationError(f"产物 ref 重复（批次内必须唯一）: {dup}")

    # requirement_id 存在性/类型/可见性；relations 引用校验
    async with readonly_session() as session:
        # 2. requirement_id 存在性 + 类型 + 对调用者可见（仅当显式提供需求时校验）
        if requirement_id is not None:
            try:
                req_node = await get_node(session, requirement_id)
            except NodeNotFoundError:
                raise PayloadValidationError(f"requirement_id 不存在: {requirement_id}")
            await _ensure_requirement_visible(
                session,
                req_node,
                type_err=f"requirement_id 类型非 Requirement: {req_node.type}",
                invisible_err=f"requirement_id 不在当前调用者可见范围: {requirement_id}",
            )

        # 3. relations 引用校验
        errors: list[str] = []
        for r in relations:
            for ref in (r.from_ref, r.to_ref):
                try:
                    uuid.UUID(str(ref))
                except (ValueError, TypeError, AttributeError):
                    # 非 UUID：必须是批次内已声明的 ref 名
                    if ref not in ref_set:
                        errors.append(f"未知 ref 名（未在 artifacts 声明）: {ref}")
                    continue
                # UUID：必须存在
                try:
                    await get_node(session, uuid.UUID(str(ref)))
                except NodeNotFoundError:
                    errors.append(f"引用 UUID 不存在: {ref}")
            if r.from_ref == r.to_ref:
                errors.append(f"自引用（from_ref == to_ref）: {r.from_ref}")
        if errors:
            raise PayloadValidationError("; ".join(errors))


def _build_publish_items(
    project_id: uuid.UUID | None,
    requirement: RequirementInput,
    related: RelatedInput | None,
    created_by: str,
    system_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """构造 publish_requirement 的 items 列表。

    结构：
    - 1 个 Requirement node item（ref="requirement"，含 system_id；project_id 可空=悬浮）
    - related.supersedes 每项 → 1 个 edge item（from_ref="requirement", edge_type="supersedes"）
    - related.relates_to 每项 → 1 个 edge item（from_ref="requirement", edge_type="relates_to"）
    """
    items: list[dict[str, Any]] = [
        build_node_item(
            ref="requirement",
            node_type="Requirement",
            title=requirement.title,
            content=requirement.content,
            properties=requirement.properties,
            tags=requirement.tags,
            project_id=project_id,
            system_id=system_id,
            created_by=created_by,
        )
    ]

    if related:
        for old_req_id in related.supersedes:
            items.append(
                build_edge_item(
                    from_ref="requirement",
                    to_ref=old_req_id,
                    edge_type="supersedes",
                )
            )
        for related_id in related.relates_to:
            items.append(
                build_edge_item(
                    from_ref="requirement",
                    to_ref=related_id,
                    edge_type="relates_to",
                )
            )

    return items


def _build_dev_items(
    *,
    project_id: uuid.UUID,
    requirement_id: uuid.UUID | None = None,
    artifacts: ArtifactsInput,
    relations: list[ArtifactRelationInput],
    created_by: str,
    profile_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """构造 submit_dev_artifacts 的 items 列表。

    结构：
    - code_snippets/solutions/design_intents/pitfalls 各项 → node item（含 ref）
    - 自动构造（requirement_id 存在时四类统一）：
      CodeSnippet→implements / Solution→realized_by / DesignIntent→embodies /
      Pitfall→described_by（边类型与 analyze_impact_scope 遍历链对齐，
      四类产物无需显式 relations 声明）
    - relations 中每项 → edge item（from_ref/to_ref 直接用输入字符串）
    """
    items: list[dict[str, Any]] = []

    # 1. 构造 node items（四类产物表驱动同构处理）
    for art_list, node_type in (
        (artifacts.code_snippets, "CodeSnippet"),
        (artifacts.solutions, "Solution"),
        (artifacts.design_intents, "DesignIntent"),
        (artifacts.pitfalls, "Pitfall"),
    ):
        for art in art_list:
            _check_content_length(art.content, f"{node_type}[{art.ref}].content")
            items.append(
                build_node_item(
                    ref=art.ref,
                    node_type=node_type,
                    title=art.title,
                    content=art.content,
                    properties=art.properties,
                    tags=art.tags,
                    project_id=project_id,
                    created_by=created_by,
                )
            )

    # 2. 自动构造 Requirement --> 产物 的语义边（仅当关联需求存在）。
    #    四类产物统一自动建边（implements/realized_by/embodies/described_by），
    #    与 analyze_impact_scope 遍历链对齐；坑/方案/意图无需显式 relations 声明。
    if requirement_id is not None:
        auto_edge_map = (
            (artifacts.code_snippets, "implements"),
            (artifacts.solutions, "realized_by"),
            (artifacts.design_intents, "embodies"),
            (artifacts.pitfalls, "described_by"),
        )
        for art_list, edge_type in auto_edge_map:
            for art in art_list:
                items.append(
                    build_edge_item(
                        from_ref=str(requirement_id),
                        to_ref=art.ref,
                        edge_type=edge_type,
                    )
                )

    # 2b. 游离知识点（无需求）：自动挂到 ProjectProfile 节点（若该节点存在）
    if profile_id is not None:
        for art in (
            *artifacts.code_snippets,
            *artifacts.solutions,
            *artifacts.design_intents,
            *artifacts.pitfalls,
        ):
            items.append(
                build_edge_item(
                    from_ref=str(profile_id),
                    to_ref=art.ref,
                    edge_type="references",
                )
            )

    # 3. 用户显式声明的 relations
    for r in relations:
        items.append(
            build_edge_item(
                from_ref=r.from_ref,
                to_ref=r.to_ref,
                edge_type=r.relation_type,
                properties=r.properties,
            )
        )

    return items


async def _update_node_impl(
    *,
    project_id: uuid.UUID | None,
    node_id: uuid.UUID,
    title: str | None,
    content: str | None,
    properties: dict[str, Any] | None,
    tags: list[str] | None,
    operation_id: str | None,
) -> WriteToolOutput:
    """update_node 核心逻辑（工具层薄封装，便于单测直接调用）。

    project_id=None 表示更新悬浮需求：目标节点必须是无项目归属的 Requirement，
    且对当前 Key 可见（is_requirement_visible，system 口径）。

    红线：提交的审批 payload **绝不能含 project_id 键**——执行器按
    `"project_id" in payload` 分流（命中 → set_node_project 解绑，未命中 → 内容更新）。
    """
    if project_id is not None:
        validate_project_access(project_id)
    if all(v is None for v in (title, content, properties, tags)):
        raise PayloadValidationError(
            "至少提供一个要变更的字段: title/content/properties/tags"
        )
    _check_content_length(content, f"节点 {node_id} 的 content")
    if properties is not None and not isinstance(properties, dict):
        raise PayloadValidationError("properties 必须为 JSON 对象")

    # 预校验目标节点存在、归属一致且未归档，并取节点类型用于审批项实体标识
    async with readonly_session() as session:
        try:
            node = await get_node(session, node_id)
        except NodeNotFoundError:
            raise PayloadValidationError(f"node_id 不存在: {node_id}")
        # 角色×节点类型闸门（需求内容归 PM，研发资产归 Dev）
        _check_update_node_type(node, role=get_current_role())
        if node.project_id != project_id:
            # 归属诊断：成功路径零额外查询，仅错误路径查 system 归属
            system_name: str | None = None
            member_ids: set[uuid.UUID] | None = None
            if node.system_id is not None:
                sys_row = await get_system(session, node.system_id)
                system_name = sys_row.name if sys_row else None
                member_ids = await get_system_project_ids(
                    session, system_id=node.system_id
                )
            raise PayloadValidationError(
                _ownership_mismatch_hint(
                    node,
                    project_id,
                    system_name=system_name,
                    system_member_ids=member_ids,
                )
            )
        if project_id is None:
            # 悬浮路径：仅支持 Requirement，且须对当前 Key 可见
            await _ensure_requirement_visible(
                session,
                node,
                type_err=(
                    f"悬浮更新仅支持 Requirement 节点，节点 {node_id} 类型为 {node.type}"
                ),
                invisible_err=f"需求节点 {node_id} 对当前 Key 不可见",
            )
        if node.status == "archived":
            raise PayloadValidationError(f"已归档节点不可更新: {node_id}")

    items = [
        build_update_node_item(
            node_id=node_id,
            node_type=node.type,
            title=title,
            content=content,
            properties=properties,
            tags=tags,
        )
    ]
    return await submit_write_batch(
        project_id=project_id,
        batch_type="update_node",
        submitter_role=get_current_role(),
        items=items,
        operation_id=operation_id,
    )


def _check_bind_dev_boundary(
    *, role: str, target_project_id: uuid.UUID | None, node_project_id: uuid.UUID | None
) -> None:
    """Dev 的 bind 边界：仅可把悬浮需求绑定到自己项目，不能解绑/改绑。

    PM 全权（绑定/解绑/改绑）。归属变更是治理动作，收在 PM 手里。
    目标项目是否属于 Dev 自身范围，由 validate_project_access 在入口处把关。

    调用点必须在幂等分支（node.project_id == target_project_id）之后 ——
    Dev 对自己项目的幂等绑定不应被误拒。
    """
    if role != "dev":
        return
    if target_project_id is None:
        raise PayloadValidationError(
            "Dev 角色不可解绑需求（解绑/改绑由 PM 执行）"
        )
    if node_project_id is not None:
        raise PayloadValidationError(
            "Dev 角色仅可绑定悬浮需求到自己项目；改绑已归属需求的请求请由 PM 执行"
        )
