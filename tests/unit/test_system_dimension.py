"""system 维度（阶段 A）单元测试：悬浮需求构造 + create_node 归属强约束。

无需真实 DB：create_node 的归属强约束在触库前抛错，可传假 session 验证；
_build_publish_items / build_node_item 为纯函数可直接测。
"""

import uuid

import pytest

from mem_lake.approval.service import PayloadValidationError
from mem_lake.gateway.tools._shared import build_node_item
from mem_lake.gateway.tools.write_tools import RelatedInput, RequirementInput, _build_publish_items
from mem_lake.knowledge.repository import create_node
from mem_lake.knowledge.schema import SchemaValidationError


def _req_input(**kw) -> RequirementInput:
    base = dict(
        title="悬浮需求",
        content="先于实现，跨项目落地",
        properties={"priority": "P0", "module": "sys"},
        tags=[],
    )
    base.update(kw)
    return RequirementInput(**base)


def test_build_publish_items_floating():
    """project_id=None（悬浮）时 node item 带 system_id、project_id 为空。"""
    system_id = uuid.uuid4()
    items = _build_publish_items(
        None, _req_input(properties={"priority": "P0", "module": "m"}), None, "ak", system_id
    )
    assert len(items) == 1
    payload = items[0]["payload"]
    assert items[0]["entity_type"] == "Requirement"
    assert payload["ref"] == "requirement"
    assert payload["project_id"] is None  # 悬浮：无 project
    assert payload["system_id"] == str(system_id)


def test_build_publish_items_with_project_and_system():
    """project_id 与 system_id 同时存在（需求归属某 project，但仍属 system）。"""
    project_id = uuid.uuid4()
    system_id = uuid.uuid4()
    items = _build_publish_items(
        project_id, _req_input(properties={"priority": "P0", "module": "m"}), None, "ak", system_id
    )
    payload = items[0]["payload"]
    assert payload["project_id"] == str(project_id)
    assert payload["system_id"] == str(system_id)


def test_build_publish_items_related_with_system():
    """悬浮 + related 仍构造 node + 边（system_id 透传）。"""
    system_id = uuid.uuid4()
    related = RelatedInput(relates_to=["REQ-OLD"])
    items = _build_publish_items(
        None, _req_input(properties={"priority": "P0", "module": "m"}), related, "ak", system_id
    )
    assert len(items) == 2
    assert items[0]["payload"]["system_id"] == str(system_id)
    assert items[0]["payload"]["project_id"] is None
    assert items[1]["item_type"] == "edge"


def test_build_node_item_requirement_floating():
    """Requirement 悬浮：project_id=None 合法（需 system_id）。"""
    item = build_node_item(
        ref="requirement", node_type="Requirement", title="t", content="c",
        properties={"priority": "P0", "module": "m"},
        project_id=None, system_id=uuid.uuid4(), created_by="ak",
    )
    assert item["payload"]["project_id"] is None
    assert "system_id" in item["payload"]


def test_build_node_item_requirement_missing_system_rejected():
    """Requirement 缺 system_id 抛错。"""
    with pytest.raises(PayloadValidationError, match="必须归属 system"):
        build_node_item(
            ref="req", node_type="Requirement", title="t", content="c",
            properties={"priority": "P0", "module": "m"},
            project_id=uuid.uuid4(), system_id=None, created_by="ak",
        )


def test_build_node_item_asset_missing_project_rejected():
    """非 Requirement 资产缺 project_id 抛错。"""
    with pytest.raises(PayloadValidationError, match="必须归属 project"):
        build_node_item(
            ref="req", node_type="CodeSnippet", title="t", content="c",
            properties={"name": "n", "type": "class", "responsibility": "r", "file_path": "f"},
            project_id=None, created_by="ak",
        )


@pytest.mark.asyncio
async def test_create_node_requirement_missing_system():
    """create_node：Requirement 缺 system_id 抛 SchemaValidationError（触库前）。"""
    with pytest.raises(SchemaValidationError, match="必须归属 system"):
        await create_node(
            session=None,  # type: ignore  # 未触库
            graph_store=None,  # type: ignore
            embedding_client=None,
            project_id=uuid.uuid4(),
            node_type="Requirement",
            title="t", content="c",
            properties={"priority": "P0", "module": "m"},
            created_by="ak",
            system_id=None,
        )


