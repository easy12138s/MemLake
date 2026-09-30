"""get_project_info 单元测试：核心逻辑与辅助函数（无需 DB / FastMCP 上下文）。"""

import uuid
from datetime import datetime, timezone

import pytest
from fastmcp.exceptions import ToolError

from mem_lake.gateway.tools.query_tools import (
    ScopeMeta,
    _build_scope_meta,
    _get_project_info_core,
    _to_project_info,
)


class FakeNode:
    def __init__(self, project_id, title, content, properties=None, tags=None,
                 created_at=None, node_id=None):
        self.project_id = project_id
        self.title = title
        self.content = content
        self.properties = properties or {}
        self.tags = tags or []
        self.created_at = created_at or datetime.now(timezone.utc)
        self.id = node_id or uuid.uuid4()


def fake_list(nodes):
    async def _fn(**kw):
        pids = kw.get("project_ids")
        if pids is None:
            return list(nodes)
        return [n for n in nodes if n.project_id in set(pids)]

    return _fn


def test_to_project_info_basic():
    n = FakeNode(uuid.uuid4(), "Proj", "desc", {"work_dir": "/a", "repo": "r"}, ["t"])
    info = _to_project_info(n)
    assert info.name == "Proj"
    assert info.work_dir == "/a"
    assert info.repo == "r"
    assert info.description == "desc"
    assert info.tags == ["t"]
    assert info.profile is None


def test_to_project_info_include_profile():
    n = FakeNode(uuid.uuid4(), "Proj", "desc", {"work_dir": "/a"})
    info = _to_project_info(n, include_profile=True)
    assert info.profile == {"work_dir": "/a"}


def test_to_project_info_name_from_properties():
    """name 优先取 properties.name，缺省回退 node.title。"""
    n = FakeNode(
        uuid.uuid4(),
        "TitleFallback",
        "desc",
        {"name": "BizName", "work_dir": "/a"},
    )
    info = _to_project_info(n)
    assert info.name == "BizName"


def test_to_project_info_name_fallback_to_title():
    """properties 无 name 时回退 node.title。"""
    n = FakeNode(uuid.uuid4(), "OnlyTitle", "desc", {"work_dir": "/a"})
    info = _to_project_info(n)
    assert info.name == "OnlyTitle"


def test_build_scope_meta_admin():
    m = _build_scope_meta(True, [], [1, 2, 3], system_scope=[], systems=[])
    assert m.scope_type == "all"
    assert m.visible_uuids == []
    assert m.visible_count == 3


class FakeSystem:
    def __init__(self, name, code=None):
        self.id = uuid.uuid4()
        self.name = name
        self.code = code


def test_build_scope_meta_admin_includes_all_systems():
    """admin 的 scope_meta 含 system 维度：system_scope_type=all，列出全部 System。"""
    s1, s2 = FakeSystem("系统甲", "SA"), FakeSystem("系统乙")
    m = _build_scope_meta(True, [], [1], system_scope=[], systems=[s1, s2])
    assert m.system_scope_type == "all"
    assert m.visible_system_count == 2
    assert [s.name for s in m.visible_systems] == ["系统甲", "系统乙"]
    assert m.visible_systems[0].system_id == str(s1.id)
    assert m.visible_systems[0].code == "SA"
    assert m.visible_systems[1].code is None


def test_build_scope_meta_scoped():
    m = _build_scope_meta(False, ["p1", "p2"], [1], system_scope=[], systems=[])
    assert m.scope_type == "scoped"
    assert m.visible_uuids == ["p1", "p2"]
    assert m.visible_count == 2
    assert m.system_scope_type == "scoped"
    assert m.visible_system_count == 0


def test_build_scope_meta_scoped_includes_bound_systems():
    """scoped key 的 scope_meta 列出其绑定的 system（dev/pm 自查入口）。"""
    s1 = FakeSystem("中方系统", "ZH")
    m = _build_scope_meta(
        False, ["p1"], [1],
        system_scope=[str(s1.id)], systems=[s1],
    )
    assert m.system_scope_type == "scoped"
    assert m.visible_system_count == 1
    assert m.visible_systems[0].system_id == str(s1.id)
    assert m.visible_systems[0].name == "中方系统"


def test_build_scope_meta_scoped_filters_systems_to_scope():
    """scoped key 只列出 claims 内的 system，越权项不进 visible_systems。"""
    s_in = FakeSystem("在权限内")
    s_out = FakeSystem("不在权限内")
    m = _build_scope_meta(
        False, [], [1],
        system_scope=[str(s_in.id)], systems=[s_in, s_out],
    )
    assert m.visible_system_count == 1
    assert m.visible_systems[0].name == "在权限内"


