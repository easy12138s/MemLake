"""manage_tools 单元测试：核心纯逻辑（无需 DB / FastMCP 上下文）。

覆盖：
- _resolve_profile_id：给定 ID 原样返回；None 时自动生成新 UUID
- _normalize_uuid_list：列表 / 字符串（逗号/空格/JSON）/ None 归一化，容错非法片段
- create_mcp_server 注册 manage_project_profile 时，project_id 为可选（参数排序合法，
  不触发 Pydantic "Non-default argument follows default argument"）
"""

import json
import uuid

from mem_lake.gateway.server import create_mcp_server
from mem_lake.gateway.tools.manage_tools import (
    _build_mcp_config,
    _build_onboarding_prompt,
    _normalize_uuid_list,
    _resolve_profile_id,
)


def test_resolve_profile_id_given_returns_same():
    """给定 project_id 应原样返回。"""
    pid = uuid.uuid4()
    assert _resolve_profile_id(pid) == pid


def test_resolve_profile_id_none_generates_new_uuid():
    """未传 project_id 时自动生成新 UUID，且每次不同。"""
    generated = _resolve_profile_id(None)
    assert isinstance(generated, uuid.UUID)
    assert _resolve_profile_id(None) != generated


def test_normalize_uuid_list_from_list():
    """列表（UUID 或字符串）归一化为 UUID 列表。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _normalize_uuid_list([a, str(b)]) == [a, b]


def test_normalize_uuid_list_from_comma_string():
    """逗号/空格/分号分隔字符串归一化为 UUID 列表。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _normalize_uuid_list(f"{a}, {b}") == [a, b]
    assert _normalize_uuid_list(f"{a} {b}") == [a, b]
    assert _normalize_uuid_list(f"{a};{b}") == [a, b]


def test_normalize_uuid_list_from_json_string():
    """JSON 数组字符串归一化为 UUID 列表。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _normalize_uuid_list(f'["{a}","{b}"]') == [a, b]


def test_normalize_uuid_list_skips_invalid_and_empty():
    """非法片段被跳过；空串/纯空白返回 None。"""
    a = uuid.uuid4()
    assert _normalize_uuid_list(f"{a}, not-a-uuid") == [a]
    assert _normalize_uuid_list("") is None
    assert _normalize_uuid_list("   ") is None
    assert _normalize_uuid_list(None) is None


def test_normalize_uuid_list_all_invalid_raises():
    """全部片段非法时抛错（AUDIT §2.16：此前静默跳过导致空操作无提示）。"""
    import pytest

    with pytest.raises(ValueError, match="全部为非法"):
        _normalize_uuid_list("not-a-uuid, still-not")


def test_build_mcp_config_contains_url_and_key():
    """_build_mcp_config 产出合法 JSON，含 url 与 X-MCP-Key 头。"""
    url = "http://example:8000/mcp"
    key = "ak_test_123"
    cfg = json.loads(_build_mcp_config(url, key))
    assert cfg["mcpServers"]["mem-lake"]["url"] == url
    assert cfg["mcpServers"]["mem-lake"]["headers"]["X-MCP-Key"] == key


def test_build_onboarding_prompt_references_skill_and_excludes_key():
    """_build_onboarding_prompt 指引调用 get_role_skills 并写入 .agents/skills/，且不出现 Key。"""
    role = "dev"
    key = "ak_secret_should_not_appear"
    prompt = _build_onboarding_prompt(role)
    assert f'get_role_skills(role="{role}")' in prompt
    assert f".agents/skills/mem-lake-{role}/SKILL.md" in prompt
    assert "## 3. 完成" in prompt
    assert key not in prompt


def test_create_mcp_server_registers_manage_project_profile():
    """注册所有工具时 manage_project_profile 的 project_id 应为可选（参数顺序合法）。

    project_id 现在排在 action/profile 之后且带默认值；若参数顺序写错，
    FastMCP 在注册建 schema 时会抛 Pydantic 错误，这里 create_mcp_server() 会直接 raise。
    """
    mcp = create_mcp_server()
    assert mcp is not None
