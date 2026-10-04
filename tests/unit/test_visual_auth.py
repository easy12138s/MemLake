"""可视化控制台会话/凭据/防爆破纯函数测试。"""
from mem_lake.visual.auth import (
    COOKIE_NAME,
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
