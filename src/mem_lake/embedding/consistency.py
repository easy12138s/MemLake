"""embedding 模型/提供商一致性检测：签名构造与变更决策（纯函数，可单测）。

签名 "provider:model" 用于在应用启动时比对「本次启动的 embedding 模型」与
「上次启动记录的模型」，检测是否发生了 provider/model 切换，从而提示 admin
存量向量由旧模型生成、需重嵌或回退。详见 knowledge/models.EmbeddingState。
"""


def compute_signature(health: dict) -> str:
    """从 embedding 服务 /health 响应构造签名。

    health 形如 {"provider": "local", "model": "/models/Qwen3-Embedding-0.6B", ...}。
    缺 provider 时退回 "local"（兼容旧版 health 无该字段）；缺 model 返回空串作为占位。
    """
    provider = health.get("provider") or "local"
    model = health.get("model") or ""
    return f"{provider}:{model}"


def decide_embedding_change(
    latest_signature: str | None, current_signature: str
) -> tuple[str, bool]:
    """据最近一条历史签名与当前签名决定动作。

    返回 (action, changed)：
    - ("baseline", False)：无历史（首次启动），应写基线，不告警。
    - ("unchanged", False)：签名一致，不写、不告警。
    - ("changed", True)：签名不一致（发生切换），应写新记录并告警。
    """
    if latest_signature is None:
        return ("baseline", False)
    if latest_signature == current_signature:
        return ("unchanged", False)
    return ("changed", True)
