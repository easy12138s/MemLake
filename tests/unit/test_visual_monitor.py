"""监控模块测试：文本解析、分位数、环形缓冲、序列派生与三端点（httpx ASGI 进程内）。"""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from mem_lake.config import Settings
from mem_lake.visual import monitor_api
from mem_lake.visual.app import _lifespan, create_visual_app
from mem_lake.visual.monitor import (
    HistogramData,
    MetricsSampler,
    ParsedMetrics,
    build_sample,
    build_snapshot,
    extract_series,
    histogram_quantile,
    merge_histograms,
    parse_prom_text,
    sum_counter,
)

# ============================== 解析与分位数 ==============================


def _pm_with_tool_metrics() -> ParsedMetrics:
    """构造带网关工具指标的解析结果（2 成功 1 失败，p50 落 0.01~0.02 桶）。"""
    text = "\n".join(
        [
            "# HELP memlake_mcp_tool_calls_total tool calls",
            "# TYPE memlake_mcp_tool_calls_total counter",
            'memlake_mcp_tool_calls_total{status="success",tool="search"} 2.0',
            'memlake_mcp_tool_calls_total{status="error",tool="search"} 1.0',
            'memlake_mcp_tool_duration_seconds_bucket{le="0.005",tool="search"} 0',
            'memlake_mcp_tool_duration_seconds_bucket{le="0.01",tool="search"} 1',
            'memlake_mcp_tool_duration_seconds_bucket{le="0.02",tool="search"} 2',
            'memlake_mcp_tool_duration_seconds_bucket{le="+Inf",tool="search"} 3',
            'memlake_mcp_tool_duration_seconds_sum{tool="search"} 0.045',
            'memlake_mcp_tool_duration_seconds_count{tool="search"} 3',
        ]
    )
    return parse_prom_text(text)


def test_parse_counters_and_labels():
    pm = _pm_with_tool_metrics()
    assert sum_counter(pm, "memlake_mcp_tool_calls_total", status="success") == 2.0
    assert sum_counter(pm, "memlake_mcp_tool_calls_total", status="error") == 1.0
    # 不存在的指标：零计数不报错
    assert sum_counter(pm, "memlake_not_exists_total") == 0.0


def test_parse_label_value_with_comma_and_escape():
    text = 'some_total{k="a,b",k2="x\\"y"} 1\n'
    pm = parse_prom_text(text)
    assert sum_counter(pm, "some_total") == 1.0
    labels = dict(pm.counters["some_total"][0][0])
    assert labels["k"] == "a,b"
    assert labels["k2"] == 'x"y'


def test_parse_skips_comments_gauges_and_bad_values():
    text = "\n".join(
        [
            "# TYPE python_info gauge",
            'python_info{version="3.11"} 1',
            "process_cpu_seconds_total nan",
            "bad_line_no_value",
        ]
    )
    pm = parse_prom_text(text)
    assert not pm.counters
    assert not pm.histograms


def test_histogram_quantile_interpolation():
    h = HistogramData(
        buckets=[(0.005, 0.0), (0.01, 1.0), (0.02, 2.0), (float("inf"), 3.0)],
        sample_count=3.0,
    )
    # rank=1.5 落 (0.01, 0.02) 桶内：0.01 + 0.5*(0.02-0.01)
    assert histogram_quantile(h, 0.5) == pytest.approx(0.015)


def test_histogram_quantile_edge_cases():
    # 零计数 → None
    assert histogram_quantile(HistogramData(sample_count=0.0), 0.5) is None
    # 空桶 → None
    assert histogram_quantile(HistogramData(), 0.5) is None
    # 分位落入 +Inf 桶 → None（无上界）
    h = HistogramData(buckets=[(0.01, 1.0), (0.02, 2.0), (float("inf"), 3.0)], sample_count=3.0)
    assert histogram_quantile(h, 0.99) is None
    # 缺失上界桶且 q 超出全部有限桶 → None
    h_no_inf = HistogramData(buckets=[(0.01, 1.0), (0.02, 2.0)], sample_count=3.0)
    assert histogram_quantile(h_no_inf, 0.99) is None
    # 缺失上界桶但 q 在有限桶内 → 正常估计
    assert histogram_quantile(h_no_inf, 0.5) == pytest.approx(0.015)


