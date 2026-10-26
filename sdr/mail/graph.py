"""Microsoft Graph（Outlook.com / Microsoft 365）。

公式資料（2026-10-26 確認）：
- 送信は「下書きを作る（POST /me/messages → 201、id と internetMessageId）→ 送る（POST /me/messages/{id}/send → 202）」。
  sendMail は ID を返さないため使わない。202 は受付であり配達ではない。
- ID はフォルダー移動で変わるため、Prefer: IdType="ImmutableId" で不変 ID を使う。
- 受信は /me/mailFolders/inbox/messages/delta（@odata.deltaLink を保存）。410 や syncStateNotFound は全体の再同期。
- 公開クライアント（PKCE・シークレットなし）、受け口は http://localhost（ポートは照合時に無視される）。
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx

from .base import (AuthError, InboundMessage, MailError, OutgoingMessage, SendRejected, SendResult, SendUnknown,
                   SyncError, html_to_text)
from .oauth import TokenCache, access_token

API = "https://graph.microsoft.com/v1.0/me"
PREFER = 'IdType="ImmutableId", outlook.body-content-type="text"'
SELECT = "id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,body,isDraft"


def _recips(addrs: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in addrs]


class GraphProvider:
    def __init__(self, account: dict, secrets, http: httpx.Client | None = None):
        self.account = account
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self.sync_days = int((account.get("config") or {}).get("sync_days") or 14)

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        headers = {"Prefer": PREFER, **kw.pop("headers", {})}
        for attempt in range(2):
            token = access_token(self.account, self.secrets, self.http, force=attempt == 1)
            r = self.http.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                TokenCache.drop(self.account["id"])
                continue
            if r.status_code == 401:
                raise AuthError("Microsoft の認証が無効です。再認証してください")
            return r
        raise AuthError("Microsoft の認証が無効です")

    def check(self) -> str:
        try:
            r = self._request("GET", API, params={"$select": "mail,userPrincipalName"})
        except httpx.HTTPError as exc:
            raise MailError(f"Microsoft Graph に接続できません：{exc}") from exc
        if r.status_code != 200:
            raise MailError(f"Microsoft Graph の確認に失敗しました（{r.status_code}）")
        j = r.json()
        addr = j.get("mail") or j.get("userPrincipalName") or ""
        note = "" if addr.lower() == self.account["from_address"].lower() else f"（注意：認証したアカウントは {addr} です）"
        return f"Microsoft Graph に接続できました：{addr}{note}"

    def send(self, msg: OutgoingMessage, on_prepared=None) -> SendResult:
        payload = {"subject": msg.subject, "body": {"contentType": "Text", "content": msg.body_text},
                   "toRecipients": _recips(msg.to), "ccRecipients": _recips(msg.cc), "bccRecipients": _recips(msg.bcc)}
        try:
            if msg.reply_to_external_id:  # 会話の中の返信として作る
                r = self._request("POST", f"{API}/messages/{msg.reply_to_external_id}/createReply")
                if r.status_code not in (200, 201):
                    raise SendRejected(f"返信の下書きを作れませんでした（{r.status_code}）")
                draft = r.json()
                r = self._request("PATCH", f"{API}/messages/{draft['id']}", json=payload)
                if r.status_code != 200:
                    raise SendRejected(f"返信の下書きを更新できませんでした（{r.status_code}）")
                draft = r.json()
            else:
                r = self._request("POST", f"{API}/messages", json=payload)
                if r.status_code != 201:
                    raise SendRejected(f"下書きを作れませんでした（{r.status_code}）：{r.text[:200]}")
                draft = r.json()
            for filename, ctype, data in msg.attachments:
                ra = self._request("POST", f"{API}/messages/{draft['id']}/attachments", json={
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": filename, "contentType": ctype,
                    "contentBytes": base64.b64encode(data).decode()})
                if ra.status_code not in (200, 201):
                    raise SendRejected(f"添付を追加できませんでした（{ra.status_code}）")
        except (httpx.HTTPError,) as exc:
            raise SendRejected(f"Microsoft Graph と通信できませんでした（送信前）：{exc}") from exc
        mid = draft.get("internetMessageId") or msg.message_id
        if on_prepared:
            on_prepared(draft["id"], mid)
        try:
            r = self._request("POST", f"{API}/messages/{draft['id']}/send")
        except AuthError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SendRejected(f"Microsoft Graph に接続できませんでした：{exc}") from exc
        except httpx.HTTPError as exc:
            raise SendUnknown(f"送信の応答がありませんでした。送信されたか分かりません：{exc}") from exc
        if r.status_code == 202:
            return SendResult(message_id_header=mid, provider_message_id=draft["id"],
                              provider_thread_id=draft.get("conversationId"))
        if r.status_code >= 500:
            raise SendUnknown(f"Microsoft 側のエラー（{r.status_code}）のため、送信されたか分かりません")
        raise SendRejected(f"Microsoft Graph が送信を受け付けませんでした（{r.status_code}）：{r.text[:200]}")

    def _headers(self, mid: str) -> dict:
        r = self._request("GET", f"{API}/messages/{mid}", params={"$select": "internetMessageHeaders"})
        if r.status_code != 200:
            return {}
        return {h["name"].lower(): h["value"] for h in r.json().get("internetMessageHeaders", []) or []}

    def _to_inbound(self, m: dict) -> InboundMessage | None:
        if m.get("isDraft") or "@removed" in m:
            return None
        hdrs = self._headers(m["id"])
        body = (m.get("body") or {}).get("content", "")
        if (m.get("body") or {}).get("contentType", "").lower() == "html":
            body = html_to_text(body)
        frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        to = ", ".join(r["emailAddress"]["address"].lower() for r in m.get("toRecipients", []))
        cc = ", ".join(r["emailAddress"]["address"].lower() for r in m.get("ccRecipients", []))
        dsn = []
        if hdrs.get("x-failed-recipients"):
            dsn = [a.strip().lower() for a in hdrs["x-failed-recipients"].split(",")]
        return InboundMessage(
            external_id=f"graph:{m['id']}", message_id=m.get("internetMessageId"), in_reply_to=hdrs.get("in-reply-to"),
            references=hdrs.get("references"), from_addr=frm, to_addrs=to, cc_addrs=cc, subject=m.get("subject") or "",
            body_text=body[:200_000], date=m.get("receivedDateTime"),
            headers={k: v for k, v in hdrs.items() if k in ("auto-submitted", "x-autoreply", "x-autorespond", "precedence",
                                                            "x-auto-response-suppress", "content-type")},
            external_thread_id=m.get("conversationId"), dsn_recipients=dsn,
            is_dsn="multipart/report" in hdrs.get("content-type", "") or frm.startswith(("postmaster@", "mailer-daemon@")))

    def fetch_new(self, cursor: str | None) -> tuple[list[InboundMessage], str]:
        try:
            if cursor and cursor.startswith("https://graph.microsoft.com/"):
                url, params = cursor, None
            else:
                since = (datetime.now(timezone.utc) - timedelta(days=self.sync_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                url = f"{API}/mailFolders/inbox/messages/delta"
                params = {"$select": SELECT, "changeType": "created", "$filter": f"receivedDateTime ge {since}"}
            out: list[InboundMessage] = []
            delta_link = cursor or ""
            for _ in range(200):
                r = self._request("GET", url, params=params, headers={"Prefer": PREFER + ", odata.maxpagesize=50"})
                if r.status_code == 410 or (r.status_code >= 400 and "syncStateNotFound" in r.text):
                    return self.fetch_new(None)  # 取得位置が無効 → 期間を区切って再同期（重複は ID で除く）
                if r.status_code != 200:
                    raise SyncError(f"Outlook の差分取得に失敗しました（{r.status_code}）")
                j = r.json()
                for m in j.get("value", []):
                    ib = self._to_inbound(m)
                    if ib:
                        out.append(ib)
                if j.get("@odata.nextLink"):
                    url, params = j["@odata.nextLink"], None
                    continue
                delta_link = j.get("@odata.deltaLink", delta_link)
                break
            return out, delta_link
        except httpx.HTTPError as exc:
            raise SyncError(f"Microsoft Graph と通信できません：{exc}") from exc

    def find_sent(self, message_id_header: str | None, provider_message_id: str | None) -> bool | None:
        """不変 ID でメッセージを確認：送信済みフォルダーにあれば送信済み、下書きのままなら未送信。"""
        if not provider_message_id:
            return None
        try:
            r = self._request("GET", f"{API}/messages/{provider_message_id}", params={"$select": "isDraft,parentFolderId"})
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                return None
            j = r.json()
            if j.get("isDraft"):
                return False
            sent = self._request("GET", f"{API}/mailFolders/sentitems", params={"$select": "id"})
            if sent.status_code == 200 and sent.json().get("id") == j.get("parentFolderId"):
                return True
            return None
        except (httpx.HTTPError, MailError):
            return None
