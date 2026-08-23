"""manage_tools 集成测试：manage_project_profile 经真实 MCP 链路端到端验证。

用 FastMCP 内存客户端触发真实中间件链 + 生命周期（需 Docker PG/AGE/embedding）。
通过 monkeypatch 认证相关函数为 admin，避免依赖真实 Access Key / HTTP 头。
"""

import json
import uuid

import pytest
from fastmcp import Client
from fastmcp.server.auth import AccessToken

from mem_lake.gateway.server import create_mcp_server


@pytest.fixture
def admin_app(monkeypatch):
    """构造一个认证为 admin 的 FastMCP 实例（内存客户端可直连）。

    内存传输无真实 HTTP request，middleware 的 on_request 会尝试设置
    scope['user'] 但 get_http_request() 在内存传输下抛 RuntimeError。这里 patch
    get_http_request 返回带 scope 的伪请求，让真实令牌链路（认证 → 设置
    scope['user'] → get_access_token 读取）完整跑通，等价于 admin 持有效 Key。
    """

    class _FakeRequest:
        def __init__(self):
            self.scope: dict = {}

    # 同一请求内 get_http_request 必须返回同一实例，否则 on_request 设置的
    # scope['user'] 无法被 on_call_tool 的 get_access_token 读取。
    _req = _FakeRequest()

    async def _fake_auth(_session, _key):
        return {
            "key_id": uuid.uuid4(),
            "role": "admin",
            "project_scope": [],
            "lax_mode": False,
        }

    monkeypatch.setattr(
        "mem_lake.gateway.middleware.extract_access_key_from_headers",
        lambda _headers: "dummy-key",
    )
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.authenticate_access_key", _fake_auth
    )
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_http_request",
        lambda: _req,
    )

    def _fake_token(*_args, **_kwargs):
        return AccessToken(
            token="dummy",
            client_id=str(uuid.uuid4()),
            scopes=["admin"],
            claims={
                "role": "admin",
                "key_id": str(uuid.uuid4()),
                "project_scope": [],
                "lax_mode": False,
            },
        )

    # 工具内部依赖（validate_project_access / get_current_key_id 等）与 RBAC
    # 分别从 dependencies / middleware 两个模块命名空间读取 get_access_token，
    # 需同时 patch 才能让内存客户端以 admin 身份通过。
    monkeypatch.setattr(
        "mem_lake.gateway.dependencies.get_access_token", _fake_token
    )
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token", _fake_token
    )
    return create_mcp_server()


def _parse(result):
    """从 CallToolResult 解析出参 dict，失败时抛错。"""
    if getattr(result, "is_error", False):
        text = ""
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "text"):
                text = item.text
                break
        raise AssertionError(f"工具返回错误: {text or '未知错误'}")
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    for item in getattr(result, "content", []) or []:
        if hasattr(item, "text"):
            return json.loads(item.text)
    return {}


def _profile_payload(name: str = "AutoIDProject") -> dict:
    return {
        "title": "自动ID项目画像",
        "content": "测试描述",
        "properties": {
            "name": name,
            "description": "自动生成 project_id 测试",
            "tech_stack": ["Python"],
            "architecture": "monolith",
        },
        "tags": [],
    }


async def test_manage_project_profile_auto_id(admin_app):
    """create 不传 project_id → 服务端自动生成并通过 project_id 出参返回。"""
    async with Client(admin_app) as client:
        result = await client.call_tool(
            "manage_project_profile",
            {"action": "create", "profile": _profile_payload()},
        )
        data = _parse(result)
        assert "project_id" in data
        pid = uuid.UUID(data["project_id"])
        assert isinstance(pid, uuid.UUID)
        assert data["action"] == "create"
        assert data["status"] == "approved"

        # 生成的 project_id 可经 get_project_profile 查回
        prof = await client.call_tool(
            "get_project_profile", {"project_id": str(pid)}
        )
        prof_data = _parse(prof)
        assert prof_data.get("project_id") == str(pid)


