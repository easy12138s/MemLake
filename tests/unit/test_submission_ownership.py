"""submission_id 归属校验测试（无 DB）。"""

import uuid
from contextlib import asynccontextmanager

import pytest

from mem_lake.approval.service import PayloadValidationError
from mem_lake.gateway.tools import write_tools
from mem_lake.gateway.tools.write_tools import _assert_submission_ownership


def _session_with_owner(owner: str | None):
    class _Result:
        def scalar_one_or_none(self):
            return owner

    class _Session:
        async def execute(self, stmt):
            return _Result()

    @asynccontextmanager
    async def _ctx():
        yield _Session()

    return _ctx


@pytest.mark.asyncio
async def test_submission_owner_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(
        write_tools, "readonly_session", _session_with_owner("ak_other")
    )

    with pytest.raises(PayloadValidationError, match="其他提交者"):
        await _assert_submission_ownership(uuid.uuid4(), "ak_me")


@pytest.mark.asyncio
async def test_submission_same_owner_or_new_is_allowed(monkeypatch):
    monkeypatch.setattr(write_tools, "readonly_session", _session_with_owner("ak_me"))
    await _assert_submission_ownership(uuid.uuid4(), "ak_me")

    monkeypatch.setattr(write_tools, "readonly_session", _session_with_owner(None))
    await _assert_submission_ownership(uuid.uuid4(), "ak_me")
