"""main.py 双 Server 启动编排测试（不实际运行 Server）。"""
from mem_lake.config import Settings
from mem_lake.main import build_uvicorn_servers


def test_single_server_when_disabled():
    servers = build_uvicorn_servers(Settings(VISUAL_ENABLED=False))
    assert len(servers) == 1
    assert servers[0].config.port == 8000


def test_dual_servers_when_enabled():
    servers = build_uvicorn_servers(Settings(VISUAL_ENABLED=True, VISUAL_PORT=8090))
    assert len(servers) == 2
    assert servers[0].config.port == 8000
    assert servers[1].config.port == 8090
    assert servers[1].config.host == "0.0.0.0"