def test_merge_histograms_sums_buckets():
    a = HistogramData(buckets=[(0.01, 1.0), (0.02, 2.0)], sample_count=2.0, sample_sum=0.03)
    b = HistogramData(buckets=[(0.01, 2.0), (0.02, 2.0)], sample_count=2.0, sample_sum=0.05)
    merged = merge_histograms([a, b])
    assert merged.buckets == [(0.01, 3.0), (0.02, 4.0)]
    assert merged.sample_count == 4.0
    assert merged.sample_sum == pytest.approx(0.08)
    assert merge_histograms([]).sample_count == 0.0


# ============================== 样本与快照 ==============================


def test_build_sample_with_and_without_embed_service():
    gw = _pm_with_tool_metrics()
    sample = build_sample(gw, None, now=1000.0)
    assert sample["ts"] == 1000.0
    assert sample["gw_tool_success"] == 2.0
    assert sample["gw_tool_error"] == 1.0
    # count=3、rank=1.5 落 0.01~0.02 桶 → 15ms
    assert sample["gw_tool_p50_ms"] == pytest.approx(15.0)
    # embedding 不可达：es_* 键整体缺失
    assert not [k for k in sample if k.startswith("es_")]

    es = parse_prom_text(
        "\n".join(
            [
                "memlake_embedding_embed_calls_total 5",
                "memlake_embedding_rerank_calls_total 2",
                'memlake_embedding_endpoint_duration_seconds_bucket{le="0.25",op="embed"} 5',
                'memlake_embedding_endpoint_duration_seconds_bucket{le="+Inf",op="embed"} 5',
                'memlake_embedding_endpoint_duration_seconds_count{op="embed"} 5',
            ]
        )
    )
    sample2 = build_sample(gw, es, now=1000.0)
    assert sample2["es_embed_calls"] == 5.0
    assert sample2["es_rerank_calls"] == 2.0
    # 全部落 0.25 桶内 → p50 介于 0 与 0.25 之间
    assert 0 < sample2["es_embed_p50_ms"] < 250


def test_build_snapshot_shape_and_degraded():
    gw = _pm_with_tool_metrics()
    snap = build_snapshot(gw, None)
    assert snap["degraded"] == {"embed_service_unavailable": True}
    assert snap["embed_service"] is None
    assert snap["gateway"]["tool_calls"]["total"] == 3.0
    assert snap["gateway"]["tool_calls"]["by_tool"] == {"search": 3.0}
    assert snap["gateway"]["tool_duration_ms"]["p50"] == pytest.approx(15.0)

    snap2 = build_snapshot(gw, parse_prom_text("memlake_embedding_embed_calls_total 1\n"))
    assert snap2["embed_service"] is not None
    assert snap2["degraded"] == {"embed_service_unavailable": False}


# ============================== 序列提取 ==============================


