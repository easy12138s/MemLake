"""ReindexTask ORM 模型：异步向量重嵌任务的状态表。

reindex_project_vectors 改为异步提交后，后台 worker 跨 worker 进程执行，
任务状态必须落库（部署为 uvicorn --workers 4，内存状态不互通）。
表由 db/init.py 的 create_tables 在启动时通过 import 本模块注册到 Base.metadata。
"""

import uuid
from datetime import datetime

from sqlalchemy import UUID, DateTime, Integer, String, Text, func
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
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), index=True, comment="归属项目"
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
