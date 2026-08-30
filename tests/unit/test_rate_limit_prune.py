"""限流中间件内存治理（AUDIT §2.9）单测：定期清扫 + 容量上限逐出。

纯单测，无 DB 依赖。直接验证 RateLimitMiddleware._prune 与容量上限逐出逻辑。
"""

import time
from types import SimpleNamespace

import pytest

from mem_lake.gateway.middleware import (
    RATE_LIMIT_WINDOW_SEC,
    RateLimitMiddleware,
)


def _token(key_id: str):
    return SimpleNamespace(client_id=key_id)


class _Context:
    """最小 MiddlewareContext：提供 .message.name 供 on_call_tool 读取。"""

    def __init__(self, tool_name="t"):
        self.message = SimpleNamespace(name=tool_name)


async def _noop_next(context):  # noqa: ARG001
    return "ok"


def _make_mw(monkeypatch, max_buckets=None, prune_interval=None):
    mw = RateLimitMiddleware()
    if max_buckets is not None:
        mw._max_buckets = max_buckets
    if prune_interval is not None:
        mw._prune_interval = prune_interval
    return mw


def test_prune_removes_expired_buckets():
    mw = RateLimitMiddleware()
    now = time.time()
    # key-a 窗口内活跃，key-b 已过期
    mw._buckets["key-a"].append(now)
    mw._buckets["key-b"].append(now - RATE_LIMIT_WINDOW_SEC - 1)
    mw._prune(now)
    assert "key-a" in mw._buckets
    assert "key-b" not in mw._buckets


@pytest.mark.asyncio
async def test_capacity_evicts_least_active(monkeypatch):
    mw = _make_mw(monkeypatch, max_buckets=2)
    now = time.time()
    # 已满：key-1 左边界较早（较不活跃），key-2 较新
    mw._buckets["key-1"].append(now - 0.5)
    mw._buckets["key-2"].append(now)
    assert len(mw._buckets) == 2

    # 新增 key-3（未认证检查走通过，需要 access_token 非 None）
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token", lambda: _token("key-3")
    )
    result = await mw.on_call_tool(_Context(), _noop_next)
    assert result == "ok"
    # 容量上限触发清扫 + 逐出最不活跃（key-1），并加入 key-3
    assert "key-1" not in mw._buckets
    assert "key-2" in mw._buckets
    assert "key-3" in mw._buckets


@pytest.mark.asyncio
async def test_periodic_prune_runs(monkeypatch):
    mw = _make_mw(monkeypatch, prune_interval=3)
    monkeypatch.setattr(
        "mem_lake.gateway.middleware.get_access_token", lambda: _token("active")
    )
    now = time.time()
    # 预置一个已过期桶（仅出现在全局桶，不当前 key）
    mw._buckets["inactive"].append(now - RATE_LIMIT_WINDOW_SEC - 1)

    # 连续调用触发定期清扫
    for _ in range(3):
        await mw.on_call_tool(_Context(), _noop_next)
    assert "inactive" not in mw._buckets
    assert "active" in mw._buckets
