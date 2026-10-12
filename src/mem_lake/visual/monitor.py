"""监控数据核心：Prometheus 文本解析、直方图分位数、进程内环形缓冲采样器。

数据来源三处：
- 网关进程内指标：observability.metrics 的 REGISTRY（同进程，generate_latest 文本）；
- embedding 服务 /metrics：外部 HTTP 拉取（同样为 Prometheus 文本格式）；
- audit_log 流水：由 monitor_api 直查数据库，不经过采样器。

内存预算：扁平 key 样本 dict（约 20 个数值字段）× deque(maxlen=1440)，
24 小时 @60s 间隔约 2MB，满足 <5MB 约束。

限制：指标为进程生命周期累计值 + 控制台采样历史，网关/控制台重启后趋势清零
（计数器清零由 delta<0 跳点规则防护，不产生负速率）。
"""

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from mem_lake.config import get_settings
from mem_lake.observability.metrics import get_metrics_body

logger = logging.getLogger("mem_lake.visual.monitor")

# 采样周期与保留窗口（24h / 60s = 1440 个样本）
SAMPLE_INTERVAL_SECONDS = 60.0
RETENTION_SAMPLES = 24 * 3600 // 60

# 趋势窗口合法值（series 端点白名单）
WINDOW_SECONDS = {"1h": 3600, "6h": 21600, "24h": 86400}

# 指标名（与 observability/metrics.py、deploy/embedding_server.py 的定义对齐）
_GW_TOOL_CALLS = "memlake_mcp_tool_calls_total"
_GW_TOOL_DURATION = "memlake_mcp_tool_duration_seconds"
_GW_SEARCH_DURATION = "memlake_search_engine_duration_seconds"
_GW_EMBED_CALLS = "memlake_embedding_calls_total"
_GW_EMBED_DURATION = "memlake_embedding_duration_seconds"
_GW_RERANK_FALLBACK = "memlake_rerank_fallback_total"
_GW_APPROVAL = "memlake_approval_batches_total"
_ES_EMBED_CALLS = "memlake_embedding_embed_calls_total"
_ES_RERANK_CALLS = "memlake_embedding_rerank_calls_total"
_ES_ENDPOINT_DURATION = "memlake_embedding_endpoint_duration_seconds"


# ============================== 文本解析 ==============================


@dataclass
class HistogramData:
    """直方图单 label 组合的桶数据（le 升序不保证，用前需 sort/merge）。"""

    buckets: list[tuple[float, float]] = field(default_factory=list)  # (上界 le, 累计次数)；+Inf 用 math.inf
    sample_sum: float = 0.0
    sample_count: float = 0.0


@dataclass
class ParsedMetrics:
    """Prometheus 文本解析结果：只保留监控页消费的 counter 与 histogram。

    gauge（process_*/python_* 等）不消费不存储，控制解析内存。
    """

    counters: dict[str, list[tuple[dict[str, str], float]]] = field(default_factory=dict)
    histograms: dict[str, list[tuple[dict[str, str], HistogramData]]] = field(default_factory=dict)


def _split_top_level(s: str) -> list[str]:
    """按引号外的逗号切分（标签值内可含逗号）。"""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escaped = False
    for ch in s:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\" and in_quotes:
            buf.append(ch)
            escaped = True
        elif ch == '"':
            buf.append(ch)
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_labels(raw: str) -> dict[str, str]:
    """解析 `k="v",k2="v2"` 标签串；处理引号内的逗号与 \\" 转义。

    限制：标签值中的字面换行（exposition 格式不允许）与空格不特殊处理——
    本项目指标 label 域均为标识符（tool/status/op/engine），不触及该限制。
    """
    labels: dict[str, str] = {}
    for part in _split_top_level(raw):
        k, sep, v = part.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if k:
            labels[k] = v
    return labels


