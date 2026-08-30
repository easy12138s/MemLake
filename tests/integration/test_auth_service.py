"""M6 集成测试：auth/service.py Access Key 服务层端到端。

真实 PG + 事务回滚隔离。覆盖：
- create_access_key：生成明文 + bcrypt hash + 写表 + 审计日志
- authenticate_access_key：完整认证流程（parse → query → status → bcrypt verify）
- revoke_access_key：吊销 + 幂等 + 不存在抛错
- list_access_keys：角色/状态过滤 + key_hash 排除
- 边界：非法角色、错误明文、已吊销 key 认证、格式错误明文

测试事务回滚隔离，不污染 DB。
"""

import uuid

import pytest

from mem_lake.auth.service import (
    AccessKeyNotFoundError,
    AccessKeyRevokedError,
    authenticate_access_key,
    create_access_key,
    get_access_key_by_id,
    list_access_keys,
    revoke_access_key,
    rotate_access_key,
    update_access_key_scope,
    update_access_key_systems,
)


def _scope(*projects: uuid.UUID) -> dict:
    """create_access_key 的两级 project_scope 入参构造（{systems,projects} 字典）。"""
    return {"systems": [], "projects": [str(p) for p in projects]}

# ============================================================================
# create_access_key 端到端
# ============================================================================


class TestCreateAccessKey:
    """create_access_key 集成测试。"""

    async def test_create_returns_key_id_and_plaintext(self, db_session):
        """create 返回 (key_id, plaintext)，plaintext 格式正确。"""
        key_id, plaintext = await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4()), created_by="admin_ak"
        )
        assert isinstance(key_id, uuid.UUID)
        assert plaintext.startswith("ak_")
        assert "." in plaintext

    async def test_create_persists_to_db(self, db_session):
        """create 后 get_access_key_by_id 可查到，字段正确。"""
        project_id = uuid.uuid4()
        key_id, plaintext = await create_access_key(
            db_session, role="dev", project_scope=_scope(project_id), created_by="admin_ak"
        )
        access_key = await get_access_key_by_id(db_session, key_id)
        assert access_key is not None
        assert access_key.id == key_id
        assert access_key.role == "dev"
        assert access_key.status == "active"
        assert str(project_id) in access_key.project_scope["projects"]
        assert access_key.key_hash != plaintext  # 存储的是 hash 不是明文
        assert access_key.revoked_at is None

    async def test_create_admin_empty_project_scope(self, db_session):
        """admin 角色允许空 project_scope（不受限）。"""
        key_id, plaintext = await create_access_key(
            db_session, role="admin", project_scope=_scope(), created_by="system"
        )
        access_key = await get_access_key_by_id(db_session, key_id)
        assert access_key.role == "admin"
        assert access_key.project_scope == {"systems": [], "projects": []}

    async def test_create_invalid_role_raises(self, db_session):
        """非法角色抛 ValueError。"""
        with pytest.raises(ValueError, match="非法角色"):
            await create_access_key(
                db_session, role="guest", project_scope=_scope(uuid.uuid4())
            )


# ============================================================================
# authenticate_access_key 端到端
# ============================================================================


class TestAuthenticateAccessKey:
    """authenticate_access_key 集成测试。"""

    async def test_authenticate_success(self, db_session):
        """create 后用明文 authenticate 成功，返回正确字段。"""
        project_id = uuid.uuid4()
        key_id, plaintext = await create_access_key(
            db_session, role="pm", project_scope=_scope(project_id), created_by="admin_ak"
        )
        result = await authenticate_access_key(db_session, plaintext)
        assert result is not None
        assert result["key_id"] == key_id
        assert result["role"] == "pm"
        assert str(project_id) in result["project_scope"]

    async def test_authenticate_wrong_plaintext_returns_none(self, db_session):
        """错误明文返回 None（bcrypt 校验失败）。"""
        _, plaintext = await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )
        # 篡改 secret 部分
        wrong = plaintext.rsplit(".", 1)[0] + ".wrong_secret"
        result = await authenticate_access_key(db_session, wrong)
        assert result is None

    async def test_authenticate_revoked_returns_none(self, db_session):
        """已吊销的 key 认证返回 None。"""
        key_id, plaintext = await create_access_key(
            db_session, role="dev", project_scope=_scope(uuid.uuid4())
        )
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")
        result = await authenticate_access_key(db_session, plaintext)
        assert result is None

    async def test_authenticate_invalid_format_returns_none(self, db_session):
        """格式错误的明文返回 None（parse_key_id 失败）。"""
        result = await authenticate_access_key(db_session, "invalid_key")
        assert result is None

    async def test_authenticate_nonexistent_id_returns_none(self, db_session):
        """不存在的 key_id 返回 None。"""
        # 构造一个格式正确但 id 不存在的明文
        fake_id = uuid.uuid4()
        fake_plaintext = f"ak_{fake_id.hex}.fakesecret"
        result = await authenticate_access_key(db_session, fake_plaintext)
        assert result is None


