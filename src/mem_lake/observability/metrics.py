"""Prometheus 指标定义与 /metrics 输出。

集中定义 mem-lake 网关进程内的指标，供中间件/服务/检索/嵌入客户端上报；
embedding_server 为独立进程，使用同名字符串指标名但不 import 本模块。
单进程部署使用默认 REGISTRY 即可；若启用 uvicorn --workers N 多进程，
需改为 enable_multiprocess + 共享目录，本模块暂不处理。
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    GC_COLLECTOR,
    PROCESS_COLLECTOR,
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)

# 注册进程/GC 基础指标（幂等，重复 import 时跳过）
for _collector in (PROCESS_COLLECTOR, GC_COLLECTOR):
    try:
        REGISTRY.register(_collector)
    except ValueError:  # 已注册
        pass

# MCP 工具调用（复用 AuditLogMiddleware 已有的计时，不新增测量）
MCP_TOOL_CALLS = Counter(
    "memlake_mcp_tool_calls_total",
    "MCP 工具调用次数（按工具与结果状态）",
    ["tool", "status"],
)
MCP_TOOL_DURATION = Histogram(
    "memlake_mcp_tool_duration_seconds",
    "MCP 工具调用耗时（秒）",
    ["tool"],
    buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# 审批批次终态
APPROVAL_BATCHES = Counter(
    "memlake_approval_batches_total",
    "审批批次终态次数",
    ["status"],
)

# 混合检索三引擎耗时
SEARCH_ENGINE_DURATION = Histogram(
    "memlake_search_engine_duration_seconds",
    "混合检索引擎耗时（秒）",
    ["engine"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

# embedding / rerank 调用（app→embedding 客户端）
EMBEDDING_CALLS = Counter(
    "memlake_embedding_calls_total",
    "embedding/rerank 调用次数",
    ["op"],
)
EMBEDDING_DURATION = Histogram(
    "memlake_embedding_duration_seconds",
    "embedding/rerank 耗时（秒）",
    ["op"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# rerank 降级回退（服务不可用/未加载时静默回退 RRF 原序）
RERANK_FALLBACK = Counter(
    "memlake_rerank_fallback_total",
    "rerank 精排降级回退次数",
)


def get_metrics_body() -> bytes:
    """返回 Prometheus 文本格式的指标输出。"""
    return generate_latest(REGISTRY)


def get_metrics_media_type() -> str:
    """/metrics 响应 Content-Type。"""
    return CONTENT_TYPE_LATEST