def parse_prom_text(text: str) -> ParsedMetrics:
    """解析 Prometheus 文本格式（本项目数据源均为 prometheus_client 输出）。

    识别：`name_total{labels} v` 计数器；`name_bucket{le=..} v` + `name_sum/_count`
    直方图。忽略注释行、gauge 与时间戳列（generate_latest 不带 exemplar/ts）。
    """
    pm = ParsedMetrics()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split(" ", 1)
        if len(tokens) < 2:
            continue
        head = tokens[0]
        rest = tokens[1].strip()
        # 取第一个空白前的 token 为值（忽略可选时间戳列）
        value_token = rest.split(" ", 1)[0]
        try:
            value = float(value_token)
        except ValueError:
            continue
        if math.isnan(value):
            continue
        if "{" in head:
            name, _, label_part = head.partition("{")
            labels = _parse_labels(label_part.rstrip("}"))
        else:
            name, labels = head, {}
        if name.endswith("_bucket"):
            base = name[: -len("_bucket")]
            le_raw = labels.pop("le", "+Inf")
            try:
                le = math.inf if le_raw in ("+Inf", "Inf") else float(le_raw)
            except ValueError:
                continue
            _histogram_of(pm, base, labels).buckets.append((le, value))
        elif name.endswith("_sum") or name.endswith("_count"):
            base = name[: -len("_sum")] if name.endswith("_sum") else name[: -len("_count")]
            h = _histogram_of(pm, base, labels)
            if name.endswith("_sum"):
                h.sample_sum = value
            else:
                h.sample_count = value
        elif name.endswith("_total"):
            pm.counters.setdefault(name, []).append((labels, value))
    return pm


def _histogram_of(pm: ParsedMetrics, base: str, labels: dict[str, str]) -> HistogramData:
    """按 label 组合取直方图累积条目（不存在则创建）。"""
    families = pm.histograms.setdefault(base, [])
    for lab, h in families:
        if lab == labels:
            return h
    h = HistogramData()
    families.append((labels, h))
    return h


# ============================== 聚合与分位数 ==============================


def sum_counter(pm: ParsedMetrics, name: str, **want: str) -> float:
    """按 label 过滤求和 counter；无匹配返回 0（零计数不报错）。"""
    total = 0.0
    for labels, value in pm.counters.get(name, []):
        if all(labels.get(k) == v for k, v in want.items()):
            total += value
    return total


def counter_by_label(pm: ParsedMetrics, name: str, label: str) -> dict[str, float]:
    """按指定 label 维度展开 counter 值（快照 by_tool 展示用）。"""
    out: dict[str, float] = {}
    for labels, value in pm.counters.get(name, []):
        key = labels.get(label)
        if key is not None:
            out[key] = out.get(key, 0.0) + value
    return out


def histograms_of(pm: ParsedMetrics, name: str) -> list[HistogramData]:
    """取指定直方图族的全部 label 组合数据。"""
    return [h for _, h in pm.histograms.get(name, [])]


def merge_histograms(items: list[HistogramData]) -> HistogramData:
    """合并同族多 label 组合的直方图（桶界一致时逐桶累加，等价于全量聚合）。"""
    if not items:
        return HistogramData()
    by_le: dict[float, float] = {}
    for h in items:
        for le, cum in h.buckets:
            by_le[le] = by_le.get(le, 0.0) + cum
    return HistogramData(
        buckets=sorted(by_le.items()),
        sample_sum=sum(h.sample_sum for h in items),
        sample_count=sum(h.sample_count for h in items),
    )


def histogram_quantile(h: HistogramData, q: float) -> float | None:
    """桶线性插值估计分位数（对齐 Prometheus histogram_quantile 语义）。

    边界约定：零计数 → None；分位落入 +Inf 桶或超出全部有限桶（缺失上界桶）
    → None（无上界不可估计，返回空值而非错误）。
    """
    if h.sample_count <= 0 or not h.buckets:
        return None
    buckets = sorted(h.buckets)
    rank = q * h.sample_count
    prev_le, prev_cum = 0.0, 0.0
    for le, cum in buckets:
        if cum >= rank:
            if math.isinf(le):
                return None
            if cum <= prev_cum:
                return le
            fraction = (rank - prev_cum) / (cum - prev_cum)
            return prev_le + fraction * (le - prev_le)
        prev_le, prev_cum = le, cum
    return None


def _quantiles_ms(items: list[HistogramData], qs: tuple[float, ...]) -> dict[str, float | None]:
    """合并直方图族并输出 {p50/p95/...: 毫秒}。"""
    merged = merge_histograms(items)
    return {
        f"p{int(q * 100)}": (round(v * 1000, 2) if (v := histogram_quantile(merged, q)) is not None else None)
        for q in qs
    }


# ============================== 采集 ==============================


def collect_gateway_metrics() -> ParsedMetrics:
    """采集网关进程内 Prometheus 指标（同进程 REGISTRY，无网络开销）。"""
    return parse_prom_text(get_metrics_body().decode("utf-8"))


