"""受信の同期・分類・案件への関連付け・予約への反映。

関連付けの順番（件名だけで関連付けることはしない）：
  1. In-Reply-To / References に、このアプリが送ったメールの Message-ID がある → 返信参照
  2. 外部サービスの会話 ID（Gmail の threadId・Outlook の conversationId）が一致 → 会話ID
  3. 送信元アドレスが、販売店の連絡先・担当者・送信済みの宛先と完全に一致 → 送信元アドレス一致
  4. 送信元ドメインが販売店のサイト・連絡先のドメインと一致（フリーメールは除く） → 送信元ドメイン一致（要確認）
  どれにも当たらない受信は保存しない（営業と関係のない私信を取り込まない）。

分類：不達通知 / 自動応答 / 配信停止要求（明確な依頼のみ） / 拒否の疑い（曖昧な表現） / 返信。
明確な配信停止は送信元アドレスを停止（確定）し、会社全体は「確認待ち」で止める。曖昧な拒否は保留にして人が確認する。
返信を受けたら、その案件の追客（2回目・3回目）を取り消す。過去に返信した会社への再営業は、担当者が別の予定として作る。
"""
from __future__ import annotations

import logging
import re

from . import runtime, sales
from .db import connect, transaction, utcnow
from .ids import PREFIX_MESSAGE, PREFIX_THREAD, new_id
from .mail import AuthError, InboundMessage, get_provider

log = logging.getLogger("sdr.inbound")

FREEMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "live.co.uk", "yahoo.com",
            "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
            "protonmail.com", "proton.me", "gmx.com", "mail.com"}
UNSUB_RE = re.compile(
    r"\b(unsubscribe|opt[- ]?out|remove (me|us|our (email|address|company))( from)?|take (me|us) off|"
    r"(do not|don't|dont|please stop) (contact|email|e-mail|emailing|mail|sending)|stop (emailing|sending|contacting)|"
    r"no (further|more) (emails|e-mails|contact))\b", re.I)
REFUSAL_RE = re.compile(
    r"\b(not interested|no,? thank(s| you)|we('re| are) not (looking|buying|interested)|no need|not for us|"
    r"we (don't|do not) (import|buy|need)|no requirement|not at this time|please don't|we'?ll pass)\b", re.I)
AUTO_SUBJ_RE = re.compile(r"(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|auto[- ]?response|on (annual )?leave|"
                          r"on holiday|away from (the )?office|abwesenheit|absence)", re.I)
BOUNCE_SUBJ_RE = re.compile(r"(undeliver|delivery status notification|mail delivery (failed|failure|subsystem)|returned mail|"
                            r"delivery failure|failure notice|non[- ]delivery)", re.I)


def strip_quoted(body: str) -> str:
    """引用部分（> で始まる行・「On ... wrote:」以降・転送ヘッダー以降）を除いた新しい本文。"""
    lines = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^(on .+wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+|sent from my )", s, re.I):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def classify(ib: InboundMessage) -> str:
    frm = ib.from_addr.lower()
    if ib.is_dsn or frm.startswith(("mailer-daemon@", "postmaster@")) or BOUNCE_SUBJ_RE.search(ib.subject or ""):
        return "不達通知"
    h = {k.lower(): (v or "").lower() for k, v in (ib.headers or {}).items()}
    auto = (h.get("auto-submitted", "no") not in ("", "no")) or "x-autoreply" in h or "x-autorespond" in h \
        or h.get("precedence") in ("auto_reply", "bulk", "junk") or bool(AUTO_SUBJ_RE.search(ib.subject or ""))
    if auto:
        return "自動応答"
    new_text = strip_quoted(ib.body_text)
    if UNSUB_RE.search(new_text) or UNSUB_RE.search(ib.subject or ""):
        return "配信停止要求"
    if REFUSAL_RE.search(new_text):
        return "拒否の疑い"
    return "返信"


def _domain(addr: str) -> str:
    return addr.split("@")[-1].lower().strip() if "@" in addr else ""


