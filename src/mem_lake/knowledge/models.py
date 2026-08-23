"""SQLAlchemy ORM 模型：knowledge_node 表。

对齐 PDD 4.2 节点主表 Schema。节点统一存储于 knowledge_node 表，通过 type 字段区分实体类型，
properties JSONB 存储类型特有属性。content_vector 支撑向量检索，content_tsv 支撑全文检索。
HNSW 向量索引通过 pgvector-python 官方方案放入 __table_args__，随 create_all 创建
（opclass 为 vector_ip_ops，配 1024 维归一化向量；参数 m=32、ef_construction=400）。
content_tsv 使用 PostgreSQL 内置 TSVECTOR 类型（GIN 索引默认 tsvector_ops opclass）。
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, PrimaryKeyConstraint, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mem_lake.db.base import Base


class System(Base):
    """系统域：PM 需求的跨项目隔离单位。

    Requirement 归属 system（system_id 必填），project 可空（悬浮需求）；
    资产（code/solution 等）仍按 project 隔离。system↔project 归属见 SystemProject。
    """

    __tablename__ = "system"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(
        String(128), unique=True, comment="系统域名（唯一）"
    )
    description: Mapped[str] = mapped_column(
        Text, default="", comment="系统域描述"
    )


class SystemProject(Base):
    """system ↔ project 归属（反查 project 属于哪些 system）。

    dev 可见判定（system 含 dev 任一 project）与影响评估聚合需按 project 反查 system，
    单靠 knowledge_node.system_id 列无法在 project 建库前回答，故建此映射表。
    """

    __tablename__ = "system_project"

    system_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), comment="system 域 ID"
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), comment="project ID"
    )

    __table_args__ = (
        PrimaryKeyConstraint("system_id", "project_id"),
        Index("idx_system_project_project", "project_id"),
    )


class KnowledgeNode(Base):
    """知识节点，统一存储各类实体（ProjectProfile/Requirement/CodeSnippet 等）。"""

    __tablename__ = "knowledge_node"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="所属项目（悬浮需求可为空）"
    )
    system_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="所属 system 域（仅 Requirement 使用）",
    )
    type: Mapped[str] = mapped_column(
        String(32),
        comment="节点类型: ProjectProfile/Requirement/CodeSnippet/Solution/DesignIntent/Decision/Pitfall",
    )
    title: Mapped[str] = mapped_column(Text, comment="节点标题")
    content: Mapped[str] = mapped_column(Text, comment="节点正文内容")
    content_vector: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True, comment="内容向量（Qwen3-Embedding-0.6B，1024 维）"
    )
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR(), nullable=True, comment="全文检索向量（触发器自动维护）"
    )
    properties: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="类型特有属性（schema 规范见 PDD 4.4）",
    )
    tags: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="标签数组",
    )
    source: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="来源信息（Agent、工具、原始文档引用）",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="approved",
        server_default=text("'approved'"),
        comment="节点状态: approved/archived",
    )
    version: Mapped[int] = mapped_column(
        default=1, server_default=text("1"), comment="版本号，从 1 开始递增"
    )
    created_by: Mapped[str] = mapped_column(Text, comment="提交者 Access Key 标识")
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), comment="创建时间"
    )
    is_deleted: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), comment="软删除标记"
    )

    __table_args__ = (
        Index("idx_node_project_type_status", "project_id", "type", "status"),
        Index("idx_node_system_type_status", "system_id", "type", "status"),
        Index("idx_node_project_tags", "tags", postgresql_using="gin"),
        Index("idx_node_tsv", "content_tsv", postgresql_using="gin"),
        # HNSW 向量索引（pgvector-python 官方方案，随 create_all 创建）
        # 适配 1024 维高维向量：m=32、ef_construction=400（业界建议 m≈32-48、ef_construction≈m*10-20）。
        # opclass 用 vector_ip_ops（内积）：向量均来自归一化 embedding 服务，内积 <#> 与余弦等价且更快，
        # 与搜索层 search/vector.py 的 max_inner_product 调用对齐。存量库需重建索引，见 deploy/init/002_*.
        Index(
            "idx_node_vector",
            "content_vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 32, "ef_construction": 400},
            postgresql_ops={"content_vector": "vector_ip_ops"},
        ),
    )
