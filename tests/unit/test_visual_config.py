"""可视化控制台配置面测试。"""
import pytest
from pydantic import ValidationError

from mem_lake.config import Settings


def test_visual_defaults():
    s = Settings()
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
        Settings(VISUAL_ENABLED=True)


def test_visual_enabled_rejects_blank_password():
    with pytest.raises(ValidationError):
        Settings(VISUAL_ENABLED=True, VISUAL_PASSWORD="   ")


def test_visual_enabled_with_password_ok():
    s = Settings(VISUAL_ENABLED=True, VISUAL_PASSWORD="s3cret-pass")
    assert s.VISUAL_ENABLED is True
    assert s.VISUAL_PASSWORD == "s3cret-pass"


def test_visual_env_override(monkeypatch):
    monkeypatch.setenv("VISUAL_ENABLED", "true")
    monkeypatch.setenv("VISUAL_PASSWORD", "custom-pass")
    s = Settings()
    assert s.VISUAL_ENABLED is True
    assert s.VISUAL_PASSWORD == "custom-pass"
