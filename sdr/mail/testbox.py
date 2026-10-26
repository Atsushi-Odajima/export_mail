"""テスト用メール接続先（外部へは一切送らない）。

送信したメールはデータベースの test_mailbox（送信箱）に保存するだけ。
画面から、その送信に対する疑似返信・自動応答・不達通知・配信停止・拒否・別アドレスからの返信・
重複配信・古い日付の返信を受信箱に入れ、受信処理の動きを確かめられる。
アカウント設定の「疑似動作」で、送信前エラー・送信結果不明・認証エラー・同期エラーも再現できる。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from ..db import utcnow
from .base import (AuthError, InboundMessage, OutgoingMessage, SendRejected, SendResult, SendUnknown, SyncError,
                   build_mime, parse_raw)

SIMULATIONS = ["正常", "送信前エラー", "送信結果不明", "認証エラー", "同期エラー"]
INJECT_KINDS = {
    "reply": "通常の返信",
    "reply_other": "同じ会社の別アドレスからの返信（返信参照あり）",
    "new_same_domain": "同じドメインからの新規メール（返信参照なし）",
    "auto": "自動応答（不在通知）",
    "bounce": "不達通知（宛先不明）",
    "unsubscribe": "配信停止の依頼",
    "refusal": "拒否の疑い（not interested）",
    "duplicate": "直前の受信と同じメールをもう一度配信",
    "old": "古い日付の返信（受信順の逆転）",
}


class TestProvider:
    def __init__(self, account: dict, secrets, conn_factory):
        self.account = account
        self.conn_factory = conn_factory
        self.simulate = (account.get("config") or {}).get("simulate", "正常")

    def _conn(self):
        if not self.conn_factory:
            raise SyncError("テスト用接続先のデータベースに接続できません")
        return self.conn_factory()

    def check(self) -> str:
        if self.simulate == "認証エラー":
            raise AuthError("（疑似）認証エラー：再認証が必要です")
        return "テスト用接続先です。外部へは送信しません"

    def send(self, msg: OutgoingMessage, on_prepared=None) -> SendResult:
        if self.simulate == "認証エラー":
            raise AuthError("（疑似）認証エラー")
        if self.simulate == "送信前エラー":
            raise SendRejected("（疑似）送信前エラー：宛先サーバーが受け付けませんでした")
        raw = build_mime(msg).as_bytes()
        conn = self._conn()
        try:
            cur = conn.execute("INSERT INTO test_mailbox(account_id, folder, raw, message_id_header, created_at) VALUES(?,?,?,?,?)",
                               (self.account["id"], "sent", raw.decode("utf-8", "replace"), msg.message_id, utcnow()))
            row_id = cur.lastrowid
        finally:
            conn.close()
        if on_prepared:
            on_prepared(f"test-{row_id}", msg.message_id)
        if self.simulate == "送信結果不明":
            # 送信箱には入っている（相手に届いた可能性がある）が、応答が返らなかった状態を再現
            raise SendUnknown("（疑似）送信結果不明：本文送信後に応答がありませんでした")
        return SendResult(message_id_header=msg.message_id, provider_message_id=f"test-{row_id}",
                          provider_thread_id=msg.thread_external_id or msg.message_id)

    def fetch_new(self, cursor: str | None) -> tuple[list[InboundMessage], str]:
        if self.simulate == "認証エラー":
            raise AuthError("（疑似）認証エラー：同期できません")
        if self.simulate == "同期エラー":
            raise SyncError("（疑似）同期エラー：受信サーバーに接続できません")
        last = int(cursor or 0)
        conn = self._conn()
        try:
            rows = conn.execute("SELECT id, raw FROM test_mailbox WHERE account_id=? AND folder='inbox' AND id>? ORDER BY id",
                                (self.account["id"], last)).fetchall()
        finally:
            conn.close()
        out = [parse_raw(r["raw"].encode("utf-8"), f"testbox-{r['id']}") for r in rows]
        return out, str(rows[-1]["id"] if rows else last)

    def find_sent(self, message_id_header: str | None, provider_message_id: str | None) -> bool | None:
        if not message_id_header:
            return None
        conn = self._conn()
        try:
            row = conn.execute("SELECT 1 FROM test_mailbox WHERE account_id=? AND folder='sent' AND message_id_header=?",
                               (self.account["id"], message_id_header)).fetchone()
        finally:
            conn.close()
        return bool(row)


def inject(conn, account: dict, kind: str, sent_raw: str | None, recipient: str | None, body: str | None = None) -> str:
    """テスト用の受信を作って受信箱に入れる。戻り値は作った Message-ID。"""
    if kind not in INJECT_KINDS:
        raise ValueError("不明な種類です")
    if kind == "duplicate":
        row = conn.execute("SELECT raw, message_id_header FROM test_mailbox WHERE account_id=? AND folder='inbox' ORDER BY id DESC LIMIT 1",
                           (account["id"],)).fetchone()
        if not row:
            raise ValueError("まだ受信がありません")
        conn.execute("INSERT INTO test_mailbox(account_id, folder, raw, message_id_header, created_at) VALUES(?,?,?,?,?)",
                     (account["id"], "inbox", row["raw"], row["message_id_header"], utcnow()))
        return row["message_id_header"]
    if not sent_raw or not recipient:
        raise ValueError("元になる送信メールを選んでください")
    orig = parse_raw(sent_raw.encode("utf-8"), "orig")
    our = account["from_address"]
    domain = recipient.split("@")[-1]
    m = EmailMessage(policy=policy.SMTP)
    when = datetime.now(timezone.utc) - (timedelta(days=3) if kind == "old" else timedelta(0))
    sender = recipient
    if kind in ("reply_other", "new_same_domain"):
        sender = f"colleague@{domain}"
    if kind == "bounce":
        sender = f"MAILER-DAEMON@{domain}"
    m["From"] = sender
    m["To"] = our
    m["Date"] = format_datetime(when)
    mid = make_msgid(domain=domain)
    m["Message-ID"] = mid
    subj = orig.subject or ""
    if kind not in ("new_same_domain", "bounce"):
        m["In-Reply-To"] = orig.message_id or ""
        m["References"] = ((orig.references or "") + " " + (orig.message_id or "")).strip()
    texts = {
        "reply": "Hello,\n\nThanks for your email. We might be interested. Could you send more details about pricing?\n\nBest regards",
        "reply_other": "Hi,\n\nMy colleague forwarded your message. Please send us a list of available vehicles.\n\nRegards",
        "new_same_domain": "Hello,\n\nWe heard about your company. Do you supply Japanese vans?\n\nThanks",
        "auto": "I am out of the office until next Monday with limited access to email.",
        "unsubscribe": "Please unsubscribe us and do not contact us again.",
        "refusal": "Thanks, but we are not interested at the moment.",
        "old": "Hello,\n\nSorry for the late reply. Yes, please send more information.\n\nKind regards",
    }
    if kind == "auto":
        m["Auto-Submitted"] = "auto-replied"
        m["Subject"] = "Automatic reply: " + subj
    elif kind == "new_same_domain":
        m["Subject"] = "Enquiry about Japanese vehicles"
    elif kind != "bounce":
        m["Subject"] = "Re: " + subj
    if kind == "bounce":
        # 不達通知（RFC 3464 の multipart/report 形式）を文字列で組み立てる
        boundary = "bounce-" + mid.strip("<>").split("@")[0]
        raw = (f"From: {sender}\r\nTo: {our}\r\nDate: {m['Date']}\r\nMessage-ID: {mid}\r\n"
               "Subject: Undelivered Mail Returned to Sender\r\nMIME-Version: 1.0\r\n"
               f'Content-Type: multipart/report; report-type=delivery-status; boundary="{boundary}"\r\n\r\n'
               f"--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               f"This is the mail system. Your message to <{recipient}> could not be delivered: user unknown.\r\n\r\n"
               f"--{boundary}\r\nContent-Type: message/delivery-status\r\n\r\n"
               f"Reporting-MTA: dns; mx.{domain}\r\n\r\nFinal-Recipient: rfc822; {recipient}\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n"
               f"--{boundary}\r\nContent-Type: text/rfc822-headers\r\n\r\n"
               f"Message-ID: {orig.message_id}\r\nSubject: {subj}\r\n\r\n--{boundary}--\r\n")
        conn.execute("INSERT INTO test_mailbox(account_id, folder, raw, message_id_header, created_at) VALUES(?,?,?,?,?)",
                     (account["id"], "inbox", raw, mid, utcnow()))
        return mid
    m.set_content(body or texts.get(kind, texts["reply"]))
    raw = m.as_string()
    conn.execute("INSERT INTO test_mailbox(account_id, folder, raw, message_id_header, created_at) VALUES(?,?,?,?,?)",
                 (account["id"], "inbox", raw, mid, utcnow()))
    return mid
