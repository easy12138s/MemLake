"""结构化日志配置与请求上下文绑定。

基于 structlog，通过 stdlib.ProcessorFormatter 桥接，使现有 logging.getLogger("mem_lake.*")
调用点无需改动即可输出结构化日志（json 或 console）。
structlog.contextvars 绑定 request_id/operation_id/actor/project_id 等贯通字段，
在中间件 per-request 绑定、finally 清理。
"""

import logging

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
)

from mem_lake.config import get_settings


def configure_logging(level: int = logging.INFO, fmt: str | None = None) -> None:
    """配置全局日志为结构化输出。

    fmt：'json'（单行 JSON，生产推荐）/ 'console'（可读，默认）。
    通过 ProcessorFormatter 桥接 stdlib，现有 logging.getLogger 输出自动结构化；
    每个 record 会合并 structlog 绑定的请求上下文（merge_contextvars）。
    """
    fmt = fmt or get_settings().OBS_LOG_FORMAT

    # structlog 配置：stdlib 适配，使 structlog.config 与 stdlib logging 均可使用
    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer()
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
            processors=[
                merge_contextvars,
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    # 清掉默认 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)


def bind_request_context(**kwargs) -> None:
    """为当前请求绑定结构化上下文字段（request_id/operation_id/actor 等）。"""
    bind_contextvars(**kwargs)


def clear_request_context() -> None:
    """清理当前请求的上下文绑定（中间件 finally 调用）。"""
    clear_contextvars()
