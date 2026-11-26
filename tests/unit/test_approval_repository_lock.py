"""approval 批次行锁测试（无 DB）。"""

import uuid
from types import SimpleNamespace

import pytest

from mem_lake.approval import repository


class _Result:
    def __init__(self, batch):
        self._batch = batch

    def scalar_one_or_none(self):
        return self._batch


class _Session:
    def __init__(self, batch):
        self.batch = batch
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return _Result(self.batch)


@pytest.mark.asyncio
async def test_get_batch_detail_for_update_sets_row_lock():
    batch = SimpleNamespace(id=uuid.uuid4(), items=[])
    session = _Session(batch)

    await repository.get_batch_detail(session, batch.id, for_update=True)

    assert session.stmt._for_update_arg is not None


@pytest.mark.asyncio
async def test_get_batch_detail_default_has_no_row_lock():
    batch = SimpleNamespace(id=uuid.uuid4(), items=[])
    session = _Session(batch)

    await repository.get_batch_detail(session, batch.id)

    assert session.stmt._for_update_arg is None
