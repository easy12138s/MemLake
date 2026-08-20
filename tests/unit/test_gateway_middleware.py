"""网关 RBAC 中间件测试：工具列表按角色过滤。

纯单测，无 DB 依赖。验证 RBACMiddleware.on_list_tools 按角色裁剪工具列表。
"""

from types import SimpleNamespace

import pytest

from mem_lake.auth.rbac import ADMIN_ONLY_TOOLS, ADMIN_TOOLS, PM_TOOLS
from mem_lake.gateway.middleware import RBACMiddleware


def _fake_token(role: str):
    return SimpleNamespace(claims={"role": role, "key_id": "k"})


def _all_tools():
    # 全部工具 = admin 工具集（pm + dev + admin 专属的并集）
    return [SimpleNamespace(name=n) for n in ADMIN_TOOLS]


async def _call_next(context):
    return _all_tools()


@pytest.fixture
def middleware():
    return RBACMiddleware()


async def test_pm_sees_only_own_tools(monkeypatch, middleware):
    """pm 列工具只看得到 pm 工具集，不含 admin 专属工具。"""
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token",
        lambda: _fake_token("pm"),
    )
    result = await middleware.on_list_tools(
        context=SimpleNamespace(), call_next=_call_next
    )
    names = {t.name for t in result}
    assert names == set(PM_TOOLS)
    assert not any(n in ADMIN_ONLY_TOOLS for n in names)


async def test_dev_sees_only_own_tools(monkeypatch, middleware):
    """dev 列工具只看得到 dev 工具集，不含 admin 专属工具。"""
    from mem_lake.auth.rbac import DEV_TOOLS

    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token",
        lambda: _fake_token("dev"),
    )
    result = await middleware.on_list_tools(
        context=SimpleNamespace(), call_next=_call_next
    )
    names = {t.name for t in result}
    assert names == set(DEV_TOOLS)
    assert not any(n in ADMIN_ONLY_TOOLS for n in names)


async def test_admin_sees_all(monkeypatch, middleware):
    """admin 列工具看到全部（含 admin 专属）。"""
    from mem_lake.auth.rbac import ADMIN_TOOLS

    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token",
        lambda: _fake_token("admin"),
    )
    result = await middleware.on_list_tools(
        context=SimpleNamespace(), call_next=_call_next
    )
    names = {t.name for t in result}
    assert names == set(ADMIN_TOOLS)


async def test_unauthenticated_empty(monkeypatch, middleware):
    """未认证（无 token）返回空列表，不泄露任何工具。"""
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token", lambda: None
    )
    result = await middleware.on_list_tools(
        context=SimpleNamespace(), call_next=_call_next
    )
    assert result == []


async def test_missing_role_empty(monkeypatch, middleware):
    """token 缺少 role claims 返回空列表。"""
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token",
        lambda: SimpleNamespace(claims={}),
    )
    result = await middleware.on_list_tools(
        context=SimpleNamespace(), call_next=_call_next
    )
    assert result == []
