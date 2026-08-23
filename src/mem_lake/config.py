"""配置管理：环境变量加载与配置项定义。

使用 pydantic-settings BaseSettings，支持 .env 文件 + 环境变量覆盖。
字段命名严格对齐 .env.example，扁平结构（因 .env 为扁平命名）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置，从 .env 文件或环境变量加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ========== 数据库 ==========
    DATABASE_URL: str = "postgresql+psycopg_async://memlake:memlake@localhost:5432/memlake"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # ========== MCP 网关 ==========
    MCP_SERVER_NAME: str = "mem-lake"
    MCP_SERVER_HOST: str = "0.0.0.0"
    MCP_SERVER_PORT: int = 8000
    MCP_RATE_LIMIT_QPS: int = 100
    MCP_PUBLIC_URL: str = "http://localhost:8000/mcp"

    # ========== Embedding 服务 ==========
    EMBEDDING_MODEL_PATH: str = "./models/Qwen3-Embedding-0.6B"
    EMBEDDING_DIMENSION: int = 1024
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_HOST: str = "localhost"
    EMBEDDING_PORT: int = 8001

    # ========== Rerank 精排 ==========
    # bge-reranker-base（CrossEncoder）路径。空字符串=不启用精排（向下兼容）。
    # 留空时 ENABLE_RERANK 即使为 True 也不生效，检索退回 RRF 原序。
    RERANK_MODEL_PATH: str = ""
    # 精排候选数（取 RRF 融合后前 N 个精排），越小延迟越低
    RERANK_TOP_K: int = 30
    # 总开关：配合 RERANK_MODEL_PATH 非空才真正启用
    ENABLE_RERANK: bool = True

    # ========== AGE 图 ==========
    AGE_GRAPH_NAME: str = "mem_lake_graph"

    # ========== 安全 ==========
    BCRYPT_ROUNDS: int = 12
    APPROVAL_WARNING_DAYS: int = 7
    APPROVAL_TIMEOUT_DAYS: int = 30
    # 冲突检测相似度阈值。2026-08-22 已按 Qwen3-Embedding-0.6B 实测标定确认
    # （query-doc 模式：相关不同实体最高 0.807、同实体改写最低 0.912，
    # 0.85 落在空隙内；标定记录见 scripts/calibrate_conflict_threshold.py docstring）。
    # 换 embedding 模型后必须用该脚本重标。
    CONFLICT_SIMILARITY_THRESHOLD: float = 0.85

    # ========== 宽松模式（免审批直接入库）==========
    # 全局主开关：false 时即便某个 Access Key 标记为宽松（lax_mode=true）也强制走审批，
    # 作为紧急熔断（单一判定源见 approval/service.submit_batch_with_mode）。
    LAX_MODE_ENABLED: bool = True

    # ========== 可观测性 ==========
    # 是否挂载网关 /metrics（Prometheus 拉取）。面向内网，默认不加鉴权。
    OBS_METRICS_ENABLED: bool = True
    # 结构化日志格式：console（可读文本，默认）/ json（单行 JSON，生产）
    OBS_LOG_FORMAT: str = "console"


@lru_cache
def get_settings() -> Settings:
    """返回 Settings 单例（lru_cache 保证进程内复用）。"""
    return Settings()
