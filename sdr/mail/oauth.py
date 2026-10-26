"""OAuth 2.0（認可コード＋PKCE、デスクトップアプリ向けのループバック受け口）。

- 受け口はこのアプリ自身の画面（http://127.0.0.1:ポート/oauth/callback。Microsoft は http://localhost:ポート/…）。
  Google・Microsoft ともデスクトップ向けにループバックの受け口を認めている（2026-10-26 公式資料で確認）。
- 更新トークン（refresh token）だけを OS の資格情報保管に保存し、アクセストークンはメモリーにだけ置く。
- 利用者が自分の Google Cloud / Microsoft Entra でアプリ登録を行い、クライアント ID を設定する。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx

from .base import AuthError, MailError

GOOGLE = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
    "redirect_host": "127.0.0.1",
}
MICROSOFT_SCOPE = "offline_access User.Read Mail.Send Mail.ReadWrite"


def microsoft_endpoints(tenant: str) -> dict:
    tenant = tenant if tenant in ("common", "consumers", "organizations") or len(tenant) == 36 else "common"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return {"auth_url": base + "/authorize", "token_url": base + "/token", "scope": MICROSOFT_SCOPE,
            "redirect_host": "localhost"}


def endpoints_for(account: dict) -> dict:
    if account["provider"] == "gmail":
        return GOOGLE
    if account["provider"] == "graph":
        return microsoft_endpoints((account.get("config") or {}).get("tenant", "common"))
    raise MailError("OAuth を使わない接続方式です")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def redirect_uri(account: dict, port: int) -> str:
    return f"http://{endpoints_for(account)['redirect_host']}:{port}/oauth/callback"


def authorization_url(account: dict, port: int, state: str, challenge: str) -> str:
    ep = endpoints_for(account)
    client_id = (account.get("config") or {}).get("client_id", "")
    if not client_id:
        raise MailError("クライアント ID が設定されていません")
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri(account, port),
              "scope": ep["scope"], "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    if account["provider"] == "gmail":
        params.update({"access_type": "offline", "prompt": "consent", "login_hint": account["from_address"]})
    else:
        params.update({"response_mode": "query", "login_hint": account["from_address"]})
    return ep["auth_url"] + "?" + urlencode(params)


def exchange_code(account: dict, secret_store, code: str, verifier: str, port: int, http: httpx.Client | None = None) -> None:
    ep = endpoints_for(account)
    data = {"client_id": account["config"].get("client_id", ""), "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(account, port), "code_verifier": verifier}
    secret = secret_store.get(f"{account['id']}:client_secret") if account["provider"] == "gmail" else None
    if secret:
        data["client_secret"] = secret
    if account["provider"] == "graph":
        data["scope"] = ep["scope"]
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(ep["token_url"], data=data)
    except httpx.HTTPError as exc:
        raise MailError(f"認証サーバーに接続できません：{exc}") from exc
    if r.status_code != 200:
        raise AuthError(f"認可コードを交換できませんでした（{r.status_code}）：{_err(r)}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise AuthError("更新トークンが返されませんでした（Google はアクセス許可の画面で同意し直してください）")
    secret_store.set(f"{account['id']}:refresh_token", tok["refresh_token"])
    TokenCache.put(account["id"], tok.get("access_token"), int(tok.get("expires_in", 0)))


def _err(r: httpx.Response) -> str:
    try:
        j = r.json()
        return str(j.get("error_description") or j.get("error") or j)[:300]
    except ValueError:
        return r.text[:300]


class TokenCache:
    _lock = threading.Lock()
    _tokens: dict[str, tuple[str, float]] = {}

    @classmethod
    def put(cls, key: str, token: str | None, expires_in: int) -> None:
        if token:
            with cls._lock:
                cls._tokens[key] = (token, time.time() + max(60, expires_in) - 60)

    @classmethod
    def get(cls, key: str) -> str | None:
        with cls._lock:
            v = cls._tokens.get(key)
            return v[0] if v and v[1] > time.time() else None

    @classmethod
    def drop(cls, key: str) -> None:
        with cls._lock:
            cls._tokens.pop(key, None)


def access_token(account: dict, secret_store, http: httpx.Client | None = None, force: bool = False) -> str:
    """保存した更新トークンからアクセストークンを得る。失効していれば AuthError（再認証が必要）。"""
    if not force:
        cached = TokenCache.get(account["id"])
        if cached:
            return cached
    refresh = secret_store.get(f"{account['id']}:refresh_token") if secret_store else None
    if not refresh:
        raise AuthError("まだ接続（認証）されていません。メール接続の画面で「接続する」を押してください")
    ep = endpoints_for(account)
    data = {"client_id": account["config"].get("client_id", ""), "grant_type": "refresh_token", "refresh_token": refresh}
    if account["provider"] == "gmail":
        secret = secret_store.get(f"{account['id']}:client_secret")
        if secret:
            data["client_secret"] = secret
    else:
        data["scope"] = ep["scope"]
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(ep["token_url"], data=data)
    except httpx.HTTPError as exc:
        raise MailError(f"認証サーバーに接続できません：{exc}") from exc
    if r.status_code in (400, 401):
        TokenCache.drop(account["id"])
        raise AuthError(f"認証の期限切れか取り消しのため、再認証が必要です：{_err(r)}")
    if r.status_code != 200:
        raise MailError(f"アクセストークンを取得できません（{r.status_code}）")
    tok = r.json()
    if tok.get("refresh_token") and tok["refresh_token"] != refresh:  # 更新トークンが入れ替わる場合（Microsoft）
        secret_store.set(f"{account['id']}:refresh_token", tok["refresh_token"])
    TokenCache.put(account["id"], tok["access_token"], int(tok.get("expires_in", 3600)))
    return tok["access_token"]


class PendingAuth:
    """画面で「接続する」を押してから戻ってくるまでの state と PKCE の値（メモリーのみ・10 分で失効）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, str, float]] = {}

    def add(self, account_id: str) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        with self._lock:
            now = time.time()
            self._items = {k: v for k, v in self._items.items() if v[2] > now}
            self._items[state] = (account_id, verifier, now + 600)
        return state, challenge

    def pop(self, state: str) -> tuple[str, str] | None:
        with self._lock:
            v = self._items.pop(state, None)
        if not v or v[2] < time.time():
            return None
        return v[0], v[1]