# ============================================================================
# revoke_access_key 端到端
# ============================================================================


class TestRevokeAccessKey:
    """revoke_access_key 集成测试。"""

    async def test_revoke_sets_status_and_timestamp(self, db_session):
        """revoke 后 status=revoked + revoked_at 非 None。"""
        key_id, _ = await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")
        access_key = await get_access_key_by_id(db_session, key_id)
        assert access_key.status == "revoked"
        assert access_key.revoked_at is not None

    async def test_revoke_idempotent(self, db_session):
        """重复 revoke 不报错（幂等）。"""
        key_id, _ = await create_access_key(
            db_session, role="dev", project_scope=_scope(uuid.uuid4())
        )
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")
        # 第二次 revoke 不抛错
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")
        access_key = await get_access_key_by_id(db_session, key_id)
        assert access_key.status == "revoked"

    async def test_revoke_nonexistent_raises(self, db_session):
        """revoke 不存在的 key 抛 AccessKeyNotFoundError。"""
        fake_id = uuid.uuid4()
        with pytest.raises(AccessKeyNotFoundError):
            await revoke_access_key(db_session, key_id=fake_id, actor="admin_ak")


# ============================================================================
# list_access_keys 端到端
# ============================================================================


class TestListAccessKeys:
    """list_access_keys 集成测试。"""

    async def test_list_all(self, db_session):
        """列出所有 key（不过滤）。"""
        await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )
        await create_access_key(
            db_session, role="dev", project_scope=_scope(uuid.uuid4())
        )
        keys = await list_access_keys(db_session)
        assert len(keys) >= 2

    async def test_list_filter_by_role(self, db_session):
        """按角色过滤。"""
        await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )
        await create_access_key(
            db_session, role="dev", project_scope=_scope(uuid.uuid4())
        )
        pm_keys = await list_access_keys(db_session, role="pm")
        assert len(pm_keys) >= 1
        assert all(k.role == "pm" for k in pm_keys)

    async def test_list_filter_by_status(self, db_session):
        """按状态过滤。"""
        key_id, _ = await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")
        # 再创建一个 active 的
        await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )

        active_keys = await list_access_keys(db_session, status="active")
        revoked_keys = await list_access_keys(db_session, status="revoked")
        assert all(k.status == "active" for k in active_keys)
        assert all(k.status == "revoked" for k in revoked_keys)
        assert len(revoked_keys) >= 1

    async def test_list_excludes_key_hash(self, db_session):
        """list 返回的 AccessKey 对象 key_hash 被 defer，访问触发懒加载抛错（避免泄漏）。

        defer 排除 key_hash 列后，async session 上下文外访问该属性抛 MissingGreenlet
        （async session 不支持同步懒加载），实现防泄漏。
        """
        from sqlalchemy.exc import MissingGreenlet

        await create_access_key(
            db_session, role="pm", project_scope=_scope(uuid.uuid4())
        )
        keys = await list_access_keys(db_session)
        assert len(keys) >= 1
        # defer 后访问 key_hash 触发懒加载，async 上下文抛 MissingGreenlet
        for k in keys:
            with pytest.raises(MissingGreenlet):
                _ = k.key_hash


# ============================================================================
# 端到端完整流程
# ============================================================================


class TestAccessKeyLifecycle:
    """Access Key 完整生命周期：create → authenticate → revoke → authenticate 失败。"""

    async def test_full_lifecycle(self, db_session):
        """create 后可认证，revoke 后认证失败。"""
        project_id = uuid.uuid4()

        # 1. create
        key_id, plaintext = await create_access_key(
            db_session,
            role="pm",
            project_scope=_scope(project_id),
            created_by="admin_ak",
        )

        # 2. authenticate 成功
        result = await authenticate_access_key(db_session, plaintext)
        assert result is not None
        assert result["role"] == "pm"

        # 3. revoke
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")

        # 4. authenticate 失败（已吊销）
        result = await authenticate_access_key(db_session, plaintext)
        assert result is None

    async def test_admin_full_lifecycle_empty_scope(self, db_session):
        """admin 角色 + 空 project_scope 完整流程。"""
        key_id, plaintext = await create_access_key(
            db_session, role="admin", project_scope=_scope(), created_by="system"
        )

        result = await authenticate_access_key(db_session, plaintext)
        assert result is not None
        assert result["role"] == "admin"
        assert result["project_scope"] == []

        await revoke_access_key(db_session, key_id=key_id, actor="system")
        result = await authenticate_access_key(db_session, plaintext)
        assert result is None


