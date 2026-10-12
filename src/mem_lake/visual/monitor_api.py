"""监控页只读端点：指标快照 / 趋势序列 / 工具调用流水（均会话保护）。

降级约定：
- embedding 服务不可达 → snapshot 的 embed_service 为 null + degraded 标记；
- 采样器未启动（如测试 ASGI 直连不走 lifespan）→ series 返回空序列 +
  sampler_running=false，快照仍给实时值；
- audit 查询异常向上抛（数据库故障应显式暴露，不静默吞掉）。
"""

from typing import Any, cast

from starlette.requests import Request
from starlette.responses import JSONResponse

from mem_lake.audit.service import query_audit_logs
from mem_lake.gateway.dependencies import readonly_session
from mem_lake.visual.api import session_user
from mem_lake.visual.monitor import (
    WINDOW_SECONDS,
    MetricsSampler,
    build_snapshot,
    collect_embed_metrics,
    collect_gateway_metrics,
    extract_series,
)

TAIL_DEFAULT_LIMIT = 50
TAIL_MAX_LIMIT = 200


async def monitor_snapshot(request: Request) -> JSONResponse:
    """当前指标快照：网关进程内计数器 + 分位数 + embedding 服务指标（实时采集）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    gw = collect_gateway_metrics()
    es = await collect_embed_metrics()
    return JSONResponse(build_snapshot(gw, es))


def _sampler_of(request: Request) -> MetricsSampler | None:
    """取采样器实例；未挂载（lifespan 未运行）返回 None。"""
    sampler = getattr(request.app.state, "visual_sampler", None)
    return sampler if isinstance(sampler, MetricsSampler) else None


async def monitor_series(request: Request) -> JSONResponse:
    """采样趋势序列（window=1h/6h/24h；采样器未启动时降级为空）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    window = request.query_params.get("window", "1h")
    if window not in WINDOW_SECONDS:
        return JSONResponse({"error": "非法 window，合法值: 1h/6h/24h"}, status_code=400)
    sampler = _sampler_of(request)
    samples = sampler.samples() if sampler is not None else []
    return JSONResponse(
        {
            "window": window,
            "interval_seconds": 60,
            "sampler_running": sampler is not None and sampler.running,
            "series": extract_series(samples, WINDOW_SECONDS[window]),
        }
    )


def _tail_payload(row: Any) -> dict[str, Any]:
    """audit tool_call 行 → 前端流水表载荷（detail 字段按 middleware 写入契约裁剪）。"""
    detail = row.detail or {}
    return {
        "created_at": row.created_at.isoformat(),
        "actor": row.actor,
        "tool_name": detail.get("tool_name"),
        "result_status": detail.get("result_status"),
        "duration_ms": detail.get("duration_ms"),
        "operation_id": detail.get("operation_id"),
        "error_message": detail.get("error_message"),
    }


async def monitor_tail(request: Request) -> JSONResponse:
    """最近工具调用流水（audit_log action=tool_call，只读复用审计查询）。"""
    if session_user(request) is None:
        return JSONResponse({"error": "未认证"}, status_code=401)
    raw = request.query_params.get("limit", str(TAIL_DEFAULT_LIMIT))
    try:
        limit = int(raw)
    except ValueError:
        return JSONResponse({"error": "非法 limit（整数）"}, status_code=400)
    if not 1 <= limit <= TAIL_MAX_LIMIT:
        return JSONResponse({"error": f"limit 须在 1..{TAIL_MAX_LIMIT}"}, status_code=400)
    async with readonly_session() as session:
        rows = await query_audit_logs(session, action="tool_call", limit=limit)
    # query_audit_logs 返回类型含泛型列表，此处收敛为 list 以满足 mypy strict
    return JSONResponse({"calls": [_tail_payload(r) for r in cast(list[Any], rows)]})
