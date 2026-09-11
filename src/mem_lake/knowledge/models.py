"""SQLAlchemy ORM 模型：knowledge_node 表。

对齐 PDD 4.2 节点主表 Schema。节点统一存储于 knowledge_node 表，通过 type 字段区分实体类型，
properties JSONB 存储类型特有属性。向量检索走 node_embedding 表的 facet 多向量（FIX-08 后
knowledge_node.content_vector 列废弃），content_tsv 支撑全文检索。
node_embedding 的 HNSW 向量索引通过 pgvector-python 官方方案放入 __table_args__，随 create_all 创建
（opclass 为 vector_ip_ops，配 1024 维归一化向量；参数 m=32、ef_construction=400）。
content_tsv 使用 PostgreSQL 内置 TSVECTOR 类型（GIN 索引默认 tsvector_ops opclass）。
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, PrimaryKeyConstraint, String, Text, UniqueConstraint, text
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
    code: Mapped[str | None] = mapped_column(
        String(32),
        unique=True,
        nullable=True,
        comment="系统前缀编码（用于需求主键前缀，如 HIS）；缺省由 name 派生或回退 SYS",
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
    requirement_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="需求主键（服务端分配，如 HIS-0001），仅 Requirement 使用",
    )
    type: Mapped[str] = mapped_column(
        String(32),
        comment="节点类型: ProjectProfile/Requirement/CodeSnippet/Solution/DesignIntent/Decision/Pitfall",
    )
    title: Mapped[str] = mapped_column(Text, comment="节点标题")
    content: Mapped[str] = mapped_column(Text, comment="节点正文内容")
    # FIX-08：content_vector 列废弃（检索主路径走 node_embedding 多向量），
    # 由 0003_drop_content_vector 迁移 DROP。此处不再定义。
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR(), nullable=True, comment="全文检索向量（触发器自动维护）"
    )
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="类型特有属性（schema 规范见 PDD 4.4）",
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="标签数组",
    )
    source: Mapped[dict[str, Any]] = mapped_column(
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
        Index("idx_node_requirement_key", "requirement_key"),
        # 需求主键按 system 唯一（仅对非空 requirement_key 生效；其它类型该列为 NULL 不参与）
        UniqueConstraint(
            "system_id",
            "requirement_key",
            name="uq_node_system_requirement_key",
        ),
        # FIX-08：knowledge_node 的 HNSW 向量索引 idx_node_vector 随 content_vector
        # 列一并废弃（0003_drop_content_vector 迁移 DROP）；向量检索索引在
        # node_embedding 表（idx_node_embedding_vector，见 NodeEmbedding）。
    )


class NodeEmbedding(Base):
    """节点多向量 facet 表（32k 适配 D：按字段独立 embed，检索 max-pooling）。

    每个 knowledge_node 在此表有 1~N 行：facet="content"（title+content）或某关键属性键
    （如 root_cause/solution），content_vector 为该 facet 的 1024 维归一化向量。
    检索时按 node_id 聚合取各 facet 与查询向量的最大余弦（maxsim），等价 ColBERT 式
    多向量召回，避免单向量语义稀释。

    HNSW 索引随 create_all 创建；节点通过 reindex_project_vectors 后台任务重算 facets。
    """

    __tablename__ = "node_embedding"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="所属知识节点",
    )
    facet: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="facet 名: content / 属性键（root_cause 等）",
    )
    content_vector: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True, comment="facet 向量（Qwen3-Embedding-0.6B，1024 维）"
    )

    __table_args__ = (
        Index("idx_node_embedding_node", "node_id"),
        Index("idx_node_embedding_node_facet", "node_id", "facet", unique=True),
        # HNSW 向量索引（参数/opclass 与 FIX-04 统一，m=32/ef_construction=400/vector_ip_ops）
        Index(
            "idx_node_embedding_vector",
            "content_vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 32, "ef_construction": 400},
            postgresql_ops={"content_vector": "vector_ip_ops"},
        ),
    )


class RequirementCounter(Base):
    """每个 system 域的需求序号计数器（用于分配可读需求主键 HIS-0001）。"""

    __tablename__ = "requirement_counter"

    system_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, comment="system 域 ID"
    )
    last_value: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="该 system 下需求序号当前值"
    )


class EmbeddingState(Base):
    """embedding 模型/provider 变更记录（每检测到一次切换插入一行，留历史审计）。

    签名格式 "provider:model"（如 "local:/models/Qwen3-Embedding-0.6B" /
    "remote:text-embedding-3-small"）。应用启动时比对最近一条 signature 与当前
    embedding 服务 /health 的 provider+model：不同则告警提醒重嵌或回退（见
    embedding/consistency.py）。
    """

    __tablename__ = "embedding_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    signature: Mapped[str] = mapped_column(
        String(256), comment="embedding 签名，格式 provider:model"
    )
    detected_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), comment="检测到该签名的时间"
    )

    __table_args__ = (
        Index("idx_embedding_state_detected_at", "detected_at"),
    )
