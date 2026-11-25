"""可视化控制台配置面测试。"""
import pytest
from pydantic import ValidationError

from mem_lake.config import Settings


@pytest.fixture(autouse=True)
def _clear_visual_env(monkeypatch):
    """隔离环境：其他模块（markitdown 等）可能把 .env 载入 os.environ。"""
    for name in (
        "VISUAL_ENABLED",
        "VISUAL_HOST",
        "VISUAL_PORT",
        "VISUAL_USERNAME",
        "VISUAL_PASSWORD",
        "VISUAL_SESSION_SECRET",
        "VISUAL_GRAPH_MAX_NODES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_visual_defaults():
    # _env_file=None：隔离开发者本地 .env（可能已配置 VISUAL_PASSWORD）
    s = Settings(_env_file=None)
    assert s.VISUAL_ENABLED is False
    assert s.VISUAL_HOST == "0.0.0.0"
    assert s.VISUAL_PORT == 8090
    assert s.VISUAL_USERNAME == "memlake_view"
    assert s.VISUAL_PASSWORD == ""
    assert s.VISUAL_SESSION_SECRET == ""
    assert s.VISUAL_GRAPH_MAX_NODES == 1000


def test_visual_enabled_requires_password():
    """开启控制台但未配置口令时必须拒绝启动，避免公开默认口令。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, VISUAL_ENABLED=True)


def test_visual_enabled_rejects_blank_password():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, VISUAL_ENABLED=True, VISUAL_PASSWORD="   ")


def test_visual_enabled_with_password_ok():
    s = Settings(_env_file=None, VISUAL_ENABLED=True, VISUAL_PASSWORD="s3cret-pass")
    assert s.VISUAL_ENABLED is True
    assert s.VISUAL_PASSWORD == "s3cret-pass"


def test_visual_env_override(monkeypatch):
    monkeypatch.setenv("VISUAL_ENABLED", "true")
    monkeypatch.setenv("VISUAL_PASSWORD", "custom-pass")
    s = Settings()
    assert s.VISUAL_ENABLED is True
    assert s.VISUAL_PASSWORD == "custom-pass"