async def test_core_list_admin_all():
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    nodes = [FakeNode(p1, "A", "da"), FakeNode(p2, "B", "db")]
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=False,
        role="admin", scope=[], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    assert out.action == "list"
    assert {i.project_id for i in out.projects} == {p1, p2}


async def test_core_list_scoped_filters():
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    nodes = [FakeNode(p1, "A", "da"), FakeNode(p2, "B", "db")]
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=False,
        role="dev", scope=[str(p1)], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    assert [i.project_id for i in out.projects] == [p1]


async def test_core_list_dedup_latest():
    p = uuid.uuid4()
    older = FakeNode(p, "old", "o", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    newer = FakeNode(p, "new", "n", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=False,
        role="admin", scope=[], list_fn=fake_list([newer, older]), validate_fn=lambda x: None,
    )
    assert len(out.projects) == 1
    assert out.projects[0].name == "new"


async def test_core_list_includes_placeholder_for_scope_without_profile():
    """ISSUE-07：scope 内但未建 ProjectProfile 的项目返回占位条目（name=None），不再消失。

    批次六（报告 P1-4）：占位条目带 has_profile=False 标记 + 输出级 hint
    引导「admin 可用 manage_project_profile 补建」——Agent 不再只能靠猜。
    """
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    nodes = [FakeNode(p1, "A", "da")]  # 仅 p1 有画像
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=False,
        role="dev", scope=[str(p1), str(p2)], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    by_id = {i.project_id: i for i in out.projects}
    assert p1 in by_id and by_id[p1].name == "A"
    assert p2 in by_id, "scope 内无画像的项目应返回占位条目而非消失"
    assert by_id[p2].name is None
    # 批次六：画像标记 + 输出引导
    assert by_id[p1].has_profile is True
    assert by_id[p2].has_profile is False
    assert out.hint and "manage_project_profile" in out.hint


async def test_core_list_all_profiled_no_hint():
    """全部项目有画像：不产生误导性 hint。"""
    p1 = uuid.uuid4()
    nodes = [FakeNode(p1, "A", "da")]
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=False,
        role="dev", scope=[str(p1)], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    assert out.hint is None


async def test_core_list_admin_no_placeholder_leak():
    """admin 无 scope 概念：不虚构占位条目，仅列实际存在的画像。"""
    p1 = uuid.uuid4()
    nodes = [FakeNode(p1, "A", "da")]
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=False,
        role="admin", scope=[], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    assert [i.project_id for i in out.projects] == [p1]


async def test_core_get_success():
    p = uuid.uuid4()
    nodes = [FakeNode(p, "A", "da", {"work_dir": "/x"})]
    out = await _get_project_info_core(
        action="get", project_id=p, include_profile=True, include_scope_meta=False,
        role="admin", scope=[], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    assert out.action == "get"
    assert out.project is not None
    assert out.project.project_id == p
    assert out.project.work_dir == "/x"
    assert out.project.profile == {"work_dir": "/x"}


async def test_core_get_missing_returns_none():
    out = await _get_project_info_core(
        action="get", project_id=uuid.uuid4(), include_profile=False, include_scope_meta=False,
        role="admin", scope=[], list_fn=fake_list([]), validate_fn=lambda x: None,
    )
    assert out.project is None


async def test_core_get_requires_project_id():
    with pytest.raises(ValueError):
        await _get_project_info_core(
            action="get", project_id=None, include_profile=False, include_scope_meta=False,
            role="admin", scope=[], list_fn=fake_list([]), validate_fn=lambda x: None,
        )


async def test_core_get_forbidden_raises():
    def _validate(pid):
        raise ToolError("denied")

    with pytest.raises(ToolError):
        await _get_project_info_core(
            action="get", project_id=uuid.uuid4(), include_profile=False,
            include_scope_meta=False, role="dev", scope=[],
            list_fn=fake_list([]), validate_fn=_validate,
        )


async def test_core_list_include_scope_meta():
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    nodes = [FakeNode(p1, "A", "da"), FakeNode(p2, "B", "db")]
    out = await _get_project_info_core(
        action="list", project_id=None, include_profile=False, include_scope_meta=True,
        role="admin", scope=[], list_fn=fake_list(nodes), validate_fn=lambda x: None,
    )
    assert isinstance(out.scope, ScopeMeta)
    assert out.scope.scope_type == "all"
    assert out.scope.visible_uuids == []
    assert out.scope.visible_count == 2
