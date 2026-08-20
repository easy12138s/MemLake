"""端到端验证：FastMCP 真实调用 RBACMiddleware.on_list_tools 并按角色过滤。

不依赖 Docker / 密钥：用 FastMCP 内存客户端触发真实中间件链，
monkeypatch get_access_token 模拟不同角色，验证 tools/list 结果。
"""

from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from mem_lake.gateway.middleware import RBACMiddleware


@pytest.fixture
def app():
    m = FastMCP("filter-e2e")
    m.add_middleware(RBACMiddleware())

    @m.tool(name="publish_requirement")
    def _pub() -> str:
        return "pub"

    @m.tool(name="review_approve")
    def _adm() -> str:
        return "adm"

    return m


async def _list_as(app, role, monkeypatch):
    if role is None:
        monkeypatch.setattr(
            "mem_lake.gateway.middleware.get_access_token", lambda: None
        )
    else:
        monkeypatch.setattr(
            "mem_lake.gateway.middleware.get_access_token",
            lambda: SimpleNamespace(claims={"role": role}),
        )
    async with Client(app) as client:
        tools = await client.list_tools()
    return {t.name for t in tools}


async def test_admin_sees_all(app, monkeypatch):
    """admin 角色看到全部已注册工具。"""
    names = await _list_as(app, "admin", monkeypatch)
    assert names == {"publish_requirement", "review_approve"}


async def test_pm_sees_only_pm(app, monkeypatch):
    """pm 角色只看到 pm 工具集内的工具，admin 专属被过滤。"""
    names = await _list_as(app, "pm", monkeypatch)
    assert names == {"publish_requirement"}


async def test_unauthenticated_empty(app, monkeypatch):
    """未认证返回空列表，不泄露任何工具。"""
    names = await _list_as(app, None, monkeypatch)
    assert names == set()
