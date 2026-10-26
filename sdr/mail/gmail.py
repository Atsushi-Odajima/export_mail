"""Gmail API（送信：users.messages.send、受信：users.history.list、照合：rfc822msgid 検索）。

公式資料（2026-10-26 確認）：
- 送信は RFC 2822 形式を base64url にした raw を POST する。
- 受信は getProfile の historyId から history.list（messageAdded）で差分を取る。古い historyId は 404 になり、全体の再同期が必要。
- 権限：gmail.send（機密）、gmail.readonly（制限付き）。個人利用は未確認アプリの警告を通して使える（100 人まで）。
  OAuth 同意画面が「テスト」状態だと更新トークンが 7 日で失効する。
"""
from __future__ import annotations

import base64

import httpx

from .base import AuthError, InboundMessage, MailError, OutgoingMessage, SendRejected, SendResult, SendUnknown, SyncError
from .base import build_mime, parse_raw
from .oauth import TokenCache, access_token

API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailProvider:
    def __init__(self, account: dict, secrets, http: httpx.Client | None = None):
        self.account = account
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self.sync_days = int((account.get("config") or {}).get("sync_days") or 14)

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(2):
            token = access_token(self.account, self.secrets, self.http, force=attempt == 1)
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                TokenCache.drop(self.account["id"])
                continue
            if r.status_code == 401:
                raise AuthError("Gmail の認証が無効です。再認証してください")
            return r
        raise AuthError("Gmail の認証が無効です")

    def check(self) -> str:
        try:
            r = self._request("GET", API + "/profile")
        except httpx.HTTPError as exc:
            raise MailError(f"Gmail に接続できません：{exc}") from exc
        if r.status_code != 200:
            raise MailError(f"Gmail の確認に失敗しました（{r.status_code}）")
        email = r.json().get("emailAddress", "")
        note = "" if email.lower() == self.account["from_address"].lower() else f"（注意：認証したアカウントは {email} です）"
        return f"Gmail に接続できました：{email}{note}"

    def send(self, msg: OutgoingMessage, on_prepared=None) -> SendResult:
        raw = base64.urlsafe_b64encode(build_mime(msg).as_bytes()).decode()
        body = {"raw": raw}
        if msg.thread_external_id:
            body["threadId"] = msg.thread_external_id
        if on_prepared:
            on_prepared(None, msg.message_id)
        try:
            r = self._request("POST", API + "/messages/send", json=body)
        except AuthError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SendRejected(f"Gmail に接続できませんでした：{exc}") from exc
        except httpx.HTTPError as exc:  # 要求を送った後の切断・タイムアウト
            raise SendUnknown(f"Gmail の応答がありませんでした。送信されたか分かりません：{exc}") from exc
        if r.status_code == 200:
            j = r.json()
            return SendResult(message_id_header=msg.message_id, provider_message_id=j.get("id"), provider_thread_id=j.get("threadId"))
        if r.status_code >= 500:
            raise SendUnknown(f"Gmail 側のエラー（{r.status_code}）のため、送信されたか分かりません")
        raise SendRejected(f"Gmail が送信を受け付けませんでした（{r.status_code}）：{r.text[:200]}")

    def _get_raw(self, mid: str) -> InboundMessage | None:
        r = self._request("GET", f"{API}/messages/{mid}", params={"format": "raw"})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise SyncError(f"メッセージを取得できません（{r.status_code}）")
        j = r.json()
        if "SENT" in (j.get("labelIds") or []) or "DRAFT" in (j.get("labelIds") or []):
            return None
        raw = base64.urlsafe_b64decode(j["raw"] + "=" * (-len(j["raw"]) % 4))
        return parse_raw(raw, f"gmail:{j['id']}", j.get("threadId"))

    def fetch_new(self, cursor: str | None) -> tuple[list[InboundMessage], str]:
        try:
            ids: list[str] = []
            if cursor:
                page = None
                while True:
                    params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "maxResults": 500}
                    if page:
                        params["pageToken"] = page
                    r = self._request("GET", API + "/history", params=params)
                    if r.status_code == 404:  # 取得位置が古すぎる → 期間を区切って再同期
                        cursor = None
                        ids = []
                        break
                    if r.status_code != 200:
                        raise SyncError(f"Gmail の差分取得に失敗しました（{r.status_code}）")
                    j = r.json()
                    for h in j.get("history", []):
                        for added in h.get("messagesAdded", []):
                            mid = added["message"]["id"]
                            if mid not in ids:
                                ids.append(mid)
                    new_cursor = j.get("historyId", cursor)
                    page = j.get("nextPageToken")
                    if not page:
                        break
                if cursor:
                    out = [m for m in (self._get_raw(i) for i in ids[:500]) if m]
                    return out, str(new_cursor)
            prof = self._request("GET", API + "/profile")
            if prof.status_code != 200:
                raise SyncError(f"Gmail の状態を取得できません（{prof.status_code}）")
            start = str(prof.json()["historyId"])
            r = self._request("GET", API + "/messages", params={"q": f"newer_than:{self.sync_days}d -in:sent -in:drafts",
                                                                 "maxResults": 200})
            if r.status_code != 200:
                raise SyncError(f"Gmail の一覧を取得できません（{r.status_code}）")
            out = [m for m in (self._get_raw(x["id"]) for x in r.json().get("messages", [])) if m]
            return out, start
        except httpx.HTTPError as exc:
            raise SyncError(f"Gmail と通信できません：{exc}") from exc

    def find_sent(self, message_id_header: str | None, provider_message_id: str | None) -> bool | None:
        if not message_id_header:
            return None
        try:
            r = self._request("GET", API + "/messages", params={"q": f"rfc822msgid:{message_id_header.strip('<>')} in:sent",
                                                                 "includeSpamTrash": "false"})
        except (httpx.HTTPError, MailError):
            return None
        if r.status_code != 200:
            return None
        return bool(r.json().get("messages"))