def test_extract_series_window_filter_and_rate():
    # extract_series 以 time.time() 为窗口右端，样本时间戳须取当前时间附近
    now = time.time()
    # 样本形状对齐 build_sample 产出：速率派生的源 key（success/error）恒存在
    samples = [
        {"ts": now - 7200, "gw_tool_success": 0, "gw_tool_error": 0},  # 窗口外（1h）
        {"ts": now - 3500, "gw_tool_success": 10, "gw_tool_error": 0},  # 离窗口边界远（避免取时误差 flake）
        {"ts": now - 3440, "gw_tool_success": 13, "gw_tool_error": 0, "gw_tool_p95_ms": None},
        {"ts": now - 3380, "gw_tool_success": 15, "gw_tool_error": 0, "gw_tool_p95_ms": 42.0},
        {"ts": now - 3320, "gw_tool_success": 5, "gw_tool_error": 0},  # 计数器回落 → 跳点
        {"ts": now - 600, "gw_tool_success": 6, "gw_tool_error": 0},  # 间隔过大 → 跳点
        {"ts": now - 540, "gw_tool_success": 9, "gw_tool_error": 0},
    ]
    series = extract_series(samples, 3600)
    # 窗口外样本被过滤（extract_series 对 ts 做 3 位小数舍入）
    assert series["gw_tool_success"][0] == [round(now - 3500, 3), 10]
    # None 值跳过：p95 只有一个点
    assert series["gw_tool_p95_ms"] == [[round(now - 3380, 3), 42.0]]
    # 速率派生：10→13（3 次/60s=3/min）、13→15（2/min）、6→9（3/min）；
    # 回落与间隔过大均不出点
    rates = series["gw_tool_calls_per_min"]
    assert rates == [
        [round(now - 3440, 3), 3.0],
        [round(now - 3380, 3), 2.0],
        [round(now - 540, 3), 3.0],
    ]


def test_extract_series_empty():
    assert extract_series([], 3600) == {}


# ============================== 采样器 ==============================


async def test_sampler_ring_buffer_maxlen():
    sampler = MetricsSampler(interval=0.01, max_samples=3)
    for i in range(5):
        sampler._samples.append({"ts": float(i), "gw_tool_success": i})
    assert [s["ts"] for s in sampler.samples()] == [2.0, 3.0, 4.0]


async def test_sampler_start_stop_and_error_recovery(monkeypatch):
    sampler = MetricsSampler(interval=0.01)
    calls = {"n": 0}

    async def flaky_sample() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一轮失败")

    monkeypatch.setattr(sampler, "_sample_once", flaky_sample)
    sampler.start()
    assert sampler.running
    await asyncio.sleep(0.05)
    # 首轮异常不杀死循环，后续轮次继续
    assert calls["n"] >= 2
    await sampler.stop()
    assert not sampler.running
    # 幂等
    await sampler.stop()


# ============================== 端点 ==============================


def make_client(**overrides: Any) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="test-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
    assert r.status_code == 200


async def test_monitor_endpoints_require_session():
    async with make_client() as client:
        for path in ("/api/monitor/snapshot", "/api/monitor/series", "/api/monitor/tail"):
            r = await client.get(path)
            assert r.status_code == 401, path


async def test_snapshot_endpoint(monkeypatch):
    monkeypatch.setattr(monitor_api, "collect_gateway_metrics", _pm_with_tool_metrics)
    monkeypatch.setattr(monitor_api, "collect_embed_metrics", _afake_none)

    async with make_client() as client:
        await _login(client)
        r = await client.get("/api/monitor/snapshot")
        assert r.status_code == 200
        data = r.json()
        assert data["degraded"] == {"embed_service_unavailable": True}
        assert data["embed_service"] is None
        assert data["gateway"]["tool_calls"]["total"] == 3


async def _afake_none() -> ParsedMetrics | None:
    return None


async def test_series_endpoint_validation(monkeypatch):
    monkeypatch.setattr(monitor_api, "collect_gateway_metrics", _pm_with_tool_metrics)
    monkeypatch.setattr(monitor_api, "collect_embed_metrics", _afake_none)

    async with make_client() as client:
        await _login(client)
        # 非法 window → 400；合法默认 → 200 且无采样器时降级为空
        r = await client.get("/api/monitor/series?window=2h")
        assert r.status_code == 400
        r = await client.get("/api/monitor/series")
        assert r.status_code == 200
        assert r.json()["sampler_running"] is False
        assert r.json()["series"] == {}
        r = await client.get("/api/monitor/series?window=24h")
        assert r.status_code == 200
        assert r.json()["window"] == "24h"


