"""SQLAlchemy ORM 模型：approval_batch + approval_item 表。

对齐 PDD 4.5 审批批次表。审批数据需持久化：批次待审、追溯历史、统计指标。
状态机：pending_review → approved/rejected，approved/rejected 为终态不可逆转换。
payload JSONB 暂存完整内容，审批通过后写入正式存储（knowledge_node 表与 AGE 图）。
target_id 在审批通过后回填实际节点 ID（边无 target_id，留空）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from mem_lake.db.base import Base


class ApprovalBatch(Base):
    """审批批次表：一次写工具调用产生一个批次。

    对齐 PDD 4.5 approval_batch 表。状态机：pending_review → approved/rejected。
    状态转换不可逆（approved/rejected 为终态）。
    幂等键：(submitted_by, batch_type, operation_id) 联合唯一，operation_id=NULL 时不参与。
    """

    __tablename__ = "approval_batch"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="归属项目")
    batch_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="批次类型: publish_requirement/submit_dev_artifacts/update_requirement_relations",
    )
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False, comment="提交者 Access Key ID")
    submitter_role: Mapped[str] = mapped_column(String(16), nullable=False, comment="提交者角色: pm/dev")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"), comment="自动生成的提交摘要")
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending_review",
        server_default=text("'pending_review'"),
        comment="状态: pending_review/approved/rejected",
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="提交时间"
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="审核人 Access Key ID")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="审核时间")
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="审核意见（拒绝时填写）")
    conflict_hint: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="冲突检测提示（审批通过时生成）")
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="幂等操作标识")

    items: Mapped[list["ApprovalItem"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="ApprovalItem.seq",
    )

    __table_args__ = (
        # PDD 4.5 索引：(project_id, status, submitted_at)
        Index("idx_approval_batch_status", "project_id", "status", "submitted_at"),
        # 幂等键联合唯一约束：(submitted_by, batch_type, operation_id)
        # PostgreSQL NULL 语义：operation_id=NULL 不参与唯一性比较
        UniqueConstraint(
            "submitted_by",
            "batch_type",
            "operation_id",
            name="uq_approval_batch_idempotency",
        ),
    )


class ApprovalItem(Base):
    """审批项表：批次内具体变更内容。

    对齐 PDD 4.5 approval_item 表。payload 存储完整内容，审批通过后写入正式存储。
    target_id 在审批通过后回填实际节点 ID（边无 target_id，留空）。

    payload 结构契约（审批通过时 create_node/add_edge 的入参来源）：
    - node + create: {"project_id": "uuid", "node_type": "Requirement", "title": "...",
                      "content": "...", "properties": {...}, "tags": [...], "source": {...},
                      "created_by": "ak"}
    - node + update: {"node_id": "uuid", "title": "...", "content": "...",
                      "properties": {...}, "tags": [...]}
    - edge + create: {"from_id": "uuid", "to_id": "uuid", "edge_type": "implements",
                      "properties": {...}}
    """

    __tablename__ = "approval_item"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_batch.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联的审批批次 ID",
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="批次内序号，从 1 开始")
    item_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="item 类型: node/edge")
    action: Mapped[str] = mapped_column(String(16), nullable=False, comment="操作: create/update/delete")
    entity_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="节点类型（Requirement/CodeSnippet/...）或关系类型（implements/conflicts_with/...）",
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整内容（审批通过后写入正式存储）")
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="审批通过后写入的实际节点 ID（回填）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )

    batch: Mapped["ApprovalBatch"] = relationship(back_populates="items")

    __table_args__ = (
        # PDD 4.5 索引：(batch_id, seq)
        Index("idx_approval_item_batch", "batch_id", "seq"),
    )
