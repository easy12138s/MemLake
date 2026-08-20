"""Access Key 服务层：认证、创建、吊销、查询。

对齐 PDD 3.1 认证机制与 3.5 RBAC 模型。提供 gateway 层（M6）调用的查询接口，
封装 access_key 表的 CRUD + bcrypt 校验逻辑。

职责边界：
- authenticate_access_key：完整认证流程（parse_key_id → 查 DB → bcrypt 校验 → 返回身份信息）
- create_access_key：生成 Access Key（明文仅返回一次）+ bcrypt hash 持久化
- revoke_access_key：吊销（status=revoked + revoked_at），幂等
- list_access_keys：按条件查询（不含 key_hash，避免泄漏）
- get_access_key_by_id：按 id 查询（供 gateway 层校验 project_scope 使用）

不 commit，由调用方（gateway 工具层）控制事务边界。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from mem_lake.audit.service import write_audit_log
from mem_lake.auth.models import (
    AccessKey,
    build_plaintext,
    generate_access_key,
    generate_secret,
    hash_access_key,
    parse_key_id,
    verify_access_key,
)
from mem_lake.auth.rbac import validate_role


class AccessKeyNotFoundError(Exception):
    """Access Key 不存在时抛出。"""


class AccessKeyRevokedError(Exception):
    """Access Key 已吊销时抛出。"""


async def get_access_key_by_id(
    session: AsyncSession, key_id: uuid.UUID
) -> AccessKey | None:
    """按 id 查询 Access Key（含已吊销）。

    返回 None 表示不存在。调用方根据 status 字段判断是否 active。
    """
    result = await session.execute(
        select(AccessKey).where(AccessKey.id == key_id)
    )
    return result.scalar_one_or_none()


async def authenticate_access_key(
    session: AsyncSession, plaintext: str
) -> dict | None:
    """完整 Access Key 认证流程。

    流程：
    1. parse_key_id 从明文解析 row id（格式 ak_{id_hex}.{secret}）
    2. 按 id 查 DB access_key 表
    3. 校验 status=active（已吊销返回 None）
    4. bcrypt 校验明文与 key_hash
    5. 返回 {key_id, role, project_scope} 或 None（任一步骤失败）

    不 commit。每次请求重新验证身份（PDD 3.1：不从连接历史推断授权）。
    """
    key_id = parse_key_id(plaintext)
    if key_id is None:
        return None

    access_key = await get_access_key_by_id(session, key_id)
    if access_key is None:
        return None

    if access_key.status != "active":
        return None

    if not verify_access_key(plaintext, access_key.key_hash):
        return None

    return {
        "key_id": access_key.id,
        "role": access_key.role,
        "project_scope": access_key.project_scope or [],
    }


async def create_access_key(
    session: AsyncSession,
    *,
    role: str,
    project_scope: list[uuid.UUID],
    created_by: str = "system",
) -> tuple[uuid.UUID, str]:
    """创建 Access Key。

    流程：
    1. validate_role 校验角色合法性
    2. generate_access_key 生成明文（ak_{id_hex}.{secret}）
    3. hash_access_key bcrypt 哈希
    4. INSERT access_key 表
    5. write_audit_log 记录创建审计

    返回 (key_id, plaintext)，明文仅返回一次，调用方负责安全传递给用户。

    不 commit。
    """
    if not validate_role(role):
        raise ValueError(f"非法角色: {role}")

    key_id, plaintext = generate_access_key()
    key_hash = hash_access_key(plaintext)

    access_key = AccessKey(
        id=key_id,
        key_hash=key_hash,
        role=role,
        project_scope=[str(pid) for pid in project_scope],
        status="active",
    )
    session.add(access_key)
    await session.flush()

    await write_audit_log(
        session,
        actor=created_by,
        action="create",
        target_type="access_key",
        target_id=key_id,
        detail={"role": role, "project_scope_count": len(project_scope)},
    )

    return key_id, plaintext


async def revoke_access_key(
    session: AsyncSession,
    *,
    key_id: uuid.UUID,
    actor: str = "system",
) -> None:
    """吊销 Access Key（status=revoked + revoked_at）。

    幂等：已吊销的 Access Key 重复吊销不报错。
    不存在抛 AccessKeyNotFoundError。

    不 commit。
    """
    access_key = await get_access_key_by_id(session, key_id)
    if access_key is None:
        raise AccessKeyNotFoundError(f"Access Key 不存在: {key_id}")

    if access_key.status == "revoked":
        # 幂等：已吊销直接返回
        return

    await session.execute(
        update(AccessKey)
        .where(AccessKey.id == key_id)
        .values(status="revoked", revoked_at=datetime.now(timezone.utc))
    )

    await write_audit_log(
        session,
        actor=actor,
        action="revoke",
        target_type="access_key",
        target_id=key_id,
        detail={"previous_role": access_key.role},
    )


async def list_access_keys(
    session: AsyncSession,
    *,
    role: str | None = None,
    status: str | None = None,
) -> list[AccessKey]:
    """按条件列出 Access Key（不含 key_hash，避免泄漏）。

    过滤规则：
    - role=None：不过滤角色
    - status=None：返回所有状态（active + revoked）
    - 返回的 AccessKey 对象的 key_hash 字段为 None（通过 defer 排除）

    不 commit。
    """
    from sqlalchemy.orm import defer

    stmt = select(AccessKey).options(defer(AccessKey.key_hash))
    if role is not None:
        stmt = stmt.where(AccessKey.role == role)
    if status is not None:
        stmt = stmt.where(AccessKey.status == status)

    stmt = stmt.order_by(AccessKey.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_access_key_scope(
    session: AsyncSession,
    *,
    project_scope: list[uuid.UUID],
    key_ids: list[uuid.UUID] | None = None,
    role_filter: str | None = None,
    grant_all_projects: bool = False,
    actor: str = "system",
) -> list[AccessKey]:
    """动态更新 Access Key 的项目范围（project_scope）。

    定位目标 Key（三选一，优先级 key_ids > role_filter > grant_all_projects）：
    - key_ids：显式指定一个或多个 Key
    - role_filter：按角色批量（如 "dev" → 所有 dev Key）
    - grant_all_projects：全部 Key（用于「一键全项目」授权）

    未指定任何定位方式时抛 ValueError。project_scope 为新的项目 ID 列表
    （空列表 = 不受限，语义同 admin 全项目）。仅改 project_scope，不动 role/hash/status。

    返回受影响、且重新查回的 AccessKey 列表（key_hash 已 defer）。

    不 commit。
    """
    target_ids = await _resolve_scope_targets(
        session, key_ids, role_filter, grant_all_projects
    )
    if not target_ids:
        return []

    scope = [str(pid) for pid in project_scope]
    await session.execute(
        update(AccessKey)
        .where(AccessKey.id.in_(target_ids))
        .values(project_scope=scope)
    )

    # 重新查回（defer key_hash）用于出参，确保返回最新 project_scope
    refreshed = await session.execute(
        select(AccessKey)
        .options(defer(AccessKey.key_hash))
        .where(AccessKey.id.in_(target_ids))
        .order_by(AccessKey.created_at.desc())
    )
    updated = list(refreshed.scalars().all())

    await write_audit_log(
        session,
        actor=actor,
        action="update_scope",
        target_type="access_key",
        detail={
            "target_count": len(updated),
            "key_ids": [str(i) for i in target_ids],
            "role_filter": role_filter,
            "grant_all_projects": grant_all_projects,
            "project_scope": scope,
        },
    )
    return updated


async def rotate_access_key(
    session: AsyncSession,
    *,
    key_id: uuid.UUID,
    actor: str = "system",
) -> tuple[AccessKey, str]:
    """轮换 Access Key 密钥（保留 row id，旧明文立即失效）。

    流程：
    1. 按 id 查 Key，不存在抛 AccessKeyNotFoundError
    2. 已吊销抛 AccessKeyRevokedError（不可轮换）
    3. 生成新 secret，拼装新明文（ak_{id_hex}.{new_secret}），重算 key_hash
    4. 更新行 key_hash，旧明文因 hash 不匹配立即失效

    返回 (access_key, plaintext)，明文仅此一次返回，调用方负责安全保存。

    不 commit。
    """
    access_key = await get_access_key_by_id(session, key_id)
    if access_key is None:
        raise AccessKeyNotFoundError(f"Access Key 不存在: {key_id}")
    if access_key.status != "active":
        raise AccessKeyRevokedError(f"已吊销的 Access Key 不可轮换: {key_id}")

    secret = generate_secret()
    plaintext = build_plaintext(access_key.id, secret)
    access_key.key_hash = hash_access_key(plaintext)
    await session.flush()

    await write_audit_log(
        session,
        actor=actor,
        action="rotate",
        target_type="access_key",
        target_id=key_id,
    )
    return access_key, plaintext


async def _resolve_scope_targets(
    session: AsyncSession,
    key_ids: list[uuid.UUID] | None,
    role_filter: str | None,
    grant_all_projects: bool,
) -> list[uuid.UUID]:
    """解析 update_access_key_scope 的目标 Key ID 列表。

    key_ids 优先；其次 role_filter（按角色查全部）；再次 grant_all_projects
    （全部 Key）；都为空返回空列表（调用方据此视为非法入参）。
    """
    if key_ids:
        return [uuid.UUID(str(k)) for k in key_ids]
    if role_filter is None and not grant_all_projects:
        # 未指定任何定位方式：视为非法入参，返回空（调用方据此返回 []）
        return []
    stmt = select(AccessKey.id)
    if role_filter is not None:
        stmt = stmt.where(AccessKey.role == role_filter)
    # grant_all_projects=True 且不带 role_filter：不加 role 过滤 → 全部 Key
    result = await session.execute(stmt)
    return list(result.scalars().all())
