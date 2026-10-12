"""用户页端点与组装纯函数单测（monkeypatch 数据层，不依赖 DB）。"""

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
from starlette.applications import Starlette

from mem_lake.audit.service import ActorUsageStats
from mem_lake.config import Settings
from mem_lake.visual import api as visual_api
from mem_lake.visual import create_visual_app


def make_client(base_url="http://test", **overrides: Any) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="test-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def _login(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
    assert r.status_code == 200


def _mk_key(
    *,
    role: str = "pm",
    status: str = "active",
    lax_mode: bool = False,
    scope: dict[str, Any] | None = None,
    key_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=key_id or uuid.uuid4(),
        role=role,
        status=status,
        lax_mode=lax_mode,
        created_at=datetime(2026, 10, 7, 8, 0, 0),
        revoked_at=None,
        project_scope=scope if scope is not None else {"systems": [], "projects": []},
    )


# ============================== build_keys_payload ==============================


def test_build_keys_payload_scope_resolution():
    kid, sid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = _mk_key(
        key_id=kid,
        scope={"systems": [str(sid)], "projects": [str(pid)]},
    )
    usage = {
        str(kid): ActorUsageStats(
            total_calls=10, error_calls=1, last_used_at=datetime(2026, 10, 7, 9, 0, 0)
        )
    }
    payload = visual_api.build_keys_payload(
        [key],
        {"total": 1, "active": 1, "revoked": 0, "active_24h": 1},
        {sid: ("HIS系统", "HIS")},
        {pid: "某项目"},
        usage,
        limit=50,
        offset=0,
    )
    assert payload["total"] == 1
    assert payload["limit"] == 50
    assert payload["offset"] == 0
    k = payload["keys"][0]
    assert k["key_id"] == str(kid)
    assert k["scope_unlimited"] is False
    assert k["scope"]["systems"] == [{"id": str(sid), "name": "HIS系统", "code": "HIS"}]
    assert k["scope"]["projects"] == [{"id": str(pid), "name": "某项目"}]
    assert k["usage"] == {
        "total_calls": 10,
        "error_calls": 1,
        "last_used_at": "2026-10-07T09:00:00",
    }


def test_build_keys_payload_admin_unlimited_and_empty_scope():
    kid = uuid.uuid4()
    admin = _mk_key(key_id=kid, role="admin", scope={"systems": [], "projects": []})
    payload = visual_api.build_keys_payload(
        [admin], {"total": 1, "active": 1, "revoked": 0, "active_24h": 0}, {}, {}, {}, limit=50, offset=0
    )
    k = payload["keys"][0]
    assert k["scope_unlimited"] is True
    assert k["scope"]["systems"] == []
    assert k["scope"]["projects"] == []


def test_build_keys_payload_scope_name_fallback_and_missing_usage():
    kid, sid = uuid.uuid4(), uuid.uuid4()
    key = _mk_key(key_id=kid, scope={"systems": [str(sid)], "projects": []})
    payload = visual_api.build_keys_payload(
        [key],
        {"total": 1, "active": 1, "revoked": 0, "active_24h": 0},
        {},  # 无 system 名称 → name 回退 None
        {},
        {},  # 无 usage → 填 0/null
        limit=50,
        offset=0,
    )
    k = payload["keys"][0]
    assert k["scope"]["systems"] == [{"id": str(sid), "name": None, "code": None}]
    assert k["usage"] == {"total_calls": 0, "error_calls": 0, "last_used_at": None}


def test_build_keys_payload_invalid_scope_id_skipped():
    key = _mk_key(scope={"systems": ["not-a-uuid"], "projects": ["not-a-uuid"]})
    payload = visual_api.build_keys_payload(
        [key], {"total": 1, "active": 1, "revoked": 0, "active_24h": 0}, {}, {}, {}, limit=50, offset=0
    )
    k = payload["keys"][0]
    assert k["scope"]["systems"] == []
    assert k["scope"]["projects"] == []


# ============================== 端点 ==============================


async def test_keys_requires_session():
    async with make_client() as client:
        r = await client.get("/api/keys")
        assert r.status_code == 401


async def test_keys_param_validation(monkeypatch):
    async with make_client() as client:
        await _login(client)
        for qs in ("role=bogus", "status=bogus", "limit=abc", "limit=0", "limit=201", "offset=-1"):
            r = await client.get(f"/api/keys?{qs}")
            assert r.status_code == 400, qs


class _FakeSessionCtx:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: Any) -> None:
        return None


async def test_keys_endpoint_contract(monkeypatch):
    kid, sid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = _mk_key(key_id=kid, scope={"systems": [str(sid)], "projects": [str(pid)]})

    captured: dict[str, Any] = {}

    async def fake_summary(session, *, role=None, status=None, active_since=None):
        captured["role"] = role
        captured["status"] = status
        captured["active_since"] = active_since
        return {"total": 1, "active": 1, "revoked": 0, "active_24h": 1}

    async def fake_list(session, *, role=None, status=None, lax_mode=None, limit=None, offset=0):
        captured["limit"] = limit
        captured["offset"] = offset
        return [key]

    async def fake_usage(session):
        return {str(kid): ActorUsageStats(total_calls=5, error_calls=0, last_used_at=None)}

    async def fake_systems(session):
        return [SimpleNamespace(id=sid, name="HIS系统", code="HIS")]

    async def fake_profiles(session, *, project_ids):
        captured["project_ids"] = project_ids
        return [SimpleNamespace(project_id=pid, title="某项目")]

    monkeypatch.setattr(visual_api, "readonly_session", lambda: _FakeSessionCtx())
    monkeypatch.setattr(visual_api, "get_access_key_summary", fake_summary)
    monkeypatch.setattr(visual_api, "list_access_keys", fake_list)
    monkeypatch.setattr(visual_api, "get_actor_usage_stats", fake_usage)
    monkeypatch.setattr(visual_api, "list_systems", fake_systems)
    monkeypatch.setattr(visual_api, "get_project_profiles_by_ids", fake_profiles)

    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/keys?role=pm&status=active&limit=20&offset=40")
        assert r.status_code == 200
        data = r.json()
        # 筛选/分页参数透传
        assert captured["role"] == "pm"
        assert captured["status"] == "active"
        assert captured["limit"] == 20
        assert captured["offset"] == 40
        assert captured["active_since"] is not None
        assert captured["project_ids"] == [pid]
        assert data["total"] == 1
        assert data["limit"] == 20
        assert data["offset"] == 40
        assert data["summary"]["active_24h"] == 1
        k = data["keys"][0]
        assert k["key_id"] == str(kid)
        assert k["scope"]["systems"][0]["name"] == "HIS系统"
        assert k["scope"]["projects"][0]["name"] == "某项目"
        assert k["usage"]["total_calls"] == 5
