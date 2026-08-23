"""ReindexTask ORM 模型：异步向量重嵌任务的状态表。

reindex_project_vectors 改为异步提交后，任务状态必须落库：当前单进程部署下
保证跨重启一致；未来多 worker 部署时内存状态亦不互通，落库是唯一可靠途径。
表由 db/init.py 的 create_tables 在启动时通过 import 本模块注册到 Base.metadata。
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, UUID, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from mem_lake.db.base import Base

# 任务状态机：pending → running → done / failed
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"


class ReindexTask(Base):
    """向量重嵌后台任务记录。"""

    __tablename__ = "reindex_task"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
        nullable=True,
        comment="归属项目（悬浮 system 需求节点嵌入任务可为空）",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=TASK_STATUS_PENDING,
        comment="pending/running/done/failed",
    )
    total: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="待重嵌节点总数（运行前统计）"
    )
    processed: Mapped[int] = mapped_column(
        Integer, default=0, comment="已处理节点数"
    )
    reindexed: Mapped[int] = mapped_column(
        Integer, default=0, comment="成功重嵌节点数"
    )
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败原因（status=failed 时填充）"
    )
    target_node_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
        comment="指定节点范围（审批异步嵌入场景）；为空=整库重嵌",
    )
    created_by: Mapped[str] = mapped_column(String(64), comment="触发者 Access Key ID")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始执行时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成/失败时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
