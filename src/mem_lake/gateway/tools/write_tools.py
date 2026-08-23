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
- 工具层不写业务逻辑，仅参数解析 + items 构造 + 转发 submit_batch_with_mode
- 角色 RBAC 由中间件层控制，本文件不区分角色
"""

import logging
import uuid
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import Field

from mem_lake.approval.service import (
    PayloadValidationError,
    submit_batch_with_mode,
)
from mem_lake.gateway.dependencies import (
    get_current_key_id,
    get_current_lax_mode,
    get_current_role,
    get_readonly_session,
    transactional_session,
    validate_project_access,
)
from mem_lake.gateway.tools._shared import (
    WRITE_TOOL_ANNOTATIONS,
    StrictInputModel,
    WriteToolOutput,
    _safe_enqueue_embed,
    build_edge_item,
    build_node_item,
    build_update_node_item,
    to_tool_error,
)
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    get_node,
    get_nodes_by_ids,
    list_project_profiles,
)
from mem_lake.knowledge.schema import SchemaValidationError

logger = logging.getLogger("mem_lake.gateway.tools.write")

# 单产物 content 长度上限，防止超长文本入库（审批 + 向量化成本高）
MAX_CONTENT_LENGTH: int = 10000


def _check_content_length(value: str, label: str) -> None:
    """校验 content 长度不超过上限，超限抛 PayloadValidationError。"""
    if value is not None and len(value) > MAX_CONTENT_LENGTH:
        raise PayloadValidationError(
            f"{label} 长度 {len(value)} 超过上限 {MAX_CONTENT_LENGTH} 字符"
        )


# ============================================================================
# 需求相关 Pydantic 模型（publish_requirement / update_requirement_relations）
# ============================================================================


class RequirementInput(StrictInputModel):
    """需求节点内容。"""

    title: str = Field(description="需求标题")
    content: str = Field(description="需求详细描述")
    properties: dict[str, Any] = Field(
        description=(
            "需求属性，必填字段：requirement_id（需求编号）、priority（P0/P1/P2/P3）、"
            "module（模块）、acceptance_criteria（验收标准）；可选：source_doc、version"
        )
    )
    tags: list[str] = Field(default=[], description="标签数组")


class RelatedInput(StrictInputModel):
    """需求间关联关系。"""

    supersedes: list[str] = Field(
        default=[],
        description="被替代的旧需求 requirement_id 列表（自动构造 supersedes 边）",
    )
    relates_to: list[str] = Field(
        default=[],
        description="关联需求 requirement_id 列表（自动构造 relates_to 边）",
    )


class RelationInput(StrictInputModel):
    """需求间关系（用于 update_requirement_relations）。"""

    from_id: uuid.UUID = Field(description="源需求节点 ID")
    to_id: uuid.UUID = Field(description="目标需求节点 ID")
    relation_type: str = Field(
        description="关系类型：conflicts_with/duplicates/relates_to/supersedes/version_of"
    )
    properties: dict[str, Any] = Field(default={}, description="边属性")


# ============================================================================
# 开发产物 Pydantic 模型（submit_dev_artifacts）
# ============================================================================


class CodeSnippetInput(StrictInputModel):
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


class SolutionInput(StrictInputModel):
    """实现方案。"""

    ref: str = Field(description="批次内引用名")
    title: str = Field(description="方案标题")
    content: str = Field(description="方案描述")
    properties: dict[str, Any] = Field(
        description="Solution 属性，必填：version（版本号）、approach（采用的方案）；可选：alternatives（备选方案）"
    )
    tags: list[str] = Field(default=[], description="标签数组")


class DesignIntentInput(StrictInputModel):
    """设计意图。"""

    ref: str = Field(description="批次内引用名")
    title: str = Field(description="意图标题")
    content: str = Field(description="意图描述")
    properties: dict[str, Any] = Field(
        description="DesignIntent 属性，必填：rationale（理由）、trade_offs（权衡）；可选：references"
    )
    tags: list[str] = Field(default=[], description="标签数组")


class PitfallInput(StrictInputModel):
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
        description="源引用（ref 名 / requirement_id / UUID 字符串）"
    )
    to_ref: str = Field(
        description="目标引用（ref 名 / requirement_id / UUID 字符串）"
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


def _lax_lifespan_resources() -> tuple[Any, Any, Any]:
    """宽松模式下从 lifespan context 取图谱/嵌入/检索依赖。

    返回 (graph_store, embedding_client, vector_searcher)；strict 模式无需调用。
    """
    ctx = get_context()
    lifespan_ctx = ctx.lifespan_context
    return (
        lifespan_ctx.graph_store,
        lifespan_ctx.embedding_client,
        lifespan_ctx.vector_searcher,
    )


async def _finalize_lax_output(
    *, lax: bool, decision: str | None, batch, project_id: uuid.UUID
) -> WriteToolOutput:
    """宽松模式提交后收尾：已自动审批时异步入队补向量，并构造出参。

    strict 模式（lax=False）不触发入队，仅构造包含 decision=None 的出参。
    """
    created = [
        it.target_id
        for it in (batch.items or [])
        if it.item_type == "node"
        and it.action == "create"
        and it.target_id is not None
    ]
    if lax and decision == "auto_approved" and created:
        await _safe_enqueue_embed(project_id, created)
    return WriteToolOutput.from_batch(batch, decision=decision)


def register_write_tools(mcp: FastMCP) -> None:
    """注册写入类工具到 FastMCP 实例。"""

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def publish_requirement(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        requirement: RequirementInput = Field(description="需求节点内容"),
        related: RelatedInput | None = Field(
            default=None, description="关联关系（supersedes/relates_to）"
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """发布需求节点（含关联关系），产生审批批次等待 admin 审批。

        PM 工具。需求节点暂存于审批批次，审批通过后才写入知识图谱并参与检索。
        若当前 Access Key 为宽松模式（lax_mode=true 且全局开关开启）：无冲突时提交即自动
        直接入库（返回 status="approved" + decision="auto_approved"），有冲突返回
        decision="needs_human_review" 并停在待审批。
        支持 operation_id 幂等：同 operation_id 重复提交返回首次 batch_id。
        related.supersedes/relates_to 中的 requirement_id 必须为已有 Requirement 节点的 UUID。
        """
        try:
            validate_project_access(project_id)
            _check_content_length(requirement.content, "Requirement.content")
            key_id = get_current_key_id()
            # 提交前校验关联引用的 requirement_id 存在且类型为 Requirement
            # （AUDIT §2.15：此前缺存在性校验，错误引用延迟到审批通过才失败）
            if related:
                ref_ids = [
                    *(related.supersedes or []),
                    *(related.relates_to or []),
                ]
                await _validate_requirement_refs(
                    project_id, ref_ids, "related 引用"
                )
            items = _build_publish_items(project_id, requirement, related, key_id)

            async with transactional_session() as session:
                graph_store = embedding_client = vector_searcher = None
                if get_current_lax_mode():
                    graph_store, embedding_client, vector_searcher = (
                        _lax_lifespan_resources()
                    )
                batch, decision = await submit_batch_with_mode(
                    session,
                    project_id=project_id,
                    batch_type="publish_requirement",
                    submitted_by=key_id,
                    submitter_role="pm",
                    items=items,
                    operation_id=operation_id,
                    lax_mode=get_current_lax_mode(),
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    vector_searcher=vector_searcher,
                )
            return await _finalize_lax_output(
                lax=get_current_lax_mode(),
                decision=decision,
                batch=batch,
                project_id=project_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def update_requirement_relations(
        project_id: uuid.UUID = Field(description="归属项目 ID"),
        relations: list[RelationInput] = Field(description="需求间关系列表"),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """更新需求间关系（冲突/关联/替代），产生审批批次等待 admin 审批。
        若当前 Access Key 为宽松模式（lax_mode=true 且全局开关开启）：无冲突时提交即自动
        直接入库（status="approved" + decision="auto_approved"），有冲突停在待审批。

        PM 工具。批量添加需求节点间的关系边，审批通过后写入知识图谱。
        from_id/to_id 必须为已有 Requirement 节点的 UUID。
        支持 operation_id 幂等。
        """
        try:
            validate_project_access(project_id)
            if not relations:
                raise PayloadValidationError("relations 不能为空")

            # 提交前校验两端节点存在且类型为 Requirement（AUDIT §2.15：
            # 此前 docstring 声称"必须为 Requirement"但未实现校验）
            ref_ids = [
                *(str(r.from_id) for r in relations),
                *(str(r.to_id) for r in relations),
            ]
            await _validate_requirement_refs(
                project_id, ref_ids, "需求关系引用"
            )

            items = [
                build_edge_item(
                    from_ref=str(r.from_id),
                    to_ref=str(r.to_id),
                    edge_type=r.relation_type,
                    properties=r.properties,
                )
                for r in relations
            ]

            key_id = get_current_key_id()
            async with transactional_session() as session:
                graph_store = embedding_client = vector_searcher = None
                if get_current_lax_mode():
                    graph_store, embedding_client, vector_searcher = (
                        _lax_lifespan_resources()
                    )
                batch, decision = await submit_batch_with_mode(
                    session,
                    project_id=project_id,
                    batch_type="update_requirement_relations",
                    submitted_by=key_id,
                    submitter_role="pm",
                    items=items,
                    operation_id=operation_id,
                    lax_mode=get_current_lax_mode(),
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    vector_searcher=vector_searcher,
                )
            return await _finalize_lax_output(
                lax=get_current_lax_mode(),
                decision=decision,
                batch=batch,
                project_id=project_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

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
                "关联的需求节点 ID（自动为每个 CodeSnippet 构造 implements 边）。可选："
                "省略则提交「游离知识点」，系统自动把每个产物挂到本项目的 ProjectProfile "
                "节点（若该节点存在），无需绑定具体需求"
            ),
        ),
        operation_id: str | None = Field(
            default=None, description="幂等键，同 operation_id 重复提交返回首次结果"
        ),
    ) -> WriteToolOutput:
        """批量提交开发产物（代码片段+方案+意图+踩坑），产生审批批次等待 admin 审批。
        若当前 Access Key 为宽松模式（lax_mode=true 且全局开关开启）：无冲突时提交即自动
        直接入库（status="approved" + decision="auto_approved"），有冲突停在待审批。

        Dev 工具。审批通过后产物节点写入知识图谱。
        自动关系：
        - 传入 requirement_id 时，系统为每个 CodeSnippet 自动建立 Requirement--implements-->CodeSnippet 边。
        - 省略 requirement_id（游离知识点）时，系统把每个产物自动挂到本项目的
          ProjectProfile 节点（ProjectProfile--references-->产物）；若项目无 ProjectProfile 节点则仅入库不建边。
        坑(Pitfall)/方案(Solution)/设计意图(DesignIntent) 不会自动与需求建边，
        如需把它们关联到具体需求，须在 relations 中显式声明
        （from_ref/to_ref 用 ref 名或节点 UUID，relation_type 如 described_by/references 等）。
        使用 ref 机制在批次内引用未创建的节点：artifacts 中每个产物声明 ref 名，
        relations 中用 from_ref/to_ref 引用这些 ref 名（或已有节点的 UUID）。
        临时引用在审批通过时解析为实际节点 ID。
        支持 operation_id 幂等。
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

            async with transactional_session() as session:
                graph_store = embedding_client = vector_searcher = None
                if get_current_lax_mode():
                    graph_store, embedding_client, vector_searcher = (
                        _lax_lifespan_resources()
                    )
                batch, decision = await submit_batch_with_mode(
                    session,
                    project_id=project_id,
                    batch_type="submit_dev_artifacts",
                    submitted_by=key_id,
                    submitter_role="dev",
                    items=items,
                    operation_id=operation_id,
                    lax_mode=get_current_lax_mode(),
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    vector_searcher=vector_searcher,
                )
            return await _finalize_lax_output(
                lax=get_current_lax_mode(),
                decision=decision,
                batch=batch,
                project_id=project_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)

    @mcp.tool(annotations=WRITE_TOOL_ANNOTATIONS)
    async def update_node(
        project_id: uuid.UUID = Field(description="节点所属项目 ID"),
        node_id: uuid.UUID = Field(description="要更新的已审批节点 UUID"),
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
        审批通过后原子落地：版本号 +1、标题变更同步 AGE 图投影、标题/正文/属性任一
        变更均重新生成向量（embed 输入含属性段）、写审计日志。
        节点类型不可变更；Node 不存在/不属于本项目/已归档则拒绝。
        支持 operation_id 幂等。
        """
        try:
            validate_project_access(project_id)
            if all(v is None for v in (title, content, properties, tags)):
                raise PayloadValidationError(
                    "至少提供一个要变更的字段: title/content/properties/tags"
                )
            _check_content_length(content, f"节点 {node_id} 的 content")
            if properties is not None and not isinstance(properties, dict):
                raise PayloadValidationError("properties 必须为 JSON 对象")

            # 预校验目标节点存在、归属本项目且未归档，并取节点类型用于审批项实体标识
            session = await get_readonly_session()
            try:
                try:
                    node = await get_node(session, node_id)
                except NodeNotFoundError:
                    raise PayloadValidationError(f"node_id 不存在: {node_id}")
                if node.project_id != project_id:
                    raise PayloadValidationError(f"节点不属于本项目: {node_id}")
                if node.status == "archived":
                    raise PayloadValidationError(f"已归档节点不可更新: {node_id}")
            finally:
                await session.close()

            key_id = get_current_key_id()
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
            async with transactional_session() as session:
                graph_store = embedding_client = vector_searcher = None
                if get_current_lax_mode():
                    graph_store, embedding_client, vector_searcher = (
                        _lax_lifespan_resources()
                    )
                batch, decision = await submit_batch_with_mode(
                    session,
                    project_id=project_id,
                    batch_type="update_node",
                    submitted_by=key_id,
                    submitter_role=get_current_role(),
                    items=items,
                    operation_id=operation_id,
                    lax_mode=get_current_lax_mode(),
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    vector_searcher=vector_searcher,
                )
            return await _finalize_lax_output(
                lax=get_current_lax_mode(),
                decision=decision,
                batch=batch,
                project_id=project_id,
            )
        except (PayloadValidationError, SchemaValidationError, ValueError) as e:
            raise to_tool_error(e)


# ============================================================================
# items 构造辅助
# ============================================================================


async def _validate_requirement_refs(
    project_id: uuid.UUID, ref_ids: list[str], label: str
) -> None:
    """提交前校验引用的 requirement_id 存在、类型为 Requirement、归属本项目。

    用于 publish_requirement 的 related 与 update_requirement_relations 的
    from_id/to_id。此前缺该校验，错误引用延迟到审批通过时 _execute_edge_create
    的 get_node 才失败（AUDIT §2.15）。提交时拦截，审批体验更好。
    """
    if not ref_ids:
        return
    session = await get_readonly_session()
    try:
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
            if n.type != "Requirement":
                raise PayloadValidationError(
                    f"{label} 引用非 Requirement 类型: {n.id} ({n.type})"
                )
            if str(n.project_id) != str(project_id):
                raise PayloadValidationError(
                    f"{label} 引用不属于本项目: {n.id}"
                )
    finally:
        await session.close()


async def _get_project_profile_id(project_id: uuid.UUID) -> uuid.UUID | None:
    """解析本项目最新 ProjectProfile 节点 ID；不存在则返回 None。

    用于游离知识点（requirement_id 为空）时自动挂到项目画像节点。
    list_project_profiles 按 created_at 倒序，取第一条即为最新画像。
    """
    session = await get_readonly_session()
    try:
        profiles = await list_project_profiles(
            session, project_ids=[project_id], limit=1
        )
        return profiles[0].id if profiles else None
    finally:
        await session.close()


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
    # 1. 收集已声明 ref，检测重复
    declared_refs: list[str] = []
    declared_refs.extend(a.ref for a in artifacts.code_snippets)
    declared_refs.extend(a.ref for a in artifacts.solutions)
    declared_refs.extend(a.ref for a in artifacts.design_intents)
    declared_refs.extend(a.ref for a in artifacts.pitfalls)
    ref_set = set(declared_refs)
    if len(ref_set) != len(declared_refs):
        dup = sorted({r for r in declared_refs if declared_refs.count(r) > 1})
        raise PayloadValidationError(f"产物 ref 重复（批次内必须唯一）: {dup}")

    # 2 & 3：requirement_id 存在性 + 类型 + 归属项目；relations 引用校验
    session = await get_readonly_session()
    try:
        # 2. requirement_id 存在性 + 类型 + 归属项目（仅当显式提供需求时校验）
        if requirement_id is not None:
            try:
                req_node = await get_node(session, requirement_id)
            except NodeNotFoundError:
                raise PayloadValidationError(f"requirement_id 不存在: {requirement_id}")
            if req_node.type != "Requirement":
                raise PayloadValidationError(
                    f"requirement_id 类型非 Requirement: {req_node.type}"
                )
            if req_node.project_id != project_id:
                raise PayloadValidationError(
                    f"requirement_id 不属于本项目: {requirement_id}"
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
    finally:
        await session.close()


def _build_publish_items(
    project_id: uuid.UUID,
    requirement: RequirementInput,
    related: RelatedInput | None,
    created_by: str,
) -> list[dict[str, Any]]:
    """构造 publish_requirement 的 items 列表。

    结构：
    - 1 个 Requirement node item（ref="requirement"）
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
    - 自动构造：Requirement requirement_id --implements--> 每个 CodeSnippet
    - relations 中每项 → edge item（from_ref/to_ref 直接用输入字符串）
    """
    items: list[dict[str, Any]] = []

    # 1. 构造 node items
    for code in artifacts.code_snippets:
        _check_content_length(code.content, f"CodeSnippet[{code.ref}].content")
        items.append(
            build_node_item(
                ref=code.ref,
                node_type="CodeSnippet",
                title=code.title,
                content=code.content,
                properties=code.properties,
                tags=code.tags,
                project_id=project_id,
                created_by=created_by,
            )
        )

    for solution in artifacts.solutions:
        _check_content_length(solution.content, f"Solution[{solution.ref}].content")
        items.append(
            build_node_item(
                ref=solution.ref,
                node_type="Solution",
                title=solution.title,
                content=solution.content,
                properties=solution.properties,
                tags=solution.tags,
                project_id=project_id,
                created_by=created_by,
            )
        )

    for intent in artifacts.design_intents:
        _check_content_length(intent.content, f"DesignIntent[{intent.ref}].content")
        items.append(
            build_node_item(
                ref=intent.ref,
                node_type="DesignIntent",
                title=intent.title,
                content=intent.content,
                properties=intent.properties,
                tags=intent.tags,
                project_id=project_id,
                created_by=created_by,
            )
        )

    for pitfall in artifacts.pitfalls:
        _check_content_length(pitfall.content, f"Pitfall[{pitfall.ref}].content")
        items.append(
            build_node_item(
                ref=pitfall.ref,
                node_type="Pitfall",
                title=pitfall.title,
                content=pitfall.content,
                properties=pitfall.properties,
                tags=pitfall.tags,
                project_id=project_id,
                created_by=created_by,
            )
        )

    # 2. 自动构造 Requirement --implements--> CodeSnippet 关系（仅当关联需求存在）
    if requirement_id is not None:
        for code in artifacts.code_snippets:
            items.append(
                build_edge_item(
                    from_ref=str(requirement_id),
                    to_ref=code.ref,
                    edge_type="implements",
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
