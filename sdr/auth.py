"""共有モードの認証（ログイン・パスワード保管・セッション・CSRF）。

- パスワードは scrypt（ソルト付き）でハッシュ化して保存。平文・既定の共通パスワードは持たない。
- セッショントークンは乱数 32 バイト。DB にはハッシュだけを保存。
- 更新操作（POST）はすべて CSRF トークンを確認する。
- ログイン失敗が続く場合は一定時間受け付けない。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from .db import utcnow

SESSION_HOURS = 12
MIN_PASSWORD_LEN = 10
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def validate_new_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"パスワードは {MIN_PASSWORD_LEN} 文字以上にしてください"
    if password.lower() in {"password123", "samurai1234", "1234567890", "qwertyuiop"}:
        return "推測されやすいパスワードは使えません"
    return None


def create_user(conn, username: str, password: str) -> None:
    username = username.strip()
    if not (2 <= len(username) <= 40) or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValueError("利用者名は英数字・._- の 2〜40 文字にしてください")
    err = validate_new_password(password)
    if err:
        raise ValueError(err)
    conn.execute("INSERT INTO users(username, password_hash, created_at) VALUES(?,?,?) "
                 "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, disabled=0",
                 (username, hash_password(password), utcnow()))
    # パスワード変更時は既存セッションを無効化
    conn.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username,))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(conn, username: str, password: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT * FROM users WHERE username=? AND disabled=0", (username.strip(),)).fetchone()
    if not row:
        verify_password(password, hash_password("dummy-password-x"))  # 応答時間をそろえる
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
    conn.execute("INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at) VALUES(?,?,?,?,?)",
                 (_token_hash(token), row["id"], csrf, now.isoformat(), (now + timedelta(hours=SESSION_HOURS)).isoformat()))
    return token, csrf


def session_user(conn, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT s.csrf_token, s.expires_at, u.username, u.id FROM sessions s JOIN users u ON u.id=s.user_id "
                       "WHERE s.token_hash=? AND u.disabled=0", (_token_hash(token),)).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        return None
    return {"username": row["username"], "id": row["id"], "csrf": row["csrf_token"]}


def logout(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


class LoginLimiter:
    """同じ利用者名・接続元からの連続失敗を制限する（5 回失敗で 15 分）。"""

    def __init__(self, max_failures: int = 5, window_sec: int = 900):
        self.max_failures = max_failures
        self.window = window_sec
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            fails = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = fails
            return len(fails) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