async def collect_embed_metrics() -> ParsedMetrics | None:
    """拉取 embedding 服务 /metrics；不可达/非 200 返回 None（降级不抛）。"""
    settings = get_settings()
    url = f"http://{settings.EMBEDDING_HOST}:{settings.EMBEDDING_PORT}/metrics"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return parse_prom_text(resp.text)
    except httpx.HTTPError:
        return None


def build_sample(gw: ParsedMetrics, es: ParsedMetrics | None, *, now: float | None = None) -> dict[str, Any]:
    """组装一份扁平 key 采样（分位数在采样期计算，历史只存标量，控制内存）。"""
    sample: dict[str, Any] = {
        "ts": now if now is not None else time.time(),
        "gw_tool_success": sum_counter(gw, _GW_TOOL_CALLS, status="success"),
        "gw_tool_error": sum_counter(gw, _GW_TOOL_CALLS, status="error"),
        "gw_embed_calls": sum_counter(gw, _GW_EMBED_CALLS, op="embed"),
        "gw_rerank_calls": sum_counter(gw, _GW_EMBED_CALLS, op="rerank"),
        "gw_rerank_fallback": sum_counter(gw, _GW_RERANK_FALLBACK),
        "gw_appr_approved": sum_counter(gw, _GW_APPROVAL, status="approved"),
        "gw_appr_rejected": sum_counter(gw, _GW_APPROVAL, status="rejected"),
    }
    for key, v in _quantiles_ms(histograms_of(gw, _GW_TOOL_DURATION), (0.5, 0.95, 0.99)).items():
        sample[f"gw_tool_{key}_ms"] = v
    for key, v in _quantiles_ms(histograms_of(gw, _GW_SEARCH_DURATION), (0.5, 0.95)).items():
        sample[f"gw_search_{key}_ms"] = v
    for key, v in _quantiles_ms(histograms_of(gw, _GW_EMBED_DURATION), (0.5, 0.95)).items():
        sample[f"gw_embed_{key}_ms"] = v
    if es is not None:
        sample["es_embed_calls"] = sum_counter(es, _ES_EMBED_CALLS)
        sample["es_rerank_calls"] = sum_counter(es, _ES_RERANK_CALLS)
        for op, prefix in (("embed", "es_embed"), ("rerank", "es_rerank")):
            items = [h for labels, h in es.histograms.get(_ES_ENDPOINT_DURATION, []) if labels.get("op") == op]
            for key, v in _quantiles_ms(items, (0.5, 0.95)).items():
                sample[f"{prefix}_{key}_ms"] = v
    return sample


def build_snapshot(gw: ParsedMetrics, es: ParsedMetrics | None) -> dict[str, Any]:
    """实时快照载荷（计数器 + 分位数 + by_tool 展开；embedding 不可达为 None）。"""
    tool_q = _quantiles_ms(histograms_of(gw, _GW_TOOL_DURATION), (0.5, 0.95, 0.99))
    search_q = _quantiles_ms(histograms_of(gw, _GW_SEARCH_DURATION), (0.5, 0.95))
    embed_q = _quantiles_ms(histograms_of(gw, _GW_EMBED_DURATION), (0.5, 0.95))
    success = sum_counter(gw, _GW_TOOL_CALLS, status="success")
    error = sum_counter(gw, _GW_TOOL_CALLS, status="error")
    snapshot: dict[str, Any] = {
        "collected_at": datetime.now(UTC).isoformat(),
        "gateway": {
            "tool_calls": {
                "total": success + error,
                "success": success,
                "error": error,
                "by_tool": counter_by_label(gw, _GW_TOOL_CALLS, "tool"),
            },
            "tool_duration_ms": tool_q,
            "search_duration_ms": search_q,
            "embedding_calls": {
                "embed": sum_counter(gw, _GW_EMBED_CALLS, op="embed"),
                "rerank": sum_counter(gw, _GW_EMBED_CALLS, op="rerank"),
            },
            "embedding_duration_ms": embed_q,
            "rerank_fallback": sum_counter(gw, _GW_RERANK_FALLBACK),
            "approval_batches": {
                "approved": sum_counter(gw, _GW_APPROVAL, status="approved"),
                "rejected": sum_counter(gw, _GW_APPROVAL, status="rejected"),
            },
        },
        "embed_service": None,
        "degraded": {"embed_service_unavailable": es is None},
    }
    if es is not None:
        snapshot["embed_service"] = {
            "embed_calls": sum_counter(es, _ES_EMBED_CALLS),
            "rerank_calls": sum_counter(es, _ES_RERANK_CALLS),
            "embed_duration_ms": _quantiles_ms(
                [h for labels, h in es.histograms.get(_ES_ENDPOINT_DURATION, []) if labels.get("op") == "embed"],
                (0.5, 0.95),
            ),
            "rerank_duration_ms": _quantiles_ms(
                [h for labels, h in es.histograms.get(_ES_ENDPOINT_DURATION, []) if labels.get("op") == "rerank"],
                (0.5, 0.95),
            ),
        }
    return snapshot