async def test_series_endpoint_with_samples(monkeypatch):
    monkeypatch.setattr(monitor_api, "collect_gateway_metrics", _pm_with_tool_metrics)
    monkeypatch.setattr(monitor_api, "collect_embed_metrics", _afake_none)
    sampler = MetricsSampler(interval=60, max_samples=10)
    now = time.time()
    sampler._samples.append({"ts": now - 120, "gw_tool_success": 1.0, "gw_tool_error": 0.0, "gw_tool_p95_ms": 10.0})
    sampler._samples.append({"ts": now - 60, "gw_tool_success": 4.0, "gw_tool_error": 0.0, "gw_tool_p95_ms": 12.0})

    settings = Settings(VISUAL_SESSION_SECRET="test-secret", VISUAL_USERNAME="vu", VISUAL_PASSWORD="vp")
    app: Starlette = create_visual_app(settings)
    app.state.visual_sampler = sampler  # 模拟 lifespan 已挂载（未 start，历史仍可读）
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        r = await client.get("/api/monitor/series?window=1h")
        assert r.status_code == 200
        data = r.json()
        # 采样器存在但未 start：sampler_running=false，历史样本仍可读（ts 3 位舍入）
        assert data["sampler_running"] is False
        assert data["series"]["gw_tool_success"] == [
            [round(now - 120, 3), 1.0],
            [round(now - 60, 3), 4.0],
        ]
        assert data["series"]["gw_tool_calls_per_min"] == [[round(now - 60, 3), 3.0]]


class _FakeSessionCtx:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: Any) -> None:
        return None


async def test_tail_endpoint(monkeypatch):
    rows = [
        SimpleNamespace(
            created_at=datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC),
            actor="key-1",
            detail={
                "tool_name": "search",
                "result_status": "success",
                "duration_ms": 12.5,
                "operation_id": "op-1",
                "error_message": None,
            },
        ),
        SimpleNamespace(
            created_at=datetime(2026, 10, 6, 12, 0, 1, tzinfo=UTC),
            actor="key-2",
            detail={"tool_name": "write", "result_status": "error", "duration_ms": 3.0},
        ),
    ]

    queried: list[dict[str, Any]] = []

    async def fake_query(session: Any, **kw: Any) -> list[Any]:
        queried.append(kw)
        return rows

    monkeypatch.setattr(monitor_api, "readonly_session", lambda: _FakeSessionCtx())
    monkeypatch.setattr(monitor_api, "query_audit_logs", fake_query)

    async with make_client() as client:
        await _login(client)
        # 非法 limit：非整数 / 越界 → 400
        for bad in ("abc", "0", "201", "-1"):
            r = await client.get(f"/api/monitor/tail?limit={bad}")
            assert r.status_code == 400, bad
        r = await client.get("/api/monitor/tail")
        assert r.status_code == 200
        assert queried[0]["action"] == "tool_call"
        assert queried[0]["limit"] == 50
        data = r.json()
        assert len(data["calls"]) == 2
        first = data["calls"][0]
        assert first["tool_name"] == "search"
        assert first["duration_ms"] == 12.5
        assert first["error_message"] is None
        assert data["calls"][1]["result_status"] == "error"
        # 边界：limit 上下界均合法
        for ok in ("1", "200"):
            r = await client.get(f"/api/monitor/tail?limit={ok}")
            assert r.status_code == 200, ok


# ============================== lifespan ==============================


async def test_lifespan_starts_and_stops_sampler(monkeypatch):
    """采样器跟随应用生命周期启停；ASGI 直连不跑 lifespan 时端点已降级覆盖。"""
    app = create_visual_app(Settings(VISUAL_SESSION_SECRET="s", VISUAL_USERNAME="vu", VISUAL_PASSWORD="vp"))

    async def noop_sample() -> None:
        return None

    monkeypatch.setattr(MetricsSampler, "_sample_once", noop_sample)
    async with _lifespan(app):
        sampler = app.state.visual_sampler
        assert isinstance(sampler, MetricsSampler)
        assert sampler.running
    assert not app.state.visual_sampler.running
