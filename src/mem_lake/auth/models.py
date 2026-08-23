"""ORM 模型：access_key 表 + bcrypt 辅助函数 + Access Key 明文格式定义。

对齐 PDD 4.5 access_key 表 schema。Access Key 明文格式为 ak_{id_hex}.{secret}，
在明文中嵌入 row id（UUID hex），认证时从明文解析 id 查找行再 bcrypt 校验。
此方案适配 PDD 4.5 现有 schema（仅 id + key_hash），无需新增 key_prefix 列。
"""

import secrets
import uuid
from datetime import datetime

import bcrypt
from sqlalchemy import Boolean, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mem_lake.config import get_settings
from mem_lake.db.base import Base

# Access Key 明文格式常量
ACCESS_KEY_PREFIX = "ak_"
KEY_FORMAT = "ak_{id_hex}.{secret}"


class AccessKey(Base):
    """访问密钥记录，仅存 bcrypt hash，不存明文。"""

    __tablename__ = "access_key"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    key_hash: Mapped[str] = mapped_column(comment="Access Key 哈希（bcrypt，不存明文）")
    role: Mapped[str] = mapped_column(
        String(16), comment="业务角色: admin/pm/dev"
    )
    project_scope: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{\"systems\":[],\"projects\":[]}'::jsonb"),
        comment="访问范围（两级）：{systems:[...], projects:[...]}；PM 需求按 system 隔离，资产按 project 隔离",
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default=text("'active'"),
        comment="状态: active/revoked",
    )
    lax_mode: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        comment="审核模式: false=严格(需审批) true=宽松(免审批直接入库)",
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), comment="创建时间"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="吊销时间"
    )

    __table_args__ = (
        Index("idx_access_key_status", "status"),
    )


def generate_secret() -> str:
    """生成 Access Key 随机密钥片段（secret 部分）。

    用于新建与轮换：轮换时保留 row id、仅替换 secret 并重算 key_hash。
    """
    return secrets.token_urlsafe(24)[:32]


def build_plaintext(key_id: uuid.UUID, secret: str) -> str:
    """由 row id 与 secret 拼装 Access Key 明文（ak_{id_hex}.{secret}）。"""
    return f"{ACCESS_KEY_PREFIX}{key_id.hex}.{secret}"


def generate_access_key() -> tuple[uuid.UUID, str]:
    """生成 Access Key。

    返回 (key_id, plaintext)，明文格式为 ak_{id_hex}.{secret}。
    调用方存 id + hash_access_key(plaintext)，明文仅返回一次。
    """
    key_id = uuid.uuid4()
    secret = generate_secret()
    plaintext = build_plaintext(key_id, secret)
    return key_id, plaintext


def hash_access_key(plain: str) -> str:
    """bcrypt 哈希 Access Key 明文，rounds 从 config.BCRYPT_ROUNDS 读取。"""
    settings = get_settings()
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode(), salt).decode()


def verify_access_key(plain: str, key_hash: str) -> bool:
    """bcrypt 校验 Access Key 明文与哈希是否匹配。"""
    return bcrypt.checkpw(plain.encode(), key_hash.encode())


def parse_key_id(plain: str) -> uuid.UUID | None:
    """从 Access Key 明文解析 row id。

    明文格式 ak_{id_hex}.{secret}，解析 id_hex 转 UUID。
    格式不符返回 None（供 M6 认证查找）。
    """
    if not plain.startswith(ACCESS_KEY_PREFIX):
        return None
    rest = plain[len(ACCESS_KEY_PREFIX):]
    if "." not in rest:
        return None
    id_hex = rest.split(".", 1)[0]
    try:
        return uuid.UUID(id_hex)
    except ValueError:
        return None
