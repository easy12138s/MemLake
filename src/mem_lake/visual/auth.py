"""可视化控制台认证：HMAC 签名会话 Cookie、口令校验、登录防爆破。

不依赖 itsdangerous/SessionMiddleware——会话仅承载「用户名|过期时间」，
自实现 HMAC-SHA256 签名 token：base64url(payload) + "." + hexsig。
"""

import base64
import hashlib
import hmac
import time

COOKIE_NAME = "memlake_visual_session"
SESSION_TTL_SECONDS = 12 * 3600
LOCK_MAX_FAILURES = 5
LOCK_SECONDS = 300
MAX_TRACKED_IPS = 10_000


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(
    username: str,
    secret: str,
    *,
    now: float | None = None,
    ttl: int = SESSION_TTL_SECONDS,
) -> str:
    """签发会话 token：payload 为 f"{username}|{exp}"。"""
    exp = int((now if now is not None else time.time()) + ttl)
    payload = f"{username}|{exp}"
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{_sign(payload, secret)}"


def verify_session_token(
    token: str, secret: str, *, now: float | None = None
) -> str | None:
    """校验会话 token；有效返回用户名，无效/过期/篡改返回 None。"""
    try:
        b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(_sign(payload, secret), sig):
        return None
    username, _, exp_s = payload.rpartition("|")
    if not username or not exp_s.isdigit():
        return None
    if (now if now is not None else time.time()) >= int(exp_s):
        return None
    return username


def check_credentials(
    username: str, password: str, *, expected_user: str, expected_pass: str
) -> bool:
    """口令比对（常量时间，避免时序侧信道）。"""
    ok_user = hmac.compare_digest(username.encode(), expected_user.encode())
    ok_pass = hmac.compare_digest(password.encode(), expected_pass.encode())
    return ok_user and ok_pass


class LoginGuard:
    """进程内按 IP 防爆破：窗口期内连续失败 LOCK_MAX_FAILURES 次锁定 LOCK_SECONDS 秒。

    内存护栏（批次二顺手改进）：
    - is_locked 探测未知 IP 不留持久键（原实现会插入空列表键，可被海量
      伪造 IP 探测无限撑大 dict）
    - register_failure 在达到 MAX_TRACKED_IPS 时先清理过期条目，仍满则
      逐出最早失败的 IP（伪造海量来源 IP 时防膨胀；代价为被逐出 IP 的
      计数清零，观测台可接受）
    """

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def is_locked(self, ip: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        fails = [t for t in self._failures.get(ip, []) if now - t < LOCK_SECONDS]
        if fails:
            self._failures[ip] = fails
        else:
            self._failures.pop(ip, None)
        return len(fails) >= LOCK_MAX_FAILURES

    def register_failure(self, ip: str, *, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if ip not in self._failures and len(self._failures) >= MAX_TRACKED_IPS:
            self._prune_expired(now)
        if ip not in self._failures and len(self._failures) >= MAX_TRACKED_IPS:
            oldest = min(self._failures, key=lambda k: self._failures[k][0])
            del self._failures[oldest]
        self._failures.setdefault(ip, []).append(now)

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def _prune_expired(self, now: float) -> None:
        """清理全部过期失败条目（仅容量触顶时调用，避免 O(n) 常态开销）。"""
        for ip in list(self._failures):
            fails = [t for t in self._failures[ip] if now - t < LOCK_SECONDS]
            if fails:
                self._failures[ip] = fails
            else:
                del self._failures[ip]
