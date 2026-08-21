"""SQLAlchemy ORM 模型：knowledge_node 表。

对齐 PDD 4.2 节点主表 Schema。节点统一存储于 knowledge_node 表，通过 type 字段区分实体类型，
properties JSONB 存储类型特有属性。content_vector 支撑向量检索，content_tsv 支撑全文检索。
HNSW 向量索引通过 pgvector-python 官方方案放入 __table_args__，随 create_all 创建。
content_tsv 使用 PostgreSQL 内置 TSVECTOR 类型（GIN 索引默认 tsvector_ops opclass）。
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mem_lake.db.base import Base


class KnowledgeNode(Base):
    """知识节点，统一存储各类实体（ProjectProfile/Requirement/CodeSnippet 等）。"""

    __tablename__ = "knowledge_node"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), comment="所属项目"
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
        Index("idx_node_project_tags", "tags", postgresql_using="gin"),
        Index("idx_node_tsv", "content_tsv", postgresql_using="gin"),
        # HNSW 向量索引（pgvector-python 官方方案，随 create_all 创建）
        Index(
            "idx_node_vector",
            "content_vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"content_vector": "vector_cosine_ops"},
        ),
    )
