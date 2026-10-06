"""可视化控制台会话/凭据/防爆破纯函数测试。"""
from mem_lake.visual.auth import (
    COOKIE_NAME,
    LOCK_SECONDS,
    MAX_TRACKED_IPS,
    SESSION_TTL_SECONDS,
    LoginGuard,
    check_credentials,
    create_session_token,
    verify_session_token,
)


def test_token_roundtrip():
    token = create_session_token("admin-view", "s3cret", now=1000.0)
    assert verify_session_token(token, "s3cret", now=1001.0) == "admin-view"
    # 错误密钥拒绝
    assert verify_session_token(token, "other", now=1001.0) is None


def test_token_tamper_rejected():
    token = create_session_token("u", "s", now=1000.0)
    b64, sig = token.split(".", 1)
    assert verify_session_token(f"{b64}.deadbeef", "s", now=1001.0) is None
    assert verify_session_token("not-a-token", "s", now=1001.0) is None


def test_token_expiry():
    token = create_session_token("u", "s", now=1000.0, ttl=10)
    assert verify_session_token(token, "s", now=1009.0) == "u"
    assert verify_session_token(token, "s", now=1011.0) is None


def test_check_credentials():
    assert check_credentials("vu", "vp", expected_user="vu", expected_pass="vp")
    assert not check_credentials("vu", "wrong", expected_user="vu", expected_pass="vp")
    assert not check_credentials("", "", expected_user="vu", expected_pass="vp")


def test_login_guard_lockout():
    g = LoginGuard()
    for _ in range(5):
        assert not g.is_locked("1.2.3.4", now=100.0)
        g.register_failure("1.2.3.4", now=100.0)
    assert g.is_locked("1.2.3.4", now=100.0)
    assert not g.is_locked("5.6.7.8", now=100.0)  # 其他 IP 不受影响
    # 窗口滑出后解锁
    assert not g.is_locked("1.2.3.4", now=100.0 + 301)
    g.reset("1.2.3.4")
    assert not g.is_locked("1.2.3.4", now=100.0)


def test_constants():
    assert COOKIE_NAME == "memlake_visual_session"
    assert SESSION_TTL_SECONDS == 12 * 3600


def test_login_guard_probe_no_growth():
    """is_locked 探测未知 IP 不留持久键（原实现插入空列表键，可被海量伪造 IP 撑大）。"""
    g = LoginGuard()
    for i in range(100):
        g.is_locked(f"10.0.{i // 255}.{i % 255}", now=1.0)
    assert g._failures == {}


def test_login_guard_capacity_cap():
    """超过 MAX_TRACKED_IPS 逐出最早失败 IP，dict 有界。"""
    g = LoginGuard()
    n = MAX_TRACKED_IPS + 10
    for i in range(n):
        g.register_failure(f"ip-{i}", now=1.0)
    assert len(g._failures) == MAX_TRACKED_IPS
    assert "ip-0" not in g._failures  # 最早者被逐出
    assert f"ip-{n - 1}" in g._failures  # 最新者保留


def test_login_guard_register_prunes_expired(monkeypatch):
    """容量触顶时先清理过期条目，仍满才逐出。

    brief 原测试体仅注册 2 个 IP，未触达 MAX_TRACKED_IPS 容量，
    与实现的容量触发 prune 契约不符（docstring 与实现均声明「容量触顶时」）；
    monkeypatch 容量为 1 使前置条件成立（执行器偏差，见 task-4-report）。
    """
    monkeypatch.setattr("mem_lake.visual.auth.MAX_TRACKED_IPS", 1)
    g = LoginGuard()
    g.register_failure("old", now=1.0)
    g.register_failure("new", now=1.0 + LOCK_SECONDS + 1)
    assert "old" not in g._failures
    assert "new" in g._failures
