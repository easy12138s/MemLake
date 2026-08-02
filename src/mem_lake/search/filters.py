"""统一过滤条件：项目、节点类型、审批状态、软删除、标签、时间范围编译为 SQL/Cypher WHERE。

对齐 PDD 3.3"支持过滤条件：项目、角色权限、知识状态（仅 approved）、标签、时间范围"。
FilterSpec 作为三引擎统一的过滤契约，编译为各引擎对应的 WHERE 子句，保证过滤条件一致。
图遍历层（AGE）只过滤 project_id（图节点不存 status/is_deleted/tags/created_at 字段），
完整过滤在 PG 表关联阶段完成。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.sql.elements import ColumnElement

from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.schema import NODE_TYPES


@dataclass(frozen=True)
class FilterSpec:
    """三引擎统一过滤条件。

    默认值对齐 PDD：
    - status="approved"：未审批内容不参与检索（PDD 3.4）
    - exclude_deleted=True：软删除节点不可见
    - project_id：项目隔离（RLS 已强制，此处冗余校验保证一致性）
    """

    project_id: uuid.UUID | None = None
    node_types: tuple[str, ...] | None = None
    status: str = "approved"
    exclude_deleted: bool = True
    tags: tuple[str, ...] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    def __post_init__(self) -> None:
        """校验 node_types 必须在 NODE_TYPES 白名单内，非法类型立即抛错。"""
        if self.node_types is not None:
            invalid = set(self.node_types) - NODE_TYPES
            if invalid:
                raise ValueError(
                    f"非法节点类型: {sorted(invalid)}，合法类型: {sorted(NODE_TYPES)}"
                )


def compile_sqlalchemy(spec: FilterSpec | None) -> list[ColumnElement[bool]]:
    """编译 FilterSpec 为 SQLAlchemy WHERE 子句列表。

    供 VectorSearcher/FullTextSearcher 的 select().where(*clauses) 使用。
    spec=None 时返回空列表（不过滤），由调用方决定是否强制要求 project_id。
    """
    if spec is None:
        return []

    clauses: list[ColumnElement[bool]] = []

    if spec.project_id is not None:
        clauses.append(KnowledgeNode.project_id == spec.project_id)

    if spec.node_types:
        clauses.append(KnowledgeNode.type.in_(spec.node_types))

    # status 默认 "approved"，显式传入空字符串表示不过滤状态
    if spec.status:
        clauses.append(KnowledgeNode.status == spec.status)

    if spec.exclude_deleted:
        clauses.append(KnowledgeNode.is_deleted.is_(False))

    if spec.tags:
        # JSONB 数组 @> 操作符：tags 列包含 spec.tags 的所有元素即为匹配
        # PostgreSQL JSONB 数组语义：'["auth", "P0"]'::jsonb @> '["auth"]'::jsonb 为 True
        clauses.append(KnowledgeNode.tags.contains(list(spec.tags)))

    if spec.created_after is not None:
        clauses.append(KnowledgeNode.created_at >= spec.created_after)

    if spec.created_before is not None:
        clauses.append(KnowledgeNode.created_at <= spec.created_before)

    return clauses


def compile_cypher(spec: FilterSpec | None, node_var: str = "n") -> str:
    """编译 FilterSpec 为 Cypher WHERE 子句字符串。

    供 GraphSearcher 在 AGE 图遍历时使用。AGE 图节点只存 id/project_id/title，
    不存 status/is_deleted/tags/created_at 字段，因此这些条件在图层无法过滤，
    由 GraphSearcher 在 PG 表关联阶段通过 compile_sqlalchemy 过滤。

    返回空字符串表示无需图层过滤。调用方拼入 MATCH ... WHERE <子句> ...，
    或在无子句时省略 WHERE。
    """
    if spec is None or spec.project_id is None:
        return ""

    # 图层只过滤 project_id（图节点属性含 project_id 用于隔离）
    return f"{node_var}.project_id = $project_id"
