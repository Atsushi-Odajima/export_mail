"""営業データの操作（担当者・案件・自社情報・テンプレート・下書き・承認・アカウント・会話・予約・連絡拒否）。

画面・送信処理・受信処理はここを通してデータを読み書きする。
- 下書きの宛先・件名・本文・送信元・添付が変わったら版を上げ、それまでの承認を無効にする。
- 承認は「その版の内容ハッシュ」に結び付き、内容が変わったものを送らない。
- 連絡拒否は範囲（会社・個人・アドレス・ドメイン・無効な宛先）ごとに記録し、再調査や再取込で消さない。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from . import ids
from .db import transaction, utcnow
from .runtime import audit

DEAL_STAGES = ["未着手", "初回送信予定", "送信済み・返信待ち", "返信あり・要対応", "商談中", "見送り", "成約", "連絡停止"]
CLOSED_STAGES = {"見送り", "成約", "連絡停止"}
STEPS = {"initial": "初回", "followup1": "2回目", "followup2": "3回目", "reactivation": "掘り起こし", "reply": "返信", "single": "個別"}
FOLLOWUP_STEPS = {"followup1", "followup2"}
SCHEDULE_ACTIVE = {"承認待ち", "予約済み", "一時停止", "保留"}
PROVIDERS = {"test": "テスト用接続先（外部へ送らない）", "smtp_imap": "SMTP 送信＋IMAP 受信", "gmail": "Gmail API",
             "graph": "Microsoft Graph（Outlook）"}
PROFILE_FIELDS = [
    ("company_name", "会社名（英語）", "例：Samurai Auto Ltd."),
    ("sender_name", "送信者名（英語）", "例：Taro Yamada"),
    ("sender_title", "役職（英語）", "例：Export Manager"),
    ("company_address", "所在地（英語）", ""),
    ("company_phone", "電話番号（国番号付き）", ""),
    ("company_website", "ウェブサイト", ""),
    ("company_email", "返信用メールアドレス", ""),
    ("intro_en", "自社紹介（英文・1〜3文）", "事実だけを書いてください。実績・価格・保証は確認できるものだけ"),
    ("vehicles_en", "対応車種（英文）", "例：Japanese domestic market (JDM) used cars"),
    ("services_en", "提供できるサービス（英文）", "例：sourcing from Japanese auctions, export documentation"),
    ("signature_en", "署名（英文・複数行可）", ""),
]
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}|\[\[[^\[\]]*\]\]|<<[^<>]*>>|\bTODO\b|XXX")


class SalesError(ValueError):
    pass


def _row(r) -> dict | None:
    return dict(r) if r else None


def split_addresses(value: str | None) -> list[str]:
    return [a.strip() for a in re.split(r"[,;\s]+", value or "") if a.strip()]


def validate_addresses(value: str | None, required: bool = False) -> list[str]:
    addrs = split_addresses(value)
    if required and not addrs:
        raise SalesError("宛先を入力してください")
    bad = [a for a in addrs if not EMAIL_RE.match(a)]
    if bad:
        raise SalesError(f"メールアドレスの形式が正しくありません：{', '.join(bad)}")
    return addrs


def find_placeholders(*texts: str) -> list[str]:
    found: list[str] = []
    for t in texts:
        for m in PLACEHOLDER_RE.findall(t or ""):
            if m not in found:
                found.append(m)
    return found


# ---------------------------------------------------------------- 担当者
def add_person(conn, dealer_id: str, name: str | None, role: str | None, email: str | None, phone: str | None,
               source_url: str | None, notes: str | None, actor: str | None) -> str:
    if email:
        validate_addresses(email)
    pid = ids.new_id(ids.PREFIX_PERSON)
    now = utcnow()
    conn.execute("INSERT INTO people(id, dealer_id, name, role, email, phone, source_url, notes, created_at, updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, dealer_id, name or None, role or None, (email or "").lower() or None,
                                                  phone or None, source_url or None, notes or None, now, now))
    audit(conn, actor, "担当者を追加", pid, dealer_id)
    return pid


def list_people(conn, dealer_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM people WHERE dealer_id=? ORDER BY created_at", (dealer_id,))]


# ---------------------------------------------------------------- 案件
def create_deal(conn, dealer_id: str, title: str, owner: str | None, actor: str | None, person_id: str | None = None) -> str:
    if not conn.execute("SELECT 1 FROM dealers WHERE id=?", (dealer_id,)).fetchone():
        raise SalesError("販売店が見つかりません")
    did = ids.new_id(ids.PREFIX_DEAL)
    now = utcnow()
    conn.execute("INSERT INTO deals(id, dealer_id, person_id, title, owner, stage, created_by, created_at, updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (did, dealer_id, person_id or None, title.strip() or "営業案件", owner or actor,
                                               "未着手", actor, now, now))
    audit(conn, actor, "案件を作成", did, dealer_id)
    return did


def get_deal(conn, deal_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT d.*, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name, x.website_url, x.archived, "
        "x.eligibility_manual FROM deals d JOIN dealers x ON x.id=d.dealer_id WHERE d.id=?", (deal_id,)).fetchone())


def list_deals(conn, stage: str = "", owner: str = "", dealer_id: str = "") -> list[dict]:
    sql = ("SELECT d.*, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name, "
           "(SELECT COUNT(*) FROM messages m WHERE m.deal_id=d.id AND m.needs_action=1 AND m.handled_at IS NULL) AS pending, "
           "(SELECT MIN(send_at_utc) FROM schedules s WHERE s.deal_id=d.id AND s.status IN ('予約済み','承認待ち')) AS next_send "
           "FROM deals d JOIN dealers x ON x.id=d.dealer_id WHERE 1=1")
    params: list[Any] = []
    if stage:
        sql += " AND d.stage=?"
        params.append(stage)
    if owner:
        sql += " AND d.owner=?"
        params.append(owner)
    if dealer_id:
        sql += " AND d.dealer_id=?"
        params.append(dealer_id)
    sql += " ORDER BY COALESCE(d.next_action_at, '9999'), d.updated_at DESC"
    return [dict(r) for r in conn.execute(sql, params)]


def update_deal(conn, deal_id: str, values: dict, actor: str | None) -> None:
    deal = get_deal(conn, deal_id)
    if not deal:
        raise SalesError("案件が見つかりません")
    allowed = {"title", "owner", "stage", "next_action_at", "next_action", "notes", "send_allowed", "send_basis", "person_id"}
    sets, params = [], []
    for k, v in values.items():
        if k not in allowed:
            continue
        if k == "stage" and v not in DEAL_STAGES:
            raise SalesError("案件の状態が不正です")
        if k == "send_allowed":
            v = 1 if str(v) in ("1", "true", "on") else 0
            if v and not (values.get("send_basis") or deal.get("send_basis")):
                raise SalesError("送信してよいと判断した根拠を入力してください（例：法人の公開営業窓口）")
        if deal.get(k) != v:
            sets.append(f"{k}=?")
            params.append(v if v != "" else None)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(utcnow())
    conn.execute(f"UPDATE deals SET {', '.join(sets)} WHERE id=?", (*params, deal_id))
    audit(conn, actor, "案件を更新", deal_id, ", ".join(s.split('=')[0] for s in sets[:-1]))
    new_stage = values.get("stage")
    if new_stage and new_stage != deal["stage"] and new_stage in CLOSED_STAGES:
        hold_schedules(conn, "deal_id=?", (deal_id,), f"案件の状態が「{new_stage}」に変わったため保留", cancel=True)


def set_stage_if(conn, deal_id: str, stage: str, only_from: Iterable[str] | None = None) -> None:
    deal = get_deal(conn, deal_id)
    if not deal or (only_from is not None and deal["stage"] not in only_from):
        return
    conn.execute("UPDATE deals SET stage=?, updated_at=? WHERE id=?", (stage, utcnow(), deal_id))


# ---------------------------------------------------------------- 自社情報
def get_profile(conn) -> dict[str, str]:
    values = {k: "" for k, _, _ in PROFILE_FIELDS}
    for r in conn.execute("SELECT key, value FROM company_profile"):
        values[r["key"]] = r["value"]
    return values


def save_profile(conn, values: dict, actor: str | None) -> None:
    for k, _, _ in PROFILE_FIELDS:
        if k in values:
            conn.execute("INSERT INTO company_profile(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, str(values[k]).strip()))
    audit(conn, actor, "自社情報を更新", "profile")


# ---------------------------------------------------------------- テンプレート
def list_templates(conn, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM templates" + (" WHERE active=1" if active_only else "") + " ORDER BY step, category, name"
    return [dict(r) for r in conn.execute(sql)]


def get_template(conn, tid: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone())


def save_template(conn, tid: str | None, name: str, step: str, category: str, subject: str, body: str,
                  actor: str | None) -> str:
    if step not in STEPS or step == "single":
        raise SalesError("テンプレートの段階が不正です")
    if not subject.strip() or not body.strip():
        raise SalesError("件名と本文を入力してください")
    now = utcnow()
    if tid:
        t = get_template(conn, tid)
        if not t:
            raise SalesError("テンプレートが見つかりません")
        if (t["subject"], t["body"]) != (subject, body) or t["name"] != name or t["step"] != step or t["category"] != category:
            ver = t["version"] + 1
            conn.execute("UPDATE templates SET name=?, step=?, category=?, subject=?, body=?, version=?, updated_at=? WHERE id=?",
                         (name, step, category, subject, body, ver, now, tid))
            conn.execute("INSERT INTO template_versions VALUES(?,?,?,?,?,?)", (tid, ver, subject, body, now, actor))
        return tid
    tid = ids.new_id(ids.PREFIX_TEMPLATE)
    conn.execute("INSERT INTO templates(id, name, step, category, subject, body, version, active, created_at, updated_at) "
                 "VALUES(?,?,?,?,?,?,1,1,?,?)", (tid, name, step, category, subject, body, now, now))
    conn.execute("INSERT INTO template_versions VALUES(?,?,?,?,?,?)", (tid, 1, subject, body, now, actor))
    return tid


def pick_template(conn, step: str, categories: list[str]) -> dict | None:
    for cat in list(categories) + ["共通"]:
        r = conn.execute("SELECT * FROM templates WHERE active=1 AND step=? AND category=? ORDER BY updated_at DESC LIMIT 1",
                         (step, cat)).fetchone()
        if r:
            return dict(r)
    return None


# ---------------------------------------------------------------- 下書き・承認
CONTENT_FIELDS = ("account_id", "to_address", "cc", "bcc", "subject", "body")


def get_draft(conn, draft_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def draft_attachments(conn, draft_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM attachments WHERE draft_id=? ORDER BY created_at", (draft_id,))]


def content_hash(conn, draft: dict) -> str:
    payload = {k: (draft.get(k) or "") for k in CONTENT_FIELDS}
    payload["attachments"] = [a["sha256"] for a in draft_attachments(conn, draft["id"])]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_draft(conn, deal_id: str, step: str, values: dict, source: str, actor: str | None, **meta) -> str:
    deal = get_deal(conn, deal_id)
    if not deal:
        raise SalesError("案件が見つかりません")
    if step not in STEPS:
        raise SalesError("文面の段階が不正です")
    did = ids.new_id(ids.PREFIX_DRAFT)
    now = utcnow()
    conn.execute(
        "INSERT INTO drafts(id, deal_id, dealer_id, step, account_id, to_contact_id, to_address, cc, bcc, subject, body, "
        "version, source, template_id, template_version, ai_usage_id, basis_json, summary_ja, reply_to_message_id, status, "
        "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,'下書き',?,?,?)",
        (did, deal_id, deal["dealer_id"], step, values.get("account_id") or None, values.get("to_contact_id") or None,
         values.get("to_address", ""), values.get("cc", ""), values.get("bcc", ""), values.get("subject", ""),
         values.get("body", ""), source, meta.get("template_id"), meta.get("template_version"), meta.get("ai_usage_id"),
         meta.get("basis_json"), meta.get("summary_ja"), meta.get("reply_to_message_id"), actor, now, now))
    conn.execute("INSERT INTO draft_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (did, 1, values.get("to_address", ""), values.get("cc", ""), values.get("bcc", ""), values.get("subject", ""),
                  values.get("body", ""), source, values.get("account_id"), now, actor))
    audit(conn, actor, "下書きを作成", did, f"{deal_id} {STEPS[step]} {source}")
    return did


def update_draft(conn, draft_id: str, values: dict, actor: str | None, source: str | None = None, **meta) -> bool:
    """内容が変わったら版を上げ、承認を無効にし、関連する予約を承認待ちに戻す。戻り値：版を上げたか。"""
    d = get_draft(conn, draft_id)
    if not d:
        raise SalesError("下書きが見つかりません")
    if d["status"] in ("送信済み",):
        raise SalesError("送信済みの文面は変更できません。複製して新しい下書きにしてください")
    changed = {k: values[k] for k in CONTENT_FIELDS if k in values and (values[k] or "") != (d.get(k) or "")}
    extra = {k: v for k, v in meta.items() if k in ("summary_ja", "basis_json", "ai_usage_id", "template_id", "template_version", "to_contact_id")}
    if not changed and not extra and not source:
        return False
    now = utcnow()
    new = {**d, **changed}
    if changed:
        ver = d["version"] + 1
        conn.execute(
            f"UPDATE drafts SET {', '.join(f'{k}=?' for k in changed)}, version=?, status='下書き', updated_at=?"
            + (", source=?" if source else "") + " WHERE id=?",
            (*changed.values(), ver, now, *([source] if source else []), draft_id))
        conn.execute("INSERT INTO draft_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (draft_id, ver, new["to_address"], new["cc"], new["bcc"], new["subject"], new["body"],
                      source or d["source"], new.get("account_id"), now, actor))
        revoke_approvals(conn, draft_id, "内容が変更されたため")
        audit(conn, actor, "下書きを変更", draft_id, f"版 {ver}：{', '.join(changed)}")
    elif source:
        conn.execute("UPDATE drafts SET source=?, updated_at=? WHERE id=?", (source, now, draft_id))
    for k, v in extra.items():
        conn.execute(f"UPDATE drafts SET {k}=? WHERE id=?", (v, draft_id))
    return bool(changed)


def revoke_approvals(conn, draft_id: str, reason: str) -> None:
    n = conn.execute("UPDATE approvals SET revoked_at=?, revoked_reason=? WHERE draft_id=? AND revoked_at IS NULL",
                     (utcnow(), reason, draft_id)).rowcount
    if n:
        conn.execute("UPDATE schedules SET status='承認待ち', hold_reason=?, updated_at=? WHERE draft_id=? "
                     "AND status IN ('予約済み','保留','一時停止')", (f"再承認が必要（{reason}）", utcnow(), draft_id))


def approval_problems(conn, draft: dict) -> list[str]:
    problems = []
    try:
        validate_addresses(draft["to_address"], required=True)
        validate_addresses(draft["cc"])
        validate_addresses(draft["bcc"])
    except SalesError as exc:
        problems.append(str(exc))
    if not draft["subject"].strip():
        problems.append("件名が空です")
    if not draft["body"].strip():
        problems.append("本文が空です")
    if not draft.get("account_id"):
        problems.append("送信元アカウントを選んでください")
    ph = find_placeholders(draft["subject"], draft["body"])
    if ph:
        problems.append("差し込みが完了していない箇所があります：" + "、".join(ph[:8]))
    return problems


def approve_draft(conn, draft_id: str, actor: str | None) -> int:
    d = get_draft(conn, draft_id)
    if not d:
        raise SalesError("下書きが見つかりません")
    if d["status"] == "送信済み":
        raise SalesError("送信済みです")
    problems = approval_problems(conn, d)
    if problems:
        raise SalesError("承認できません：" + " / ".join(problems))
    revoke_approvals_silent(conn, draft_id)
    cur = conn.execute("INSERT INTO approvals(draft_id, draft_version, content_hash, approved_by, approved_at) VALUES(?,?,?,?,?)",
                       (draft_id, d["version"], content_hash(conn, d), actor, utcnow()))
    conn.execute("UPDATE drafts SET status='承認済み', updated_at=? WHERE id=?", (utcnow(), draft_id))
    now = utcnow()
    for s in conn.execute("SELECT id, send_at_utc FROM schedules WHERE draft_id=? AND status='承認待ち'", (draft_id,)).fetchall():
        if s["send_at_utc"] <= now:
            conn.execute("UPDATE schedules SET status='保留', hold_reason=?, updated_at=? WHERE id=?",
                         ("承認時点で予定時刻を過ぎていました。日時を変更して再開してください", now, s["id"]))
        else:
            conn.execute("UPDATE schedules SET status='予約済み', hold_reason=NULL, updated_at=? WHERE id=?", (now, s["id"]))
    audit(conn, actor, "文面を承認", draft_id, f"版 {d['version']}")
    return cur.lastrowid


def revoke_approvals_silent(conn, draft_id: str) -> None:
    conn.execute("UPDATE approvals SET revoked_at=?, revoked_reason='新しい承認に置き換え' WHERE draft_id=? AND revoked_at IS NULL",
                 (utcnow(), draft_id))


def current_approval(conn, draft: dict) -> dict | None:
    """有効な承認（版と内容ハッシュが現在の下書きと一致するもの）。"""
    r = conn.execute("SELECT * FROM approvals WHERE draft_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
                     (draft["id"],)).fetchone()
    if not r or r["draft_version"] != draft["version"] or r["content_hash"] != content_hash(conn, draft):
        return None
    return dict(r)


def duplicate_draft(conn, draft_id: str, actor: str | None) -> str:
    d = get_draft(conn, draft_id)
    if not d:
        raise SalesError("下書きが見つかりません")
    return create_draft(conn, d["deal_id"], d["step"], {k: d[k] for k in ("account_id", "to_contact_id", "to_address", "cc", "bcc",
                                                                            "subject", "body")},
                        "手動", actor, basis_json=d["basis_json"], summary_ja=None)


def list_drafts(conn, deal_id: str | None = None, status: str = "") -> list[dict]:
    sql = ("SELECT r.*, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name FROM drafts r "
           "JOIN dealers x ON x.id=r.dealer_id WHERE 1=1")
    params: list[Any] = []
    if deal_id:
        sql += " AND r.deal_id=?"
        params.append(deal_id)
    if status:
        sql += " AND r.status=?"
        params.append(status)
    return [dict(r) for r in conn.execute(sql + " ORDER BY r.updated_at DESC LIMIT 300", params)]


# ---------------------------------------------------------------- 連絡拒否
def add_suppression(conn, scope: str, actor: str | None, source: str = "手動", status: str = "確定", dealer_id: str | None = None,
                    person_id: str | None = None, address: str | None = None, domain: str | None = None,
                    reason: str | None = None, message_id: str | None = None) -> str:
    if scope not in ("会社", "個人", "アドレス", "ドメイン", "無効な宛先"):
        raise SalesError("停止の範囲が不正です")
    if scope == "会社" and not dealer_id:
        raise SalesError("会社単位の停止には販売店が必要です")
    if scope in ("アドレス", "無効な宛先") and not address:
        raise SalesError("メールアドレスを指定してください")
    if scope == "ドメイン" and not domain:
        raise SalesError("ドメインを指定してください")
    address = (address or "").strip().lower() or None
    domain = (domain or "").strip().lower().lstrip("@") or None
    existing = conn.execute("SELECT id, status FROM suppressions WHERE scope=? AND COALESCE(dealer_id,'')=COALESCE(?, '') "
                            "AND COALESCE(address,'')=COALESCE(?, '') AND COALESCE(domain,'')=COALESCE(?, '') "
                            "AND COALESCE(person_id,'')=COALESCE(?, '') AND status!='解除'",
                            (scope, dealer_id, address, domain, person_id)).fetchone()
    if existing:
        if existing["status"] == "確認待ち" and status == "確定":
            conn.execute("UPDATE suppressions SET status='確定', resolved_at=?, resolved_by=? WHERE id=?",
                         (utcnow(), actor, existing["id"]))
        return existing["id"]
    sid = ids.new_id(ids.PREFIX_SUPPRESSION)
    conn.execute("INSERT INTO suppressions(id, scope, dealer_id, person_id, address, domain, status, reason, source, message_id, "
                 "created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, scope, dealer_id, person_id, address, domain, status, reason, source, message_id, utcnow(), actor))
    audit(conn, actor, f"送信停止を登録（{scope}・{status}）", sid, address or domain or dealer_id)
    # 該当する予約を保留にする
    if scope == "会社" and dealer_id:
        hold_schedules(conn, "dealer_id=?", (dealer_id,), f"会社単位の送信停止（{status}）")
    elif address:
        hold_schedules(conn, "draft_id IN (SELECT id FROM drafts WHERE lower(to_address) LIKE ? OR lower(cc) LIKE ? OR lower(bcc) LIKE ?)",
                       (f"%{address}%",) * 3, f"宛先 {address} の送信停止（{scope}・{status}）")
    elif domain:
        hold_schedules(conn, "draft_id IN (SELECT id FROM drafts WHERE lower(to_address) LIKE ?)", (f"%@{domain}%",),
                       f"ドメイン {domain} の送信停止")
    elif person_id:
        hold_schedules(conn, "deal_id IN (SELECT id FROM deals WHERE person_id=?)", (person_id,), "担当者の送信停止")
    return sid


def resolve_suppression(conn, sid: str, action: str, actor: str | None) -> None:
    if action not in ("確定", "解除"):
        raise SalesError("操作が不正です")
    conn.execute("UPDATE suppressions SET status=?, resolved_at=?, resolved_by=? WHERE id=?", (action, utcnow(), actor, sid))
    audit(conn, actor, f"送信停止を{action}", sid)


def blocking_suppressions(conn, dealer_id: str | None, addresses: list[str], person_id: str | None = None) -> list[str]:
    """送信を止める理由（確定・確認待ちの両方で止める）。"""
    reasons = []
    if dealer_id:
        for r in conn.execute("SELECT status, reason FROM suppressions WHERE scope='会社' AND dealer_id=? AND status IN ('確定','確認待ち')",
                              (dealer_id,)):
            reasons.append(f"会社単位の送信停止（{r['status']}）{('：' + r['reason']) if r['reason'] else ''}")
    for addr in addresses:
        a = addr.lower()
        dom = a.split("@")[-1]
        for r in conn.execute("SELECT scope, status FROM suppressions WHERE status IN ('確定','確認待ち') AND "
                              "((scope IN ('アドレス','無効な宛先') AND address=?) OR (scope='ドメイン' AND domain=?))", (a, dom)):
            reasons.append(f"{a}：{r['scope']}（{r['status']}）")
        for r in conn.execute("SELECT s.status FROM suppressions s JOIN people p ON p.id=s.person_id WHERE s.scope='個人' "
                              "AND s.status IN ('確定','確認待ち') AND lower(p.email)=?", (a,)):
            reasons.append(f"{a}：個人の送信停止（{r['status']}）")
    if person_id:
        for r in conn.execute("SELECT status FROM suppressions WHERE scope='個人' AND person_id=? AND status IN ('確定','確認待ち')",
                              (person_id,)):
            reasons.append(f"担当者の送信停止（{r['status']}）")
    return reasons


def list_suppressions(conn, status: str = "") -> list[dict]:
    sql = ("SELECT s.*, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name FROM suppressions s "
           "LEFT JOIN dealers x ON x.id=s.dealer_id")
    params: list[Any] = []
    if status:
        sql += " WHERE s.status=?"
        params.append(status)
    return [dict(r) for r in conn.execute(sql + " ORDER BY s.created_at DESC LIMIT 500", params)]


# ---------------------------------------------------------------- 予約
def hold_schedules(conn, where: str, params: tuple, reason: str, cancel: bool = False) -> int:
    status = "取消" if cancel else "保留"
    return conn.execute(f"UPDATE schedules SET status=?, hold_reason=?, updated_at=? WHERE ({where}) "
                        "AND status IN ('承認待ち','予約済み','一時停止')", (status, reason, utcnow(), *params)).rowcount


def get_schedule(conn, sid: str) -> dict | None:
    return _row(conn.execute(
        "SELECT s.*, r.subject, r.to_address, r.version AS draft_version, r.status AS draft_status, "
        "COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name, a.label AS account_label, a.provider "
        "FROM schedules s JOIN drafts r ON r.id=s.draft_id JOIN dealers x ON x.id=s.dealer_id "
        "LEFT JOIN mail_accounts a ON a.id=s.account_id WHERE s.id=?", (sid,)).fetchone())


def list_schedules(conn, status: str = "", deal_id: str = "", limit: int = 300) -> list[dict]:
    sql = ("SELECT s.*, r.subject, r.to_address, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name, "
           "a.label AS account_label, a.provider FROM schedules s JOIN drafts r ON r.id=s.draft_id "
           "JOIN dealers x ON x.id=s.dealer_id LEFT JOIN mail_accounts a ON a.id=s.account_id WHERE 1=1")
    params: list[Any] = []
    if status:
        sql += " AND s.status=?"
        params.append(status)
    if deal_id:
        sql += " AND s.deal_id=?"
        params.append(deal_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY s.send_at_utc LIMIT ?", (*params, limit))]


def create_schedule(conn, deal_id: str, draft_id: str, account_id: str, step: str, send_at_utc: str, input_tz: str,
                    actor: str | None, sequence_id: str | None = None) -> str:
    deal = get_deal(conn, deal_id)
    d = get_draft(conn, draft_id)
    if not deal or not d:
        raise SalesError("案件または文面が見つかりません")
    if not conn.execute("SELECT 1 FROM mail_accounts WHERE id=?", (account_id,)).fetchone():
        raise SalesError("送信元アカウントが見つかりません")
    if send_at_utc <= utcnow():
        raise SalesError("送信日時は現在より後にしてください")
    if d.get("account_id") != account_id:
        update_draft(conn, draft_id, {"account_id": account_id}, actor)
        d = get_draft(conn, draft_id)
    status = "予約済み" if current_approval(conn, d) else "承認待ち"
    sid = ids.new_id(ids.PREFIX_SCHEDULE)
    now = utcnow()
    conn.execute("INSERT INTO schedules(id, sequence_id, deal_id, dealer_id, draft_id, account_id, step, send_at_utc, input_tz, "
                 "status, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, sequence_id, deal_id, deal["dealer_id"], draft_id, account_id, step, send_at_utc, input_tz, status,
                  actor, now, now))
    if d["status"] == "下書き" and status == "予約済み":
        conn.execute("UPDATE drafts SET status='承認済み' WHERE id=?", (draft_id,))
    if deal["stage"] == "未着手":
        set_stage_if(conn, deal_id, "初回送信予定")
    audit(conn, actor, "送信を予約", sid, f"{STEPS.get(step, step)} {send_at_utc}")
    return sid


def create_sequence(conn, deal_id: str, name: str, stop_on_reply: bool, actor: str | None) -> str:
    sid = ids.new_id(ids.PREFIX_SEQUENCE)
    conn.execute("INSERT INTO sequences(id, deal_id, name, stop_on_reply, status, created_by, created_at) VALUES(?,?,?,?, '有効', ?, ?)",
                 (sid, deal_id, name, 1 if stop_on_reply else 0, actor, utcnow()))
    return sid


def schedule_action(conn, sid: str, action: str, actor: str | None, send_at_utc: str | None = None, tz: str | None = None) -> None:
    s = get_schedule(conn, sid)
    if not s:
        raise SalesError("予約が見つかりません")
    now = utcnow()
    if action == "cancel":
        if s["status"] in ("送信済み", "送信中", "結果確認待ち"):
            raise SalesError(f"状態が「{s['status']}」のため取消できません")
        conn.execute("UPDATE schedules SET status='取消', hold_reason='利用者が取消', updated_at=? WHERE id=?", (now, sid))
    elif action == "pause":
        if s["status"] not in ("予約済み", "承認待ち", "保留"):
            raise SalesError(f"状態が「{s['status']}」のため一時停止できません")
        conn.execute("UPDATE schedules SET status='一時停止', hold_reason='利用者が一時停止', updated_at=? WHERE id=?", (now, sid))
    elif action == "resume":
        if s["status"] not in ("一時停止", "保留", "送信前に失敗"):
            raise SalesError(f"状態が「{s['status']}」のため再開できません")
        if send_at_utc:
            if send_at_utc <= now:
                raise SalesError("送信日時は現在より後にしてください")
            conn.execute("UPDATE schedules SET send_at_utc=?, input_tz=COALESCE(?, input_tz) WHERE id=?", (send_at_utc, tz, sid))
        elif s["send_at_utc"] <= now:
            raise SalesError("予定時刻を過ぎています。新しい送信日時を指定して再開してください")
        d = get_draft(conn, s["draft_id"])
        status = "予約済み" if current_approval(conn, d) else "承認待ち"
        conn.execute("UPDATE schedules SET status=?, hold_reason=NULL, claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE id=?",
                     (status, now, sid))
    elif action == "reschedule":
        if s["status"] not in ("予約済み", "承認待ち", "一時停止", "保留"):
            raise SalesError(f"状態が「{s['status']}」のため日時を変更できません")
        if not send_at_utc or send_at_utc <= now:
            raise SalesError("送信日時は現在より後にしてください")
        conn.execute("UPDATE schedules SET send_at_utc=?, input_tz=COALESCE(?, input_tz), updated_at=? WHERE id=?",
                     (send_at_utc, tz, now, sid))
    elif action == "duplicate":
        nd = duplicate_draft(conn, s["draft_id"], actor)
        later = send_at_utc or s["send_at_utc"]
        if later <= now:
            raise SalesError("複製する予約の日時を現在より後で指定してください")
        create_schedule(conn, s["deal_id"], nd, s["account_id"], s["step"], later, tz or s["input_tz"], actor, s["sequence_id"])
    else:
        raise SalesError("不明な操作です")
    audit(conn, actor, f"予約を操作（{action}）", sid)


# ---------------------------------------------------------------- アカウント
def list_accounts(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM mail_accounts ORDER BY created_at"):
        d = dict(r)
        d["config"] = json.loads(d["config_json"] or "{}")
        d["provider_label"] = PROVIDERS.get(d["provider"], d["provider"])
        out.append(d)
    return out


def get_account(conn, aid: str) -> dict | None:
    r = conn.execute("SELECT * FROM mail_accounts WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["config"] = json.loads(d["config_json"] or "{}")
    d["provider_label"] = PROVIDERS.get(d["provider"], d["provider"])
    return d


def save_account(conn, aid: str | None, provider: str, label: str, from_address: str, from_name: str, config: dict,
                 signature: str, daily_limit: int, actor: str | None) -> str:
    if provider not in PROVIDERS:
        raise SalesError("接続方式が不正です")
    validate_addresses(from_address, required=True)
    if not (1 <= int(daily_limit) <= 2000):
        raise SalesError("1日の送信上限は 1〜2000 の範囲にしてください")
    now = utcnow()
    cfg = json.dumps(config, ensure_ascii=False)
    if aid:
        conn.execute("UPDATE mail_accounts SET label=?, from_address=?, from_name=?, config_json=?, signature=?, daily_limit=?, "
                     "updated_at=? WHERE id=?", (label, from_address, from_name, cfg, signature, int(daily_limit), now, aid))
        audit(conn, actor, "メール接続を変更", aid)
        return aid
    aid = ids.new_id(ids.PREFIX_ACCOUNT)
    conn.execute("INSERT INTO mail_accounts(id, provider, label, from_address, from_name, config_json, signature, daily_limit, "
                 "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (aid, provider, label, from_address, from_name, cfg, signature, int(daily_limit), now, now))
    audit(conn, actor, "メール接続を追加", aid, provider)
    return aid


def set_account_status(conn, aid: str, status: str, detail: str | None = None) -> None:
    conn.execute("UPDATE mail_accounts SET status=?, status_detail=?, updated_at=? WHERE id=?", (status, (detail or "")[:500], utcnow(), aid))


# ---------------------------------------------------------------- 会話
def deal_messages(conn, deal_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM messages WHERE deal_id=? ORDER BY COALESCE(sent_at, received_at, created_at)", (deal_id,))]


def dealer_messages(conn, dealer_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM messages WHERE dealer_id=? ORDER BY COALESCE(sent_at, received_at, created_at)", (dealer_id,))]


def inbox(conn, only_pending: bool = True, kind: str = "") -> list[dict]:
    sql = ("SELECT m.*, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name FROM messages m "
           "LEFT JOIN dealers x ON x.id=m.dealer_id WHERE m.direction='in'")
    params: list[Any] = []
    if only_pending:
        sql += " AND m.needs_action=1 AND m.handled_at IS NULL"
    if kind:
        sql += " AND m.kind=?"
        params.append(kind)
    return [dict(r) for r in conn.execute(sql + " ORDER BY COALESCE(m.received_at, m.created_at) DESC LIMIT 300", params)]


def get_message(conn, mid: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())


# ---------------------------------------------------------------- ダッシュボード
def dashboard(conn) -> dict:
    one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    return {
        "dealers": one("SELECT COUNT(*) FROM dealers WHERE archived=0 AND parent_dealer_id IS NULL"),
        "unreviewed": one("SELECT COUNT(*) FROM dealers WHERE archived=0 AND review_status!='確認済み'"),
        "deals": one("SELECT COUNT(*) FROM deals WHERE stage NOT IN ('見送り','成約','連絡停止')"),
        "drafts": one("SELECT COUNT(*) FROM drafts WHERE status='下書き'"),
        "approval_wait": one("SELECT COUNT(*) FROM schedules WHERE status='承認待ち'"),
        "scheduled": one("SELECT COUNT(*) FROM schedules WHERE status='予約済み'"),
        "held": one("SELECT COUNT(*) FROM schedules WHERE status IN ('保留','結果確認待ち','送信前に失敗')"),
        "sent_7d": one("SELECT COUNT(*) FROM send_attempts WHERE result IN ('受付済み','照合で受付確認','人が送信済みと判断') "
                       "AND started_at >= datetime('now','-7 days')"),
        "unknown": one("SELECT COUNT(*) FROM send_attempts WHERE result='結果不明'"),
        "new_replies": one("SELECT COUNT(*) FROM messages WHERE direction='in' AND kind='返信' AND handled_at IS NULL"),
        "pending": one("SELECT COUNT(*) FROM messages WHERE direction='in' AND needs_action=1 AND handled_at IS NULL"),
        "suppress_wait": one("SELECT COUNT(*) FROM suppressions WHERE status='確認待ち'"),
        "next_actions": [dict(r) for r in conn.execute(
            "SELECT d.id, d.title, d.next_action_at, d.next_action, COALESCE(x.name_manual, x.name_auto, x.website_host) AS dealer_name "
            "FROM deals d JOIN dealers x ON x.id=d.dealer_id WHERE d.next_action_at IS NOT NULL "
            "AND d.stage NOT IN ('見送り','成約','連絡停止') ORDER BY d.next_action_at LIMIT 10")],
    }


def with_tx(conn):
    return transaction(conn)
