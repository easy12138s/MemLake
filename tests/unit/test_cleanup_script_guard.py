"""清理脚本守卫的单元测试（纯逻辑，不连 DB）。

背景：2026-11-20 清理脚本漏列「保留 system 的关联项目」，按
（system∉保留 AND project∉保留）把三个真实项目的全部 project-scoped
节点连锅端（真实研发资产 64 个 / 111 条边）。守卫改为：system↔project
映射里的项目默认受保护，只有 --drop-project 显式点名才放开。

对应实现在 scripts/cleanup_test_leftovers.py::_effective_protected_projects。
"""

from __future__ import annotations

import uuid

from scripts.cleanup_test_leftovers import (
    _effective_protected_projects,
    _node_delete_cond,
)


def _u(s: str) -> uuid.UUID:
    return uuid.UUID(s)


# 11-20 事故现场：保留 system「中方诊药云系统」关联 4 个真实项目，
# 当次清理只显式列了其中 1 个。
SYS_PROJECTS = {
    _u("01276605-8670-4f6f-82bd-fbe0b03a9358"),  # 门诊服务
    _u("0abd0149-e51c-4594-8387-77513d1533fb"),  # 医保绑定域
    _u("6d50c0c9-43ba-4b8f-9bac-16fa1155714b"),  # 需求关系域
    _u("70e9a3e0-0b51-420b-ad0e-b336bbc33736"),  # 商城
}
KEEP_EXPLICIT = {_u("0abd0149-e51c-4594-8387-77513d1533fb")}


def test_system_projects_protected_by_default() -> None:
    """事故参数下，未显式 --keep-project 的 system 关联项目仍受保护。"""
    protected = _effective_protected_projects(KEEP_EXPLICIT, SYS_PROJECTS, set())

    assert protected == SYS_PROJECTS
    # 事故当日被连锅端的三个项目，现在都在保护集里
    for pid in (
        _u("01276605-8670-4f6f-82bd-fbe0b03a9358"),
        _u("6d50c0c9-43ba-4b8f-9bac-16fa1155714b"),
        _u("70e9a3e0-0b51-420b-ad0e-b336bbc33736"),
    ):
        assert pid in protected


def test_drop_project_releases_only_that_project() -> None:
    """--drop-project 只放开被点名的那个 system 关联项目。"""
    target = _u("70e9a3e0-0b51-420b-ad0e-b336bbc33736")
    protected = _effective_protected_projects(KEEP_EXPLICIT, SYS_PROJECTS, {target})

    assert target not in protected
    assert SYS_PROJECTS - {target} <= protected


def test_keep_project_wins_over_drop_project() -> None:
    """矛盾输入（同时 keep 与 drop）取安全侧：仍然保留。"""
    target = _u("0abd0149-e51c-4594-8387-77513d1533fb")
    protected = _effective_protected_projects(
        {target}, SYS_PROJECTS, {target}
    )

    assert target in protected


def test_keep_project_protected_even_if_not_system_mapped() -> None:
    """显式 --keep-project 的项目（不在 system 映射里）同样受保护。"""
    extra = _u("7e81667e-2f89-4fe8-8645-1c6e03d423b8")
    protected = _effective_protected_projects({extra}, SYS_PROJECTS, set())

    assert extra in protected


def test_delete_condition_excludes_protected_projects() -> None:
    """删除谓词对「受保护项目」恒为假（节点不会被判入待删集）。"""
    from sqlalchemy.dialects import postgresql

    keep_system_ids = {uuid.uuid4(): "sys"}
    protected = _effective_protected_projects(KEEP_EXPLICIT, SYS_PROJECTS, set())
    cond = _node_delete_cond(keep_system_ids, protected)
    sql = str(
        cond.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    # 保护集里的项目逐一出现在 NOT IN 子句中
    for pid in protected:
        assert str(pid) in sql
    assert "knowledge_node.project_id NOT IN" in sql
