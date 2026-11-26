"""manage_tools 单元测试：核心纯逻辑（无需 DB / FastMCP 上下文）。

覆盖：
- _resolve_profile_id：给定 ID 原样返回；None 时自动生成新 UUID
- _normalize_uuid_list：列表 / 字符串（逗号/空格/JSON）/ None 归一化，容错非法片段
- create_mcp_server 注册 manage_project_profile 时，project_id 为可选（参数排序合法，
  不触发 Pydantic "Non-default argument follows default argument"）
"""

import json
import uuid

import pytest

from mem_lake.approval.validation import PayloadValidationError
from mem_lake.gateway.server import create_mcp_server
from mem_lake.gateway.tools.manage_tools import (
    _build_mcp_config,
    _build_user_hint,
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


def test_build_user_hint_references_skill_and_excludes_key():
    """_build_user_hint：指向 MCP 资源与 get_skill，不再指引 GitHub，且不含 Key。"""
    role = "dev"
    key = "ak_secret_should_not_appear"
    hint = _build_user_hint(role)
    assert f"memlake://skills/{role}/SKILL" in hint
    assert "get_skill" in hint
    assert "raw.githubusercontent.com" not in hint
    assert f"{role} 角色" in hint
    assert key not in hint


def test_create_mcp_server_registers_manage_project_profile():
    """注册所有工具时 manage_project_profile 的 project_id 应为可选（参数顺序合法）。

    project_id 现在排在 action/profile 之后且带默认值；若参数顺序写错，
    FastMCP 在注册建 schema 时会抛 Pydantic 错误，这里 create_mcp_server() 会直接 raise。
    """
    mcp = create_mcp_server()
    assert mcp is not None


class TestWorkDirValidation:
    """work_dir 必填 + 跨平台绝对路径判定。"""

    def test_accepts_cross_platform_absolute_paths(self):
        from mem_lake.gateway.tools.manage_tools import _is_absolute_any_platform

        for ok in (
            r"D:\EasyFiles\private\project\MemLake",
            "d:/proj-a",
            "/a/b",
            "/x",
            r"\\server\share\x",
        ):
            assert _is_absolute_any_platform(ok) is True, ok

    def test_rejects_relative_and_drive_relative_paths(self):
        from mem_lake.gateway.tools.manage_tools import _is_absolute_any_platform

        for bad in ("C:foo", "proj-a", "./x", "../x", ""):
            assert _is_absolute_any_platform(bad) is False, bad

    def test_work_dir_is_required(self):
        """work_dir 缺失 → Pydantic 必填报错。"""
        import pydantic

        from mem_lake.gateway.tools.manage_tools import ProjectProfileInput

        with pytest.raises(pydantic.ValidationError):
            ProjectProfileInput(
                title="t",
                content="c",
                properties={
                    "name": "n",
                    "description": "d",
                    "tech_stack": [],
                    "architecture": "a",
                },
            )

    def test_profile_properties_writes_work_dir_unconditionally(self):
        from mem_lake.gateway.tools.manage_tools import (
            ProjectProfileInput,
            _profile_properties,
        )

        p = ProjectProfileInput(
            title="t",
            content="c",
            properties={
                "name": "n",
                "description": "d",
                "tech_stack": [],
                "architecture": "a",
            },
            work_dir="/abs/path",
        )
        assert _profile_properties(p)["work_dir"] == "/abs/path"


class TestProjectMustHaveProfile:
    """堵幽灵项目入口：项目 UUID 必须先经 manage_project_profile 建画像。"""

    @pytest.mark.asyncio
    async def test_rejects_projects_without_profile(self, monkeypatch):
        from mem_lake.gateway.tools import manage_tools

        async def fake_ids(session, *, project_ids):
            # 库里没有任何画像
            return set()

        monkeypatch.setattr(manage_tools, "list_project_ids_with_profile", fake_ids)

        with pytest.raises(PayloadValidationError, match="尚未创建画像"):
            await manage_tools._assert_projects_have_profile(None, [uuid.uuid4()])

    @pytest.mark.asyncio
    async def test_accepts_projects_with_profile(self, monkeypatch):
        from mem_lake.gateway.tools import manage_tools

        p_ok = uuid.uuid4()

        async def fake_ids(session, *, project_ids):
            return set(project_ids)

        monkeypatch.setattr(manage_tools, "list_project_ids_with_profile", fake_ids)

        # 不抛异常
        await manage_tools._assert_projects_have_profile(None, [p_ok])

    @pytest.mark.asyncio
    async def test_empty_project_ids_passes(self, monkeypatch):
        from mem_lake.gateway.tools import manage_tools

        # 空集合（admin project_scope=[]）不触发校验
        await manage_tools._assert_projects_have_profile(None, [])

    @pytest.mark.asyncio
    async def test_error_message_lists_missing_uuids(self, monkeypatch):
        from mem_lake.gateway.tools import manage_tools

        p_missing = uuid.uuid4()

        async def fake_ids(session, *, project_ids):
            return set()

        monkeypatch.setattr(manage_tools, "list_project_ids_with_profile", fake_ids)

        with pytest.raises(PayloadValidationError) as exc:
            await manage_tools._assert_projects_have_profile(None, [p_missing])

        msg = str(exc.value)
        assert str(p_missing) in msg
        assert "manage_project_profile" in msg

    @pytest.mark.asyncio
    async def test_does_not_use_limit_capped_profile_list(self, monkeypatch):
        """回归：存在性判定不得再走「全局 LIMIT + created_at 倒序」的列表查询。

        list_project_profiles 的 limit 是全局的：某项目画像过多时会把其他项目的
        画像挤出结果集，导致已建档项目被误判为「尚未建档」（fail-closed 的误拒）。
        """
        from mem_lake.gateway.tools import manage_tools

        async def boom(*_args, **_kwargs):
            raise AssertionError("不应调用 list_project_profiles 做存在性判定")

        async def fake_ids(session, *, project_ids):
            return set(project_ids)

        monkeypatch.setattr(manage_tools, "list_project_profiles", boom, raising=False)
        monkeypatch.setattr(manage_tools, "list_project_ids_with_profile", fake_ids)

        await manage_tools._assert_projects_have_profile(None, [uuid.uuid4()])



def test_user_hint_schema_points_to_mcp_resource():
    from mem_lake.gateway.tools.manage_tools import CreateAccessKeyOutput

    description = CreateAccessKeyOutput.model_fields["user_hint"].description

    assert "memlake://skills/" in description
    assert "get_skill" in description
    assert "GitHub" not in description