async def test_manage_project_profile_explicit_id_returns_same(admin_app):
    """create 传入 project_id → 出参原样返回同一 ID。"""
    pid = uuid.uuid4()
    async with Client(admin_app) as client:
        result = await client.call_tool(
            "manage_project_profile",
            {
                "action": "create",
                "project_id": str(pid),
                "profile": _profile_payload("ExplicitProject"),
            },
        )
        data = _parse(result)
        assert uuid.UUID(data["project_id"]) == pid


async def test_manage_access_key_rotate(admin_app):
    """rotate 返回新明文，且 key_id 不变。"""
    async with Client(admin_app) as client:
        created = await client.call_tool(
            "manage_access_key",
            {
                "action": "create",
                "role": "dev",
                "project_scope": [str(uuid.uuid4())],
            },
        )
        created_data = _parse(created)
        key_id = created_data["created"]["key_id"]
        old_plaintext = created_data["created"]["plaintext"]

        rotated = await client.call_tool(
            "manage_access_key", {"action": "rotate", "key_id": key_id}
        )
        rotated_data = _parse(rotated)
        assert rotated_data["action"] == "rotate"
        assert rotated_data["rotated"]["key_id"] == key_id
        new_plaintext = rotated_data["rotated"]["plaintext"]
        assert new_plaintext.startswith("ak_")
        assert new_plaintext != old_plaintext

        # create / rotate 均返回两部分初始化产物
        _assert_onboarding(created_data["created"], old_plaintext, "dev")
        _assert_onboarding(rotated_data["rotated"], new_plaintext, "dev")


def _assert_onboarding(output: dict, plaintext: str, role: str) -> None:
    """断言 create/rotate 出参含 mcp_config（给用户）与 onboarding_prompt（给 Agent）。"""
    import json as _json

    mcp_config = output["mcp_config"]
    assert isinstance(mcp_config, str) and mcp_config.strip()
    cfg = _json.loads(mcp_config)
    assert cfg["mcpServers"]["mem-lake"]["url"]
    assert cfg["mcpServers"]["mem-lake"]["headers"]["X-MCP-Key"] == plaintext

    prompt = output["onboarding_prompt"]
    assert isinstance(prompt, str) and prompt.strip()
    assert f'get_role_skills(role="{role}")' in prompt
    assert ".agents/skills/mem-lake-{role}/SKILL.md".replace("{role}", role) in prompt
    # 安全：Key 不应出现在给 Agent 的提示词里
    assert plaintext not in prompt


async def test_manage_access_key_create_onboarding(admin_app):
    """create 返回 mcp_config（JSON 给用户）与 onboarding_prompt（不含 Key，给 Agent）。"""
    async with Client(admin_app) as client:
        created = await client.call_tool(
            "manage_access_key",
            {
                "action": "create",
                "role": "pm",
                "project_scope": [str(uuid.uuid4())],
            },
        )
        data = _parse(created)
        assert data["action"] == "create"
        _assert_onboarding(data["created"], data["created"]["plaintext"], "pm")


async def test_manage_access_key_update_scope_by_role(admin_app):
    """update_scope 按 role_filter 批量更新该角色 Key 的 project_scope。"""
    async with Client(admin_app) as client:
        pid = uuid.uuid4()
        # 创建两个 dev Key
        c1 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        c2 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        k1, k2 = c1["created"]["key_id"], c2["created"]["key_id"]

        updated = await client.call_tool(
            "manage_access_key",
            {
                "action": "update_scope",
                "project_scope": [str(pid)],
                "role_filter": "dev",
            },
        )
        updated_data = _parse(updated)
        assert updated_data["action"] == "update_scope"
        scoped_ids = {k["key_id"] for k in updated_data["scoped"]}
        # 本次创建的两个 dev Key 必须被更新
        assert k1 in scoped_ids and k2 in scoped_ids
        for k in updated_data["scoped"]:
            if k["key_id"] in {k1, k2}:
                assert k["project_scope"] == [str(pid)]