# ============================================================================
# rotate_access_key 端到端
# ============================================================================


class TestRotateAccessKey:
    """rotate_access_key 集成测试。"""

    async def test_rotate_returns_new_plaintext_and_invalidates_old(self, db_session):
        """轮换后返回新明文，旧明文认证失败、新明文认证成功。"""
        key_id, old_plaintext = await create_access_key(
            db_session, role="dev", project_scope=_scope(uuid.uuid4()), created_by="admin_ak"
        )
        # 旧明文先认证通过
        assert (await authenticate_access_key(db_session, old_plaintext)) is not None

        ak, new_plaintext = await rotate_access_key(db_session, key_id=key_id, actor="admin_ak")
        assert ak.id == key_id
        assert new_plaintext.startswith("ak_")
        assert new_plaintext != old_plaintext
        # 解析出的 row id 不变（保留原 id）
        from mem_lake.auth.models import parse_key_id

        assert parse_key_id(new_plaintext) == key_id

        # 旧明文立即失效，新明文可用
        assert (await authenticate_access_key(db_session, old_plaintext)) is None
        new_auth = await authenticate_access_key(db_session, new_plaintext)
        assert new_auth is not None
        assert new_auth["role"] == "dev"

    async def test_rotate_revoked_raises(self, db_session):
        """已吊销的 Key 不可轮换。"""
        key_id, _ = await create_access_key(
            db_session, role="dev", project_scope=_scope(), created_by="admin_ak"
        )
        await revoke_access_key(db_session, key_id=key_id, actor="admin_ak")
        with pytest.raises(AccessKeyRevokedError):
            await rotate_access_key(db_session, key_id=key_id, actor="admin_ak")

    async def test_rotate_nonexistent_raises(self, db_session):
        """不存在的 Key 轮换抛 AccessKeyNotFoundError。"""
        with pytest.raises(AccessKeyNotFoundError):
            await rotate_access_key(db_session, key_id=uuid.uuid4(), actor="admin_ak")


# ============================================================================
# update_access_key_scope 端到端
# ============================================================================


class TestUpdateAccessKeyScope:
    """update_access_key_scope 集成测试。"""

    async def test_update_explicit_key_ids(self, db_session):
        """显式指定 key_ids 仅更新这些 Key 的 project_scope。"""
        pid_a = uuid.uuid4()
        pid_b = uuid.uuid4()
        k1, _ = await create_access_key(db_session, role="dev", project_scope=_scope(), created_by="admin_ak")
        k2, _ = await create_access_key(db_session, role="dev", project_scope=_scope(), created_by="admin_ak")

        updated = await update_access_key_scope(
            db_session,
            project_scope=[pid_a, pid_b],
            key_ids=[k1],
            actor="admin_ak",
        )
        assert len(updated) == 1
        assert updated[0].id == k1
        assert set(updated[0].project_scope["projects"]) == {str(pid_a), str(pid_b)}

        # k2 不受影响
        k2_db = await get_access_key_by_id(db_session, k2)
        assert k2_db.project_scope == {"systems": [], "projects": []}

    async def test_update_by_role_filter(self, db_session):
        """role_filter 批量更新该角色全部 Key（含既有行，断言覆盖本次创建的 Key）。"""
        pid = uuid.uuid4()
        d1, _ = await create_access_key(db_session, role="dev", project_scope=_scope(), created_by="admin_ak")
        d2, _ = await create_access_key(db_session, role="dev", project_scope=_scope(), created_by="admin_ak")
        pm_key, _ = await create_access_key(db_session, role="pm", project_scope=_scope(), created_by="admin_ak")

        updated = await update_access_key_scope(
            db_session, project_scope=[pid], role_filter="dev", actor="admin_ak"
        )
        updated_ids = {u.id for u in updated}
        # 本次创建的 dev Key 必须被更新，pm Key 必须不被更新
        assert {d1, d2}.issubset(updated_ids)
        assert pm_key not in updated_ids
        for u in updated:
            if u.id in {d1, d2}:
                assert u.project_scope["projects"] == [str(pid)]

    async def test_update_grant_all_projects(self, db_session):
        """grant_all_projects 更新全部 Key（空 scope = 不受限）。"""
        d1, _ = await create_access_key(db_session, role="dev", project_scope=_scope(uuid.uuid4()), created_by="admin_ak")
        p1, _ = await create_access_key(db_session, role="pm", project_scope=_scope(uuid.uuid4()), created_by="admin_ak")

        updated = await update_access_key_scope(
            db_session, project_scope=[], grant_all_projects=True, actor="admin_ak"
        )
        updated_ids = {u.id for u in updated}
        assert {d1, p1}.issubset(updated_ids)
        for u in updated:
            if u.id in {d1, p1}:
                assert u.project_scope == {"systems": [], "projects": []}

    async def test_update_no_target_returns_empty(self, db_session):
        """未指定任何定位方式时返回空列表（不改任何 Key）。"""
        updated = await update_access_key_scope(
            db_session, project_scope=[uuid.uuid4()], actor="admin_ak"
        )
        assert updated == []