# ============================== 采样器 ==============================


class MetricsSampler:
    """随可视化应用生命周期的后台采样器：周期采集写入进程内环形缓冲。

    单事件循环内与请求处理器共享（无跨线程访问），deque 无需加锁。
    stop 后缓冲仍可读（series 端点降级为历史尾巴）。
    """

    def __init__(
        self,
        interval: float = SAMPLE_INTERVAL_SECONDS,
        max_samples: int = RETENTION_SAMPLES,
    ) -> None:
        self._interval = interval
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """启动采样循环（幂等；需在事件循环内调用）。"""
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """停止采样循环（幂等；不清理缓冲）。"""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def samples(self) -> list[dict[str, Any]]:
        """返回缓冲副本（请求处理器安全持有）。"""
        return list(self._samples)

    async def _run(self) -> None:
        while True:
            # 单轮失败只跳过本轮：采样循环必须比任何一次采集更长寿
            try:
                await self._sample_once()
            except Exception:
                logger.exception("监控采样失败，跳过本轮")
            await asyncio.sleep(self._interval)

    async def _sample_once(self) -> None:
        gw = collect_gateway_metrics()
        es = await collect_embed_metrics()
        self._samples.append(build_sample(gw, es))


# ============================== 序列提取 ==============================

# 直接透出的累计型序列 key（与 build_sample 的扁平 key 对齐）
_SERIES_KEYS = (
    "gw_tool_success",
    "gw_tool_error",
    "gw_tool_p50_ms",
    "gw_tool_p95_ms",
    "gw_tool_p99_ms",
    "gw_search_p95_ms",
    "gw_embed_calls",
    "gw_rerank_calls",
    "gw_embed_p95_ms",
    "gw_rerank_fallback",
    "gw_appr_approved",
    "gw_appr_rejected",
    "es_embed_calls",
    "es_rerank_calls",
    "es_embed_p95_ms",
    "es_rerank_p95_ms",
)

# 派生速率序列：(输出 key, 源累计 key 列表)
_RATE_SERIES = (
    ("gw_tool_calls_per_min", ("gw_tool_success", "gw_tool_error")),
    ("gw_embed_calls_per_min", ("gw_embed_calls",)),
)


def extract_series(samples: list[dict[str, Any]], window_seconds: int) -> dict[str, list[list[float]]]:
    """从采样缓冲提取窗口内趋势序列；值为 None/缺失的点跳过（降级为少点）。"""
    cutoff = time.time() - window_seconds
    window = sorted((s for s in samples if s["ts"] >= cutoff), key=lambda s: s["ts"])
    series: dict[str, list[list[float]]] = {}
    for key in _SERIES_KEYS:
        points = [
            [round(s["ts"], 3), s[key]] for s in window if isinstance(s.get(key), (int, float)) and s[key] is not None
        ]
        if points:
            series[key] = points
    for out_key, src_keys in _RATE_SERIES:
        points = _derive_rate(window, src_keys)
        if points:
            series[out_key] = points
    return series


def _derive_rate(window: list[dict[str, Any]], src_keys: tuple[str, ...]) -> list[list[float]]:
    """由累计计数器相邻样本差值派生每分钟速率。

    跳点规则：计数器回落（进程重启清零）、样本间隔异常（>2.5 倍周期，
    视为采样中断）时不出点，避免负值与尖刺。
    """
    points: list[list[float]] = []
    for prev, curr in zip(window, window[1:]):
        dt = curr["ts"] - prev["ts"]
        if dt <= 0 or dt > 2.5 * SAMPLE_INTERVAL_SECONDS:
            continue
        delta = 0.0
        ok = True
        for key in src_keys:
            pv, cv = prev.get(key), curr.get(key)
            if not isinstance(pv, (int, float)) or not isinstance(cv, (int, float)):
                ok = False
                break
            if cv < pv:  # 计数器回落：重启清零
                ok = False
                break
            delta += cv - pv
        if ok:
            points.append([round(curr["ts"], 3), round(delta / dt * 60, 2)])
    return points
