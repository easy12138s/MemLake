"""可观测性：Prometheus 指标 /metrics 与结构化日志。

本包承载：
- metrics：mem-lake 网关与 embedding 服务两个进程各自的指标定义与输出。
- logging：基于 structlog 的结构化日志配置（JSON/console）与请求上下文绑定。
"""
