"""メール接続の共通部分：型・例外・メールの組み立てと読み取り。

送信結果は 3 種類に分ける。
- 成功（SendResult）：送信サービスが受け付けた。相手への配達・開封を意味しない。
- SendRejected：送信前に失敗した（認証・宛先拒否・相手サーバーの明確なエラー等）。送っていないことが確か。
- SendUnknown：結果が分からない（本文送信中の切断・タイムアウト・サーバー側エラー）。自動で再送しない。
"""
from __future__ import annotations

import html as html_mod
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parsedate_to_datetime


class MailError(Exception):
    pass


class AuthError(MailError):
    """認証できない（期限切れ・取り消し・パスワード誤り）。再認証が必要。"""


class SendRejected(MailError):
    """送信前に失敗した（送っていないことが確か）。"""


class SendUnknown(MailError):
    """送信結果が分からない。外部の送信履歴と照合するまで再送しない。"""


class SyncError(MailError):
    pass


@dataclass
class OutgoingMessage:
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body_text: str
    message_id: str
    in_reply_to: str | None = None
    references: str | None = None
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)  # (ファイル名, 種類, 中身)
    thread_external_id: str | None = None      # Gmail の threadId 等
    reply_to_external_id: str | None = None    # Graph で返信を作る元のメッセージ ID
    list_unsubscribe: str | None = None

    @property
    def envelope_recipients(self) -> list[str]:
        return [*self.to, *self.cc, *self.bcc]


@dataclass
class SendResult:
    message_id_header: str
    provider_message_id: str | None = None
    provider_thread_id: str | None = None


@dataclass
class InboundMessage:
    external_id: str
    message_id: str | None
    in_reply_to: str | None
    references: str | None
    from_addr: str
    to_addrs: str
    cc_addrs: str
    subject: str
    body_text: str
    date: str | None
    headers: dict = field(default_factory=dict)       # 自動応答判定に使う一部のヘッダー（小文字キー）
    external_thread_id: str | None = None
    dsn_recipients: list[str] = field(default_factory=list)  # 不達通知に書かれた宛先
    dsn_original_message_id: str | None = None
    is_dsn: bool = False


def new_message_id(from_addr: str) -> str:
    domain = from_addr.split("@")[-1] if "@" in from_addr else "samurai.local"
    return make_msgid(idstring=secrets.token_hex(6), domain=domain)


def build_mime(msg: OutgoingMessage) -> EmailMessage:
    m = EmailMessage(policy=policy.SMTP)
    m["From"] = formataddr((msg.from_name, msg.from_addr)) if msg.from_name else msg.from_addr
    m["To"] = ", ".join(msg.to)
    if msg.cc:
        m["Cc"] = ", ".join(msg.cc)
    # BCC はヘッダーに入れない（他の宛先に見えない）。送信時の宛先にだけ使う
    m["Subject"] = msg.subject
    m["Date"] = format_datetime(datetime.now(timezone.utc))
    m["Message-ID"] = msg.message_id
    if msg.in_reply_to:
        m["In-Reply-To"] = msg.in_reply_to
    if msg.references:
        m["References"] = msg.references
    if msg.list_unsubscribe:
        m["List-Unsubscribe"] = msg.list_unsubscribe
    m.set_content(msg.body_text, subtype="plain", charset="utf-8")
    for filename, ctype, data in msg.attachments:
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        m.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)
    return m


def _addr_list(value) -> str:
    if not value:
        return ""
    return ", ".join(a for _, a in getaddresses([str(value)]) if a)


def html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond", "precedence", "x-auto-response-suppress",
                "content-type", "x-failed-recipients", "return-path")


def parse_raw(raw: bytes, external_id: str, external_thread_id: str | None = None, max_body: int = 200_000) -> InboundMessage:
    """受信メールを読み取る。添付やリンクは実行・取得しない（本文の文字だけを保存する）。"""
    m = BytesParser(policy=policy.default).parsebytes(raw)
    body = ""
    is_dsn = m.get_content_type() == "multipart/report"
    dsn_recipients: list[str] = []
    original_mid = None
    try:
        part = m.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = html_to_text(content) if part.get_content_type() == "text/html" else content
    except Exception:
        body = ""
    for part in m.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            is_dsn = True
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else [payload]
            for block in blocks:
                text = block.as_string() if hasattr(block, "as_string") else str(block)
                for mm in re.finditer(r"(?im)^(?:Final|Original)-Recipient:\s*[^;]*;\s*<?([^>\s]+@[^>\s]+)>?", text):
                    if mm.group(1).lower() not in dsn_recipients:
                        dsn_recipients.append(mm.group(1).lower())
        elif ctype in ("message/rfc822", "text/rfc822-headers"):
            text = part.as_string()
            mm = re.search(r"(?im)^Message-ID:\s*(<[^>]+>)", text)
            if mm:
                original_mid = mm.group(1)
    for value in m.get_all("X-Failed-Recipients", []) or []:
        for a in re.findall(r"[\w.%+'-]+@[\w.-]+\.\w+", str(value)):
            if a.lower() not in dsn_recipients:
                dsn_recipients.append(a.lower())
    if not original_mid and is_dsn:
        mm = re.search(r"(?im)Message-ID:\s*(<[^>]+>)", body)
        if mm:
            original_mid = mm.group(1)
    date = None
    try:
        if m["Date"]:
            date = parsedate_to_datetime(str(m["Date"])).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        date = None
    headers = {h: str(m[h]) for h in AUTO_HEADERS if m[h] is not None}
    from_addr = _addr_list(m["From"]).split(",")[0].strip().lower()
    return InboundMessage(
        external_id=external_id, message_id=(str(m["Message-ID"]).strip() if m["Message-ID"] else None),
        in_reply_to=(str(m["In-Reply-To"]).strip() if m["In-Reply-To"] else None),
        references=(str(m["References"]).strip() if m["References"] else None),
        from_addr=from_addr, to_addrs=_addr_list(m["To"]).lower(), cc_addrs=_addr_list(m["Cc"]).lower(),
        subject=str(m["Subject"] or ""), body_text=(body or "")[:max_body], date=date, headers=headers,
        external_thread_id=external_thread_id, dsn_recipients=dsn_recipients, dsn_original_message_id=original_mid,
        is_dsn=is_dsn)