# ============================================================================
# update_access_key_systems 端到端（manage_system.bind_keys 路径）
# ============================================================================


class TestUpdateAccessKeySystems:
    """update_access_key_systems 集成测试（回归 bind_keys 的 CAST(:sj AS jsonb) bug）。"""

    async def test_bind_system_by_key_ids(self, db_session):
        """显式指定 key_ids 绑定 system 层，保留 projects 层不变。"""
        sid = uuid.uuid4()
        pid = uuid.uuid4()
        k1, _ = await create_access_key(
            db_session, role="pm", project_scope=_scope(pid), created_by="admin_ak"
        )
        k2, _ = await create_access_key(
            db_session, role="pm", project_scope=_scope(), created_by="admin_ak"
        )

        updated = await update_access_key_systems(
            db_session, system_ids=[sid], key_ids=[k1], actor="admin_ak"
        )
        assert len(updated) == 1
        assert updated[0].id == k1
        assert updated[0].project_scope["systems"] == [str(sid)]
        assert updated[0].project_scope["projects"] == [str(pid)]  # projects 保留

        # k2 不受影响
        k2_db = await get_access_key_by_id(db_session, k2)
        assert k2_db.project_scope == {"systems": [], "projects": []}

    async def test_bind_system_by_role_filter(self, db_session):
        """role_filter 批量绑定 system 层。"""
        sid = uuid.uuid4()
        d1, _ = await create_access_key(
            db_session, role="dev", project_scope=_scope(uuid.uuid4()), created_by="admin_ak"
        )
        pm_key, _ = await create_access_key(
            db_session, role="pm", project_scope=_scope(), created_by="admin_ak"
        )

        updated = await update_access_key_systems(
            db_session, system_ids=[sid], role_filter="dev", actor="admin_ak"
        )
        updated_ids = {u.id for u in updated}
        assert d1 in updated_ids
        assert pm_key not in updated_ids
        for u in updated:
            if u.id == d1:
                assert u.project_scope["systems"] == [str(sid)]

    async def test_bind_system_grant_all(self, db_session):
        """grant_all 绑定全部 Key 的 system 层。"""
        sid = uuid.uuid4()
        d1, _ = await create_access_key(
            db_session, role="dev", project_scope=_scope(), created_by="admin_ak"
        )

        updated = await update_access_key_systems(
            db_session, system_ids=[sid], grant_all=True, actor="admin_ak"
        )
        updated_ids = {u.id for u in updated}
        assert d1 in updated_ids
        for u in updated:
            if u.id == d1:
                assert u.project_scope["systems"] == [str(sid)]

    async def test_bind_system_accumulates(self, db_session):
        """多次绑定追加到 systems 层，不覆盖已有值。"""
        sid_a, sid_b = uuid.uuid4(), uuid.uuid4()
        k1, _ = await create_access_key(
            db_session, role="pm", project_scope=_scope(), created_by="admin_ak"
        )

        await update_access_key_systems(
            db_session, system_ids=[sid_a], key_ids=[k1], actor="admin_ak"
        )
        updated = await update_access_key_systems(
            db_session, system_ids=[sid_b], key_ids=[k1], actor="admin_ak"
        )
        assert set(updated[0].project_scope["systems"]) == {str(sid_a), str(sid_b)}
