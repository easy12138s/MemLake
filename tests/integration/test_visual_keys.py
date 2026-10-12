"""用户页数据层集成测试：usage 聚合 + summary 汇总（真实 PG + 事务回滚隔离）。

开发库有存量 audit/access_key 数据，故断言采用「全新 actor / baseline 差值」口径，
不假设库为空。
"""

import uuid
from datetime import UTC, datetime, timedelta

from mem_lake.audit.service import get_actor_usage_stats, write_audit_log
from mem_lake.auth.models import AccessKey
from mem_lake.auth.service import get_access_key_summary


def _active_since_24h() -> datetime:
    """近 24h 起点（naive UTC，对齐 audit_log.created_at 无时区列）。"""
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)


async def test_get_actor_usage_stats_aggregates(db_session):
    """按 actor 聚合 tool_call：total / error / last_used；非 tool_call 不计数。"""
    kid = str(uuid.uuid4())
    await write_audit_log(
        db_session, actor=kid, action="tool_call", target_type="tool",
        detail={"result_status": "success"},
    )
    await write_audit_log(
        db_session, actor=kid, action="tool_call", target_type="tool",
        detail={"result_status": "error"},
    )
    await write_audit_log(
        db_session, actor=kid, action="tool_call", target_type="tool",
        detail={"result_status": "success"},
    )
    await write_audit_log(
        db_session, actor=kid, action="write", target_type="node", detail={}
    )

    stats = await get_actor_usage_stats(db_session)
    assert kid in stats
    assert stats[kid].total_calls == 3
    assert stats[kid].error_calls == 1
    assert stats[kid].last_used_at is not None


async def test_get_actor_usage_stats_unknown_actor_absent(db_session):
    """无调用记录的 actor 不出现在聚合结果中。"""
    stats = await get_actor_usage_stats(db_session)
    assert str(uuid.uuid4()) not in stats


async def test_get_access_key_summary_counts(db_session):
    """total/active/revoked 随新增正确（baseline 差值）；未写调用则 active_24h 不变。"""
    active_since = _active_since_24h()
    base = await get_access_key_summary(
        db_session, role=None, status=None, active_since=active_since
    )

    db_session.add_all([
        AccessKey(key_hash="h1", role="pm", status="active",
                  project_scope={"systems": [], "projects": []}),
        AccessKey(key_hash="h2", role="pm", status="active",
                  project_scope={"systems": [], "projects": []}),
        AccessKey(key_hash="h3", role="dev", status="revoked",
                  project_scope={"systems": [], "projects": []}),
    ])
    await db_session.flush()

    summary = await get_access_key_summary(
        db_session, role=None, status=None, active_since=active_since
    )
    assert summary["total"] == base["total"] + 3
    assert summary["active"] == base["active"] + 2
    assert summary["revoked"] == base["revoked"] + 1
    assert summary["active_24h"] == base["active_24h"]


async def test_get_access_key_summary_active_24h(db_session):
    """新增 key 并写近 24h tool_call 后，active_24h 相应 +1。"""
    active_since = _active_since_24h()
    base = await get_access_key_summary(
        db_session, role=None, status=None, active_since=active_since
    )

    k = AccessKey(key_hash="hx", role="pm", status="active",
                  project_scope={"systems": [], "projects": []})
    db_session.add(k)
    await db_session.flush()
    await write_audit_log(
        db_session, actor=str(k.id), action="tool_call", target_type="tool",
        detail={"result_status": "success"},
    )

    summary = await get_access_key_summary(
        db_session, role=None, status=None, active_since=active_since
    )
    assert summary["total"] == base["total"] + 1
    assert summary["active_24h"] == base["active_24h"] + 1


async def test_get_access_key_summary_filter(db_session):
    """role/status 筛选只影响当前口径（baseline 差值）。"""
    active_since = _active_since_24h()
    base_all = await get_access_key_summary(
        db_session, role=None, status=None, active_since=active_since
    )
    base_admin = await get_access_key_summary(
        db_session, role="admin", status=None, active_since=active_since
    )

    db_session.add(AccessKey(key_hash="ha", role="admin", status="active",
                             project_scope={"systems": [], "projects": []}))
    await db_session.flush()

    summary_all = await get_access_key_summary(
        db_session, role=None, status=None, active_since=active_since
    )
    summary_admin = await get_access_key_summary(
        db_session, role="admin", status=None, active_since=active_since
    )
    assert summary_all["total"] == base_all["total"] + 1
    assert summary_admin["total"] == base_admin["total"] + 1