def _host_domain(url: str | None) -> str:
    m = re.match(r"https?://([^/:]+)", url or "")
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def link(conn, account: dict, ib: InboundMessage) -> tuple[dict | None, str | None]:
    """(参照先の送信メッセージ or 販売店情報, 関連付け方法)。"""
    refs = re.findall(r"<[^>]+>", " ".join(x for x in (ib.in_reply_to, ib.references, ib.dsn_original_message_id) if x))
    for ref in reversed(refs):
        r = conn.execute("SELECT * FROM messages WHERE message_id_header=? AND dealer_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                         (ref,)).fetchone()
        if r:
            return dict(r), "返信参照"
    if ib.external_thread_id:
        r = conn.execute("SELECT * FROM messages WHERE account_id=? AND external_thread_id=? AND dealer_id IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT 1", (account["id"], ib.external_thread_id)).fetchone()
        if r:
            return dict(r), "会話ID"
    candidates = [ib.from_addr] + (ib.dsn_recipients or [])
    for addr in candidates:
        a = addr.lower()
        r = conn.execute("SELECT * FROM messages WHERE direction='out' AND (lower(to_addrs) LIKE ? OR lower(cc_addrs) LIKE ?) "
                         "ORDER BY sent_at DESC LIMIT 1", (f"%{a}%", f"%{a}%")).fetchone()
        if r:
            return dict(r), "送信元アドレス一致"
        r = conn.execute("SELECT dealer_id FROM contact_methods WHERE kind='email' AND lower(value)=? LIMIT 1", (a,)).fetchone() \
            or conn.execute("SELECT dealer_id FROM people WHERE lower(email)=? LIMIT 1", (a,)).fetchone()
        if r:
            return {"dealer_id": r["dealer_id"], "deal_id": None}, "送信元アドレス一致"
    dom = _domain(ib.from_addr)
    if dom and dom not in FREEMAIL:
        for row in conn.execute("SELECT id, website_url FROM dealers WHERE archived=0"):
            if _host_domain(row["website_url"]) == dom:
                return {"dealer_id": row["id"], "deal_id": None}, "送信元ドメイン一致（要確認）"
        r = conn.execute("SELECT dealer_id FROM contact_methods WHERE kind='email' AND lower(value) LIKE ? LIMIT 1",
                         (f"%@{dom}",)).fetchone()
        if r:
            return {"dealer_id": r["dealer_id"], "deal_id": None}, "送信元ドメイン一致（要確認）"
    return None, None


def store(conn, account: dict, ib: InboundMessage) -> dict | None:
    """1 通を保存して反映する。保存しなかった（重複・自分の送信・無関係）場合は None。"""
    if ib.from_addr.lower() == account["from_address"].lower():
        return None
    dup = conn.execute("SELECT 1 FROM messages WHERE account_id=? AND (external_id=? OR (message_id_header IS NOT NULL AND "
                       "message_id_header=? AND direction='in'))", (account["id"], ib.external_id, ib.message_id or "")).fetchone()
    if dup:
        return None
    ref, method = link(conn, account, ib)
    if not ref:
        return None
    kind = classify(ib)
    dealer_id = ref.get("dealer_id")
    deal_id = ref.get("deal_id")
    if not deal_id and dealer_id:
        r = conn.execute("SELECT id FROM deals WHERE dealer_id=? ORDER BY updated_at DESC LIMIT 1", (dealer_id,)).fetchone()
        deal_id = r["id"] if r else None
    thread_id = ref.get("thread_id")
    if not thread_id:
        thread_id = new_id(PREFIX_THREAD)
        conn.execute("INSERT INTO threads(id, account_id, dealer_id, deal_id, subject, external_thread_id, last_message_at, created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (thread_id, account["id"], dealer_id, deal_id, ib.subject, ib.external_thread_id,
                                                 ib.date or utcnow(), utcnow()))
    else:
        conn.execute("UPDATE threads SET last_message_at=MAX(COALESCE(last_message_at,''), ?) WHERE id=?", (ib.date or utcnow(), thread_id))
    needs = 0 if kind == "自動応答" else 1
    mid = new_id(PREFIX_MESSAGE)
    conn.execute(
        "INSERT INTO messages(id, thread_id, account_id, direction, message_id_header, in_reply_to, references_hdr, external_id, "
        "external_thread_id, from_addr, to_addrs, cc_addrs, subject, body_text, received_at, dealer_id, deal_id, kind, link_method, "
        "needs_action, created_at) VALUES(?,?,?,'in',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, thread_id, account["id"], ib.message_id, ib.in_reply_to, ib.references, ib.external_id, ib.external_thread_id,
         ib.from_addr, ib.to_addrs, ib.cc_addrs, ib.subject, ib.body_text, ib.date or utcnow(), dealer_id, deal_id, kind, method,
         needs, utcnow()))
    apply_effects(conn, mid, kind, method, dealer_id, deal_id, ib)
    return {"id": mid, "kind": kind, "link": method, "dealer_id": dealer_id, "deal_id": deal_id}