async def test_manage_access_key_update_scope_requires_target(admin_app):
    """update_scope 未指定任何定位方式时返回空 scoped 列表。"""
    async with Client(admin_app) as client:
        updated = await client.call_tool(
            "manage_access_key",
            {"action": "update_scope", "project_scope": [str(uuid.uuid4())]},
        )
        updated_data = _parse(updated)
        assert updated_data["action"] == "update_scope"
        assert updated_data["scoped"] == []


async def test_manage_access_key_update_scope_key_ids_as_string(admin_app):
    """update_scope 的 key_ids 以字符串（逗号分隔）传入时仍正确归一化并更新。

    复现并修复：部分客户端将数组序列化成字符串，导致 pydantic 报
    "Input should be a list"。工具层 _normalize_uuid_list 归一化后路径可用。
    """
    async with Client(admin_app) as client:
        pid = uuid.uuid4()
        c1 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        c2 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        k1, k2 = c1["created"]["key_id"], c2["created"]["key_id"]

        # 以逗号分隔的字符串传入 key_ids（模拟客户端序列化）
        updated = await client.call_tool(
            "manage_access_key",
            {
                "action": "update_scope",
                "project_scope": [str(pid)],
                "key_ids": f"{k1},{k2}",
            },
        )
        updated_data = _parse(updated)
        assert updated_data["action"] == "update_scope"
        scoped_ids = {k["key_id"] for k in updated_data["scoped"]}
        assert {k1, k2} == scoped_ids
        for k in updated_data["scoped"]:
            assert k["project_scope"] == [str(pid)]


async def test_manage_access_key_set_mode_by_key_ids(admin_app):
    """set_mode 显式指定 key_ids 可把该 Key 设为宽松（lax_mode=true）。"""
    async with Client(admin_app) as client:
        c1 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        c2 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        k1 = c1["created"]["key_id"]
        assert c1["created"]["lax_mode"] is False  # create 默认严格

        updated = await client.call_tool(
            "manage_access_key",
            {"action": "set_mode", "lax_mode": True, "key_ids": [k1]},
        )
        updated_data = _parse(updated)
        assert updated_data["action"] == "set_mode"
        mode_ids = {k["key_id"] for k in updated_data["mode_set"]}
        assert k1 in mode_ids
        target = next(k for k in updated_data["mode_set"] if k["key_id"] == k1)
        assert target["lax_mode"] is True
        # 未被指定的 k2 不受影响
        assert c2["created"]["key_id"] not in mode_ids


async def test_manage_access_key_set_mode_by_role(admin_app):
    """set_mode 按 role_filter 可批量将该角色全部 Key 设为宽松。"""
    async with Client(admin_app) as client:
        c1 = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        k1 = c1["created"]["key_id"]

        updated = await client.call_tool(
            "manage_access_key",
            {"action": "set_mode", "lax_mode": True, "role_filter": "dev"},
        )
        updated_data = _parse(updated)
        mode_ids = {k["key_id"] for k in updated_data["mode_set"]}
        assert k1 in mode_ids  # 本次创建的 dev Key 都被设为宽松


async def test_manage_access_key_list_filter_by_lax_mode(admin_app):
    """list 按 lax_mode=true 过滤可查到宽松 Key，严格 Key 不在其中。"""
    async with Client(admin_app) as client:
        created = _parse(
            await client.call_tool(
                "manage_access_key",
                {"action": "create", "role": "dev", "project_scope": []},
            )
        )
        target_key = created["created"]["key_id"]
        await client.call_tool(
            "manage_access_key",
            {"action": "set_mode", "lax_mode": True, "key_ids": [target_key]},
        )

        lax_list = _parse(
            await client.call_tool(
                "manage_access_key", {"action": "list", "lax_mode": True}
            )
        )
        lax_ids = {k["key_id"] for k in lax_list["listed"]}
        assert target_key in lax_ids
        for k in lax_list["listed"]:
            assert k["lax_mode"] is True

        strict_list = _parse(
            await client.call_tool(
                "manage_access_key", {"action": "list", "lax_mode": False}
            )
        )
        strict_ids = {k["key_id"] for k in strict_list["listed"]}
        assert target_key not in strict_ids
        for k in strict_list["listed"]:
            assert k["lax_mode"] is False
