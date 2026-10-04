"""可视化控制台配置面测试。"""
from mem_lake.config import Settings


def test_visual_defaults():
    s = Settings()
    assert s.VISUAL_ENABLED is False
    assert s.VISUAL_HOST == "0.0.0.0"
    assert s.VISUAL_PORT == 8090
    assert s.VISUAL_USERNAME == "memlake_view"
    assert s.VISUAL_PASSWORD == "easy12138s"
    assert s.VISUAL_SESSION_SECRET == ""
    assert s.VISUAL_GRAPH_MAX_NODES == 1000


def test_visual_env_override(monkeypatch):
    monkeypatch.setenv("VISUAL_ENABLED", "true")
    monkeypatch.setenv("VISUAL_PASSWORD", "custom-pass")
    s = Settings()
    assert s.VISUAL_ENABLED is True
    assert s.VISUAL_PASSWORD == "custom-pass"