def apply_effects(conn, message_id: str, kind: str, method: str, dealer_id: str | None, deal_id: str | None,
                  ib: InboundMessage) -> None:
    actor = "受信処理"
    sure = method in ("返信参照", "会話ID", "送信元アドレス一致")
    if kind == "不達通知":
        for addr in ib.dsn_recipients or []:
            sales.add_suppression(conn, "無効な宛先", actor, source="不達通知", status="確定", dealer_id=dealer_id, address=addr,
                                  reason="不達通知を受信", message_id=message_id)
        if not ib.dsn_recipients and dealer_id:
            sales.hold_schedules(conn, "dealer_id=?", (dealer_id,), "不達通知を受信（宛先を特定できないため確認待ち）")
        return
    if kind == "自動応答":
        return
    if not dealer_id:
        return
    if kind == "配信停止要求":
        sales.add_suppression(conn, "アドレス", actor, source="返信", status="確定", dealer_id=dealer_id, address=ib.from_addr,
                              reason="配信停止の依頼を受信", message_id=message_id)
        sales.add_suppression(conn, "会社", actor, source="返信", status="確認待ち", dealer_id=dealer_id,
                              reason="配信停止の依頼（会社全体かどうか確認待ち）", message_id=message_id)
    elif kind == "拒否の疑い":
        sales.hold_schedules(conn, "dealer_id=?", (dealer_id,), "拒否の可能性がある返信を受信（確認待ち）")
    if kind in ("返信", "配信停止要求", "拒否の疑い"):
        if deal_id and sure:
            # 最初の営業への追客を止める（取消）。返信後の新しい再営業は担当者が別の予定として作る
            conn.execute("UPDATE schedules SET status='取消', hold_reason=?, updated_at=? WHERE deal_id=? AND step IN "
                         "('followup1','followup2') AND status IN ('承認待ち','予約済み','一時停止','保留')",
                         (f"返信を受信したため追客を停止（{message_id}）", utcnow(), deal_id))
            sales.set_stage_if(conn, deal_id, "返信あり・要対応",
                               only_from=("未着手", "初回送信予定", "送信済み・返信待ち", "商談中"))
            conn.execute("UPDATE deals SET next_action_at=?, next_action=COALESCE(next_action, '受信した返信を確認'), updated_at=? "
                         "WHERE id=?", (utcnow(), utcnow(), deal_id))
        sales.hold_schedules(conn, "dealer_id=?", (dealer_id,), f"新しい受信（{kind}）の確認待ち")


class SyncService:
    def __init__(self, data_dir, db_path, secret_store, http=None):
        self.data_dir = data_dir
        self.db_path = db_path
        self.secrets = secret_store
        self.http = http
        self.provider_override = None

    def conn(self):
        return connect(self.db_path)

    def provider(self, account):
        if self.provider_override:
            return self.provider_override(account)
        return get_provider(account, self.secrets, self.conn, self.http)

    def sync_account(self, account_id: str) -> dict:
        conn = self.conn()
        try:
            account = sales.get_account(conn, account_id)
        finally:
            conn.close()
        if not account:
            return {"ok": False, "message": "アカウントが見つかりません"}
        try:
            messages, cursor = self.provider(account).fetch_new(account.get("sync_cursor"))
        except AuthError as exc:
            self._mark(account_id, ok=False, status="認証エラー", error=str(exc))
            return {"ok": False, "message": f"認証エラー：{exc}"}
        except Exception as exc:
            self._mark(account_id, ok=False, status="同期エラー", error=str(exc))
            return {"ok": False, "message": f"同期エラー：{exc}"}
        stored = []
        conn = self.conn()
        try:
            with transaction(conn):
                for ib in sorted(messages, key=lambda m: m.date or ""):
                    r = store(conn, account, ib)
                    if r:
                        stored.append(r)
                conn.execute("UPDATE mail_accounts SET sync_cursor=?, last_sync_at=?, last_sync_ok_at=?, sync_error=NULL, "
                             "status=CASE WHEN status IN ('同期エラー','認証エラー','未接続') THEN '接続確認済み' ELSE status END, "
                             "updated_at=? WHERE id=?", (cursor, utcnow(), utcnow(), utcnow(), account_id))
                if stored:
                    runtime.audit(conn, "受信処理", "受信を同期", account_id, f"{len(stored)} 件を保存")
        finally:
            conn.close()
        return {"ok": True, "message": f"受信を確認しました（新しく保存 {len(stored)} 件・取得 {len(messages)} 件）", "stored": stored}

    def _mark(self, account_id: str, ok: bool, status: str, error: str) -> None:
        conn = self.conn()
        try:
            conn.execute("UPDATE mail_accounts SET last_sync_at=?, sync_error=?, status=?, status_detail=?, updated_at=? WHERE id=?",
                         (utcnow(), error[:500], status, error[:500], utcnow(), account_id))
            runtime.audit(conn, "受信処理", f"受信の同期に失敗（{status}）", account_id, error[:300])
        finally:
            conn.close()
