"""ORM 模型：audit_log 表。

对齐 PDD 4.5 audit_log 表 schema。append-only 语义：无 updated_at 字段，
service 层仅暴露 INSERT 与 SELECT 路径（见 audit/service.py）。
"""

import uuid
from datetime import datetime

from sqlalchemy import Index, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mem_lake.db.base import Base


class AuditLog(Base):
    """审计日志记录，append-only，禁止 UPDATE/DELETE。"""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    actor: Mapped[str] = mapped_column(comment="操作人 Access Key 标识")
    action: Mapped[str] = mapped_column(
        comment="操作类型: write/update/approve/reject/archive"
    )
    target_type: Mapped[str] = mapped_column(
        comment="目标类型: node/edge/access_key"
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="目标 ID，部分操作无具体目标"
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="归属项目（节点/边写操作维度，admin/access_key 等跨项目操作留空）",
    )
    operation_id: Mapped[str | None] = mapped_column(
        nullable=True, comment="幂等操作标识（可选）"
    )
    detail: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="操作详情",
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), comment="操作时间"
    )

    __table_args__ = (
        Index("idx_audit_actor_created", "actor", "created_at"),
        Index("idx_audit_target", "target_type", "target_id"),
        Index("idx_audit_project", "project_id"),
    )
