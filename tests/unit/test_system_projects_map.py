"""system→projects 批量映射测试（无 DB）。"""

import uuid

import pytest

from mem_lake.knowledge import repository


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def execute(self, stmt):
        self.calls += 1
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_get_system_projects_map_uses_single_query():
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    session = _Session([(s1, p1), (s1, p2), (s2, p1)])

    mapping = await repository.get_system_projects_map(
        session, system_ids=[s1, s2]
    )

    assert session.calls == 1
    assert mapping == {s1: [p1, p2], s2: [p1]}


@pytest.mark.asyncio
async def test_get_system_projects_map_empty_input_skips_query():
    session = _Session([])

    assert await repository.get_system_projects_map(session, system_ids=[]) == {}
    assert session.calls == 0
