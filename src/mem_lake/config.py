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

    # ========== Embedding 服务 ==========
    EMBEDDING_MODEL_PATH: str = "./models/Qwen3-Embedding-0.6B"
    EMBEDDING_DIMENSION: int = 1024
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_HOST: str = "localhost"
    EMBEDDING_PORT: int = 8001

    # ========== AGE 图 ==========
    AGE_GRAPH_NAME: str = "mem_lake_graph"

    # ========== 安全 ==========
    BCRYPT_ROUNDS: int = 12
    APPROVAL_WARNING_DAYS: int = 7
    APPROVAL_TIMEOUT_DAYS: int = 30
    CONFLICT_SIMILARITY_THRESHOLD: float = 0.85


@lru_cache
def get_settings() -> Settings:
    """返回 Settings 单例（lru_cache 保证进程内复用）。"""
    return Settings()