@pytest.mark.asyncio
async def test_create_node_asset_missing_project():
    """create_node：非 Requirement 缺 project_id 抛 SchemaValidationError（触库前）。"""
    with pytest.raises(SchemaValidationError, match="必须归属 project"):
        await create_node(
            session=None,  # type: ignore  # 未触库
            graph_store=None,  # type: ignore
            embedding_client=None,
            project_id=None,
            node_type="CodeSnippet",
            title="t", content="c",
            properties={"name": "n", "type": "class", "responsibility": "r", "file_path": "f"},
            created_by="ak",
        )


# ============================================================================
# 阶段 B：scope 两级 + 可见性 + FilterSpec
# ============================================================================


class _Node:
    """最小 Requirement 节点（project_id/system_id 可空）。"""

    def __init__(self, project_id=None, system_id=None) -> None:
        self.project_id = project_id
        self.system_id = system_id


class _FakeToken:
    def __init__(self, claims) -> None:
        self.claims = claims
        self.scopes = []
        self.client_id = "client"


def test_norm_scope_dict_and_non_dict_rejected():
    """_norm_scope：dict 原样归一化；非 dict（如扁平列表）抛 ValueError。"""
    from mem_lake.auth.service import _norm_scope

    scope = _norm_scope({"systems": ["s1"], "projects": ["p1", "p2"]})
    assert scope["systems"] == ["s1"]
    assert scope["projects"] == ["p1", "p2"]

    with pytest.raises(ValueError, match="project_scope 必须是"):
        _norm_scope(["p1", "p2"])


def test_get_current_system_scope(monkeypatch):
    """get_current_system_scope 读取 claims.system_scope，缺省空。"""
    from mem_lake.gateway.dependencies import get_current_system_scope

    monkeypatch.setattr(
        "mem_lake.gateway.dependencies.get_access_token",
        lambda: _FakeToken({"system_scope": ["s1"]}),
    )
    assert get_current_system_scope() == ["s1"]


def test_validate_system_access(monkeypatch):
    """validate_system_access：admin 豁免；pm/dev 越权拒绝。"""
    from fastmcp.exceptions import ToolError

    from mem_lake.gateway.dependencies import validate_system_access

    sid = uuid.uuid4()
    monkeypatch.setattr(
        "mem_lake.gateway.dependencies.get_access_token",
        lambda: _FakeToken({"role": "admin"}),
    )
    validate_system_access(sid)  # admin 不抛

    monkeypatch.setattr(
        "mem_lake.gateway.dependencies.get_access_token",
        lambda: _FakeToken({"role": "pm", "system_scope": [str(uuid.uuid4())]}),
    )
    with pytest.raises(ToolError, match="不在当前 Access Key 的 system 范围内"):
        validate_system_access(sid)


def test_is_requirement_visible_admin_and_project():
    """is_requirement_visible：admin 恒可见；project 命中可见。"""
    from mem_lake.gateway.access import is_requirement_visible

    async def run():
        # admin
        assert await is_requirement_visible(
            None, req_node=_Node(), role="admin", project_scope=[], system_scope=[]
        ) is True
        pid = str(uuid.uuid4())
        # pm 命中 project
        assert await is_requirement_visible(
            None, req_node=_Node(project_id=uuid.UUID(pid)), role="pm",
            project_scope=[pid], system_scope=[],
        ) is True
        # pm 未命中 project/system → 不可见
        assert await is_requirement_visible(
            None, req_node=_Node(project_id=uuid.UUID(pid)), role="pm",
            project_scope=["other"], system_scope=[],
        ) is False

    import asyncio

    asyncio.run(run())


def test_is_requirement_visible_system_scope():
    """is_requirement_visible：system_id ∈ system_scope 可见（pm/dev）。"""
    from mem_lake.gateway.access import is_requirement_visible

    sid = uuid.uuid4()

    async def run():
        assert await is_requirement_visible(
            None, req_node=_Node(system_id=sid), role="pm",
            project_scope=[], system_scope=[str(sid)],
        ) is True

    import asyncio

    asyncio.run(run())


def test_filter_spec_compiles_system_and_multi_project():
    """FilterSpec 编译 system_id 与 project_ids 为对应列条件。"""
    from mem_lake.search.filters import FilterSpec, compile_sqlalchemy

    spec = FilterSpec(
        system_id=uuid.uuid4(),
        project_ids=(uuid.uuid4(), uuid.uuid4()),
        node_types=("Requirement",),
    )
    clauses = compile_sqlalchemy(spec)
    assert clauses
    text_all = " ".join(str(c) for c in clauses)
    assert "system_id" in text_all
    assert "project_id" in text_all
