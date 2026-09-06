"""ingest 单测：resolve_system 解析（纯逻辑，无需 DB）。

FIX-06 之后本文档仅保留不依赖 DB 的纯逻辑用例：
- resolve_system：name/code 解析 System（通过 fake session / monkeypatch 模拟）。
依赖真实数据库/AGE 的 run_import_batch 批量用例已整体迁至
tests/integration/test_cli_ingest.py（见 FIX-06 单测分层）。
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.cli.ingest import resolve_system


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_resolve_system_missing_raises(monkeypatch):
    """name/code 均未命中时抛 ValueError。"""
    src = iter([_FakeResult(None), _FakeResult(None)])

    async def fake_execute(stmt):
        return next(src)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    with pytest.raises(ValueError):
        await resolve_system(session, name="NOPE", code="NOPE")


@pytest.mark.asyncio
async def test_resolve_system_found(monkeypatch):
    """按 code 命中 System。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="HIS", code="HIS")
    src = iter([_FakeResult(sys_obj)])

    async def fake_execute(stmt):
        return next(src)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    got = await resolve_system(session, code="HIS")
    assert got is sys_obj


@pytest.mark.asyncio
async def test_resolve_system_by_name(monkeypatch):
    """未传 code、只按 name 也能解析（DB system code 为 NULL 的场景）。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="中方诊药云系统", code=None)

    async def fake_execute(stmt):
        return _FakeResult(sys_obj)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    got = await resolve_system(session, name="中方诊药云系统")
    assert got is sys_obj


@pytest.mark.asyncio
async def test_resolve_system_falls_back_to_code(monkeypatch):
    """name 未命中时回退 code。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="HIS", code="HIS")
    src = iter([_FakeResult(None), _FakeResult(sys_obj)])

    async def fake_execute(stmt):
        return next(src)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)
    got = await resolve_system(session, code="HIS", name="NOPE")
    assert got is sys_obj
