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


def test_build_scope_meta_admin():
    m = _build_scope_meta(True, [], [1, 2, 3])
    assert m.scope_type == "all"
    assert m.visible_uuids == []
    assert m.visible_count == 3


def test_build_scope_meta_scoped():
    m = _build_scope_meta(False, ["p1", "p2"], [1])
    assert m.scope_type == "scoped"
    assert m.visible_uuids == ["p1", "p2"]
    assert m.visible_count == 2


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
