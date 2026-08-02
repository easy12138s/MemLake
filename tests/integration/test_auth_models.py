"""M2 集成测试：access_key 表 CRUD + bcrypt 辅助函数。

事务回滚隔离，验证 AccessKey ORM 与 hash/verify/generate/parse 辅助函数。
"""

from datetime import datetime, timezone

from sqlalchemy import select

from mem_lake.auth.models import (
    AccessKey,
    generate_access_key,
    hash_access_key,
    parse_key_id,
    verify_access_key,
)


def test_hash_and_verify_access_key():
    """hash 后 verify 成功，错误明文 verify 失败。"""
    plain = "ak_test_secret_key_123"
    h = hash_access_key(plain)
    assert h != plain
    assert verify_access_key(plain, h) is True
    assert verify_access_key("wrong_key", h) is False


def test_generate_access_key_format():
    """返回 (key_id, plaintext)，plaintext 格式正确，parse_key_id 可还原。"""
    key_id, plaintext = generate_access_key()
    assert plaintext.startswith("ak_")
    assert "." in plaintext
    assert parse_key_id(plaintext) == key_id


def test_parse_key_id_invalid():
    """格式不符返回 None。"""
    assert parse_key_id("invalid") is None
    assert parse_key_id("ak_no_dot") is None
    assert parse_key_id("ak_notuuid123.secret") is None


async def test_access_key_crud(db_session):
    """插入 AccessKey 行，按 id 查询，字段正确。"""
    key_id, plaintext = generate_access_key()
    key_hash = hash_access_key(plaintext)
    ak = AccessKey(
        id=key_id,
        key_hash=key_hash,
        role="pm",
        project_scope=["proj-001"],
    )
    db_session.add(ak)
    await db_session.flush()

    result = await db_session.execute(select(AccessKey).where(AccessKey.id == key_id))
    fetched = result.scalar_one()
    assert fetched.role == "pm"
    assert fetched.status == "active"
    assert fetched.project_scope == ["proj-001"]
    assert fetched.revoked_at is None
    # 验证明文与存储的 hash 匹配
    assert verify_access_key(plaintext, fetched.key_hash) is True


async def test_access_key_status_field(db_session):
    """插入 active，更新 status=revoked + revoked_at（验证字段可写，实际 revoke 业务在 M6）。"""
    key_id, plaintext = generate_access_key()
    ak = AccessKey(id=key_id, key_hash=hash_access_key(plaintext), role="dev")
    db_session.add(ak)
    await db_session.flush()

    ak.status = "revoked"
    ak.revoked_at = datetime.now(timezone.utc)
    await db_session.flush()

    result = await db_session.execute(select(AccessKey).where(AccessKey.id == key_id))
    fetched = result.scalar_one()
    assert fetched.status == "revoked"
    assert fetched.revoked_at is not None


async def test_role_values(db_session):
    """插入 admin/pm/dev 三种 role 行，查询验证。"""
    for role in ("admin", "pm", "dev"):
        key_id, plaintext = generate_access_key()
        db_session.add(
            AccessKey(id=key_id, key_hash=hash_access_key(plaintext), role=role)
        )
    await db_session.flush()

    result = await db_session.execute(select(AccessKey))
    rows = result.scalars().all()
    roles = {r.role for r in rows}
    assert roles == {"admin", "pm", "dev"}
