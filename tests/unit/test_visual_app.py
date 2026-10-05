"""可视化控制台应用骨架：登录/会话/登出 API 测试（httpx ASGI 进程内）。"""
import httpx
from starlette.applications import Starlette

from mem_lake.config import Settings
from mem_lake.visual import create_visual_app


def make_client(**overrides) -> httpx.AsyncClient:
    settings = Settings(
        VISUAL_SESSION_SECRET="test-secret",
        VISUAL_USERNAME="vu",
        VISUAL_PASSWORD="vp",
        **overrides,
    )
    app: Starlette = create_visual_app(settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_me_requires_session():
    async with make_client() as client:
        r = await client.get("/api/me")
        assert r.status_code == 401


async def test_login_logout_flow():
    async with make_client() as client:
        # 错误口令
        r = await client.post(
            "/api/login", json={"username": "vu", "password": "wrong"}
        )
        assert r.status_code == 401
        # 正确口令 → 会话 Cookie 生效
        r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
        assert r.status_code == 200
        assert r.json()["username"] == "vu"
        r = await client.get("/api/me")
        assert r.status_code == 200
        assert r.json()["username"] == "vu"
        # 登出后失效
        r = await client.post("/api/logout")
        assert r.status_code == 200
        r = await client.get("/api/me")
        assert r.status_code == 401


async def test_login_lockout():
    async with make_client() as client:
        for _ in range(5):
            r = await client.post(
                "/api/login", json={"username": "vu", "password": "bad"}
            )
            assert r.status_code == 401
        # 第 6 次：正确口令也被锁定拒绝
        r = await client.post("/api/login", json={"username": "vu", "password": "vp"})
        assert r.status_code == 429


async def test_login_invalid_body():
    async with make_client() as client:
        r = await client.post(
            "/api/login",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401


async def test_login_non_dict_json_body():
    async with make_client() as client:
        r = await client.post(
            "/api/login",
            content=b"[1, 2]",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401
        r = await client.post(
            "/api/login",
            content=b'"plain-string"',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401
