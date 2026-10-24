"""データ操作の集約（画面・取得処理から SQL を直接書かない）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from . import classify as C
from .config import DEFAULT_SETTINGS, SETTING_LIMITS
from .db import transaction, utcnow
from .extract import is_aggregator
from .fetcher import Cache, FetchResult, compress, decompress
from .ids import PREFIX_CANDIDATE, PREFIX_CONTACT, PREFIX_DEALER, PREFIX_JOB, PREFIX_RUN, new_id
from .netsafe import UnsafeURLError, check_url_syntax, host_key, normalize_url

MANUAL_FIELDS = ["name", "region", "address", "categories", "category_basis", "summary"]
CONTACT_KINDS = {"email": "メール", "form": "問い合わせフォーム", "contact_page": "問い合わせページ", "phone": "電話"}
REVIEW_STATUSES = ["未確認", "確認済み", "要確認"]


class RepoError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> str:
    if key not in DEFAULT_SETTINGS:
        raise RepoError(f"不明な設定です: {key}")
    lo, hi = SETTING_LIMITS[key]
    try:
        num = float(value)
    except ValueError as exc:
        raise RepoError(f"{key} には数値を入力してください") from exc
    num = min(max(num, lo), hi)
    text = str(int(num)) if isinstance(lo, int) and isinstance(hi, int) else str(num)
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, text))
    return text


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO schema_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


# ---------------------------------------------------------------------------
# 変更履歴
# ---------------------------------------------------------------------------
def log_change(conn, entity_type: str, entity_id: str, fieldname: str, old: Any, new: Any, origin: str,
               by: str | None = None) -> None:
    def norm(v):
        if v is None:
            return None
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if norm(old) == norm(new):
        return
    conn.execute(
        "INSERT INTO change_log(entity_type, entity_id, field, old_value, new_value, origin, changed_at, changed_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, fieldname, norm(old), norm(new), origin, utcnow(), by),
    )


# ---------------------------------------------------------------------------
# 販売店
# ---------------------------------------------------------------------------
def effective(row: sqlite3.Row | dict, name: str) -> Any:
    manual = row[f"{name}_manual"]
    value = manual if manual not in (None, "") else row[f"{name}_auto"]
    if name == "categories":
        try:
            return json.loads(value) if value else []
        except ValueError:
            return []
    return value


def validate_site_url(url: str) -> str:
    try:
        url = normalize_url(url)
        check_url_syntax(url)
    except UnsafeURLError as exc:
        raise RepoError(str(exc)) from exc
    if is_aggregator(url):
        raise RepoError("車両掲載サイト・検索サイト・SNS 等は販売店の公式サイトとして登録できません")
    if host_key(url).endswith(".ie"):
        raise RepoError("アイルランド（.ie）のサイトは対象外です。英国の事業者であれば手動で確認してください")
    return url


def find_by_host(conn, url: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM dealers WHERE website_host=? ORDER BY created_at LIMIT 1",
                        (host_key(url),)).fetchone()


def create_dealer(conn, url: str, origin: str, name: str | None = None, region: str | None = None,
                  notes: str | None = None, dealer_id: str | None = None, by: str | None = None) -> tuple[str, bool, str]:
    """戻り値 (販売店ID, 新規作成したか, 説明)。同じドメインは既存の販売店を返す（重複登録しない）。"""
    url = validate_site_url(url)
    existing = find_by_host(conn, url)
    if existing:
        return existing["id"], False, f"同じドメインの販売店が登録済みです（{existing['id']}）"
    now = utcnow()
    did = dealer_id or new_id(PREFIX_DEALER)
    conn.execute(
        "INSERT INTO dealers(id, created_at, updated_at, website_url, website_host, name_manual, region_manual, notes, "
        "data_origin) VALUES(?,?,?,?,?,?,?,?,?)",
        (did, now, now, url, host_key(url), name or None, region or None, notes or None, origin),
    )
    log_change(conn, "dealer", did, "created", None, url, "手動" if origin == "利用者追加" else "取込", by)
    _flag_similar_name(conn, did, name)
    return did, True, "登録しました"


def _norm_name(name: str | None) -> str:
    import re
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"\b(ltd|limited|llp|plc|the|cars?|motors?|motor company|automotive|uk|group|of)\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def _flag_similar_name(conn, dealer_id: str, name: str | None) -> None:
    """似た社名は自動統合せず「重複確認待ち」にする。"""
    key = _norm_name(name)
    if len(key) < 4:
        return
    for row in conn.execute("SELECT id, name_auto, name_manual, duplicate_status FROM dealers WHERE id<>? AND archived=0",
                            (dealer_id,)):
        other = _norm_name(row["name_manual"] or row["name_auto"])
        if other and other == key:
            conn.execute("UPDATE dealers SET duplicate_status='重複確認待ち', duplicate_of=? WHERE id=? "
                         "AND (duplicate_status IS NULL OR duplicate_status<>'別事業者と確認済み')",
                         (row["id"], dealer_id))
            log_change(conn, "dealer", dealer_id, "duplicate_status", None, f"重複確認待ち（{row['id']} と社名が類似）", "システム")
            return


def _flag_same_phone(conn, dealer_id: str) -> None:
    rows = conn.execute("SELECT value_norm FROM contact_methods WHERE dealer_id=? AND kind='phone'", (dealer_id,)).fetchall()
    for r in rows:
        other = conn.execute(
            "SELECT c.dealer_id FROM contact_methods c JOIN dealers d ON d.id=c.dealer_id "
            "WHERE c.kind='phone' AND c.value_norm=? AND c.dealer_id<>? AND d.archived=0 LIMIT 1",
            (r["value_norm"], dealer_id)).fetchone()
        if other:
            cur = conn.execute("SELECT duplicate_status, parent_dealer_id FROM dealers WHERE id=?", (dealer_id,)).fetchone()
            if cur["duplicate_status"] is None and cur["parent_dealer_id"] is None:
                conn.execute("UPDATE dealers SET duplicate_status='重複確認待ち', duplicate_of=? WHERE id=?",
                             (other["dealer_id"], dealer_id))
                log_change(conn, "dealer", dealer_id, "duplicate_status", None,
                           f"重複確認待ち（{other['dealer_id']} と同じ電話番号）", "システム")
            return


def get_dealer(conn, dealer_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM dealers WHERE id=?", (dealer_id,)).fetchone()
    return dealer_view(conn, row) if row else None


def dealer_view(conn, row: sqlite3.Row, contacts: list[dict] | None = None) -> dict:
    d = dict(row)
    for f in MANUAL_FIELDS:
        d[f] = effective(row, f)
        d[f + "_is_manual"] = row[f + "_manual"] not in (None, "")
    d["categories_auto_list"] = json.loads(row["categories_auto"]) if row["categories_auto"] else []
    d["categories_manual_list"] = json.loads(row["categories_manual"]) if row["categories_manual"] else None
    if contacts is None:
        contacts = list_contacts(conn, row["id"])
    d["contacts"] = contacts
    current = [c for c in contacts if c["is_current"]]
    d["emails_ok"] = [c for c in current if c["kind"] == "email" and c["status"] == C.EMAIL_STATUS_OK
                      and not c["purpose"].startswith("除外")]
    d["emails_review"] = [c for c in current if c["kind"] == "email" and c["status"] == C.EMAIL_STATUS_REVIEW]
    d["forms_ok"] = [c for c in current if c["kind"] == "form" and c["status"] == "フォーム確認済み"]
    d["contact_pages"] = [c for c in current if c["kind"] in ("contact_page", "form") and c["status"] != "フォーム確認済み"]
    d["phones"] = [c for c in current if c["kind"] == "phone"]
    d["has_email"] = bool(d["emails_ok"])
    d["has_form"] = bool(d["forms_ok"])
    if d["has_email"] and d["has_form"]:
        d["contact_summary"] = "メール・フォーム確認済み"
    elif d["has_email"]:
        d["contact_summary"] = "公開メール確認済み"
    elif d["has_form"]:
        d["contact_summary"] = "フォーム確認済み"
    elif d["contact_pages"] or d["emails_review"]:
        d["contact_summary"] = "要手動確認"
    elif row["research_status"] in ("取得失敗",):
        d["contact_summary"] = "取得失敗"
    elif row["research_status"] in ("未調査", "待機中", "調査中"):
        d["contact_summary"] = "未調査"
    else:
        d["contact_summary"] = "公開メール未確認"
    d["needs_review"] = (
        row["review_status"] != "確認済み"
        or C.CAT_REVIEW in d["categories"]
        or row["duplicate_status"] == "重複確認待ち"
        or any(c["status"] in ("要手動確認",) for c in current)
    )
    d["used_sales"] = row["used_sales_auto"]
    d["eligibility"] = row["eligibility_manual"] or (
        "対象（自動判定）" if row["used_sales_auto"] == "中古車販売を確認" and (row["uk_check_auto"] or "").startswith("英国")
        and "英国外" not in (row["uk_check_auto"] or "") else "要確認")
    return d


def list_dealers(conn, filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    rows = conn.execute("SELECT * FROM dealers ORDER BY created_at").fetchall()
    contacts_by: dict[str, list[dict]] = {}
    for c in conn.execute("SELECT * FROM contact_methods ORDER BY created_at"):
        contacts_by.setdefault(c["dealer_id"], []).append(_contact_view(c))
    out = []
    q = (filters.get("q") or "").strip().lower()
    region = (filters.get("region") or "").strip().lower()
    category = filters.get("category") or ""
    for row in rows:
        d = dealer_view(conn, row, contacts_by.get(row["id"], []))
        if not filters.get("include_archived") and row["archived"]:
            continue
        if q and q not in " ".join(str(x or "") for x in (d["name"], row["website_url"], row["notes"], row["id"])).lower():
            continue
        if region and region not in (d["region"] or "").lower() and region not in (d["address"] or "").lower():
            continue
        if category and category not in d["categories"]:
            continue
        if filters.get("has_email") and not d["has_email"]:
            continue
        if filters.get("has_form") and not d["has_form"]:
            continue
        if filters.get("needs_review") and not d["needs_review"]:
            continue
        if filters.get("status") and row["research_status"] != filters["status"]:
            continue
        if filters.get("origin") and row["data_origin"] != filters["origin"]:
            continue
        out.append(d)
    return out


def stats(conn) -> dict:
    dealers = [d for d in list_dealers(conn) if not d["parent_dealer_id"]]
    cat_counts = {c: 0 for c in C.CATEGORIES}
    for d in dealers:
        for c in d["categories"]:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    return {
        "total": len(dealers),
        "reviewed": sum(1 for d in dealers if d["review_status"] == "確認済み"),
        "with_email": sum(1 for d in dealers if d["has_email"]),
        "with_form": sum(1 for d in dealers if d["has_form"]),
        "with_email_or_form": sum(1 for d in dealers if d["has_email"] or d["has_form"]),
        "unconfirmed_contact": sum(1 for d in dealers if not d["has_email"] and not d["has_form"]),
        "failed": sum(1 for d in dealers if d["research_status"] == "取得失敗"),
        "categories": cat_counts,
        "branches": conn.execute("SELECT COUNT(*) FROM dealers WHERE parent_dealer_id IS NOT NULL").fetchone()[0],
    }


def update_manual(conn, dealer_id: str, values: dict[str, Any], by: str | None = None) -> None:
    """人の確定値を保存（空欄にすると確定値を解除し、自動取得値の表示に戻す）。"""
    row = conn.execute("SELECT * FROM dealers WHERE id=?", (dealer_id,)).fetchone()
    if not row:
        raise RepoError("販売店が見つかりません")
    sets, params = [], []
    for key, value in values.items():
        if key in MANUAL_FIELDS:
            col = f"{key}_manual"
            if key == "categories":
                cats = [c for c in (value or []) if c in C.CATEGORIES]
                new = json.dumps(cats, ensure_ascii=False) if cats else None
            elif key == "category_basis":
                new = value if value in C.BASES else None
            else:
                new = (value or "").strip() or None
        elif key in ("notes", "eligibility_manual"):
            col = key
            new = (value or "").strip() or None
        elif key == "website_url":
            col = key
            new = validate_site_url(value)
            other = find_by_host(conn, new)
            if other and other["id"] != dealer_id:
                raise RepoError(f"同じドメインの販売店が既にあります（{other['id']}）")
            sets.append("website_host=?")
            params.append(host_key(new))
        elif key == "parent_dealer_id":
            col = key
            new = value or None
            if new == dealer_id:
                raise RepoError("自分自身を本体に指定できません")
            if new and not conn.execute("SELECT 1 FROM dealers WHERE id=?", (new,)).fetchone():
                raise RepoError("本体の販売店IDが見つかりません")
        elif key in ("duplicate_status", "archived"):
            col = key
            new = value
        else:
            raise RepoError(f"編集できない項目です: {key}")
        if row[col] != new:
            log_change(conn, "dealer", dealer_id, col, row[col], new, "手動", by)
            sets.append(f"{col}=?")
            params.append(new)
    if sets:
        sets.append("updated_at=?")
        params.append(utcnow())
        conn.execute(f"UPDATE dealers SET {', '.join(sets)} WHERE id=?", (*params, dealer_id))


def set_review(conn, dealer_id: str, status: str, by: str | None = None) -> None:
    if status not in REVIEW_STATUSES:
        raise RepoError("確認状態が不正です")
    row = conn.execute("SELECT review_status FROM dealers WHERE id=?", (dealer_id,)).fetchone()
    if not row:
        raise RepoError("販売店が見つかりません")
    log_change(conn, "dealer", dealer_id, "review_status", row["review_status"], status, "手動", by)
    conn.execute("UPDATE dealers SET review_status=?, reviewed_at=?, reviewed_by=?, updated_at=? WHERE id=?",
                 (status, utcnow(), by, utcnow(), dealer_id))


def set_research_status(conn, dealer_id: str, status: str) -> None:
    conn.execute("UPDATE dealers SET research_status=?, updated_at=? WHERE id=?", (status, utcnow(), dealer_id))


# ---------------------------------------------------------------------------
# 連絡方法
# ---------------------------------------------------------------------------
def _contact_view(row: sqlite3.Row) -> dict:
    c = dict(row)
    c["status"] = row["status_manual"] or row["status_auto"]
    c["status_is_manual"] = bool(row["status_manual"])
    c["kind_label"] = CONTACT_KINDS.get(row["kind"], row["kind"])
    c["purpose"] = row["purpose"] or ""
    return c


def list_contacts(conn, dealer_id: str) -> list[dict]:
    return [_contact_view(r) for r in conn.execute(
        "SELECT * FROM contact_methods WHERE dealer_id=? ORDER BY is_current DESC, kind, created_at", (dealer_id,))]


def normalize_contact(kind: str, value: str) -> str:
    import re
    value = value.strip()
    if kind == "email":
        return value.lower()
    if kind == "phone":
        digits = re.sub(r"[^\d+]", "", value)
        if digits.startswith("+44"):
            digits = "0" + digits[3:].lstrip("0")
        return digits
    return value.rstrip("/").lower()


def add_contact_manual(conn, dealer_id: str, kind: str, value: str, source_url: str | None, note: str | None,
                       person_name: str | None = None, person_role: str | None = None, by: str | None = None) -> str:
    if kind not in CONTACT_KINDS:
        raise RepoError("連絡方法の種類が不正です")
    value = (value or "").strip()
    if not value:
        raise RepoError("値を入力してください")
    if kind == "email":
        from .extract import valid_email
        if not valid_email(value):
            raise RepoError("メールアドレスの形式が正しくありません")
    if kind in ("form", "contact_page") or source_url:
        for u in filter(None, [value if kind in ("form", "contact_page") else None, source_url]):
            try:
                check_url_syntax(normalize_url(u))
            except UnsafeURLError as exc:
                raise RepoError(f"URLが不正です: {exc}") from exc
    norm = normalize_contact(kind, value)
    existing = conn.execute("SELECT id FROM contact_methods WHERE dealer_id=? AND kind=? AND value_norm=?",
                            (dealer_id, kind, norm)).fetchone()
    now = utcnow()
    status = {"email": C.EMAIL_STATUS_OK, "form": "フォーム確認済み", "contact_page": "問い合わせページあり・フォーム未確認",
              "phone": "公開番号確認済み"}[kind]
    if existing:
        conn.execute("UPDATE contact_methods SET status_manual=?, human_verified_at=?, human_verified_by=?, is_current=1, "
                     "person_name=COALESCE(?, person_name), person_role=COALESCE(?, person_role) WHERE id=?",
                     (status, now, by, person_name or None, person_role or None, existing["id"]))
        log_change(conn, "contact", existing["id"], "status_manual", None, status, "手動", by)
        return existing["id"]
    cid = new_id(PREFIX_CONTACT)
    conn.execute(
        "INSERT INTO contact_methods(id, dealer_id, kind, value, value_norm, purpose, status_auto, status_manual, person_name, "
        "person_role, source_url, excerpt, origin, first_observed_at, last_observed_at, human_verified_at, human_verified_by, "
        "is_current, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (cid, dealer_id, kind, value, norm, "手動登録", None, status, person_name or None, person_role or None,
         source_url or None, note or None, "手動", now, now, now, by, now),
    )
    if source_url or note:
        conn.execute("INSERT INTO evidence(dealer_id, contact_method_id, topic, source_url, excerpt, observed_at, origin) "
                     "VALUES(?,?,?,?,?,?, '手動')", (dealer_id, cid, CONTACT_KINDS[kind], source_url, note, now))
    log_change(conn, "contact", cid, "created", None, f"{kind}:{value}", "手動", by)
    return cid


CONTACT_MANUAL_STATUSES = ["公開メール確認済み", "フォーム確認済み", "問い合わせページあり・フォーム未確認", "要手動確認",
                           "除外", "公開番号確認済み", "取得失敗"]


def set_contact_status(conn, contact_id: str, status: str | None, by: str | None = None, note: str | None = None) -> str:
    row = conn.execute("SELECT * FROM contact_methods WHERE id=?", (contact_id,)).fetchone()
    if not row:
        raise RepoError("連絡方法が見つかりません")
    if status and status not in CONTACT_MANUAL_STATUSES:
        raise RepoError("状態が不正です")
    log_change(conn, "contact", contact_id, "status_manual", row["status_manual"], status, "手動", by)
    conn.execute("UPDATE contact_methods SET status_manual=?, human_verified_at=?, human_verified_by=?, "
                 "exclusion_reason=CASE WHEN ? IS NOT NULL THEN ? ELSE exclusion_reason END WHERE id=?",
                 (status or None, utcnow() if status else None, by if status else None, note, note, contact_id))
    return row["dealer_id"]


# ---------------------------------------------------------------------------
# 調査結果の反映（手動確定値は上書きしない）
# ---------------------------------------------------------------------------
def start_run(conn, dealer_id: str, job_id: str | None) -> str:
    rid = new_id(PREFIX_RUN)
    conn.execute("INSERT INTO research_runs(id, dealer_id, job_id, started_at, status) VALUES(?,?,?,?, '調査中')",
                 (rid, dealer_id, job_id, utcnow()))
    set_research_status(conn, dealer_id, "調査中")
    return rid


def apply_research(conn, dealer_id: str, run_id: str, result) -> None:
    now = utcnow()
    pages_json = json.dumps([p.__dict__ for p in result.pages], ensure_ascii=False)
    with transaction(conn):
        row = conn.execute("SELECT * FROM dealers WHERE id=?", (dealer_id,)).fetchone()
        conn.execute(
            "UPDATE research_runs SET finished_at=?, status=?, pages_attempted=?, pages_ok=?, error_summary=?, pages_json=? WHERE id=?",
            (now, result.status, len(result.pages), sum(1 for p in result.pages if p.status in ("取得", "キャッシュ")),
             result.failure_reason or None, pages_json, run_id))
        if not result.ok:
            conn.execute("UPDATE dealers SET research_status=?, failure_reason=?, last_researched_at=?, updated_at=? WHERE id=?",
                         (result.status, result.failure_reason, now, now, dealer_id))
            # 既存の連絡方法・自動値は残す（取得失敗で消さない）
            return
        auto = {
            "name_auto": result.name, "region_auto": result.region, "address_auto": result.address,
            "postcode_auto": result.postcode,
            "categories_auto": json.dumps(result.categories, ensure_ascii=False),
            "category_basis_auto": result.category_basis, "category_scope_auto": result.category_scope,
            "summary_auto": result.summary, "used_sales_auto": result.used_sales, "uk_check_auto": result.uk_check,
        }
        sets, params = [], []
        for col, val in auto.items():
            if row[col] != val:
                log_change(conn, "dealer", dealer_id, col, row[col], val, "自動")
                sets.append(f"{col}=?")
                params.append(val)
        sets += ["research_status=?", "failure_reason=?", "last_researched_at=?", "updated_at=?"]
        params += [result.status, result.failure_reason or None, now, now]
        conn.execute(f"UPDATE dealers SET {', '.join(sets)} WHERE id=?", (*params, dealer_id))

        for topic, url, excerpt in result.evidence:
            conn.execute("INSERT INTO evidence(dealer_id, research_run_id, topic, source_url, excerpt, observed_at) "
                         "VALUES(?,?,?,?,?,?)", (dealer_id, run_id, topic, url, (excerpt or "")[:300], now))

        seen_ids = set()
        for f in result.contacts:
            norm = normalize_contact(f.kind, f.value)
            existing = conn.execute("SELECT * FROM contact_methods WHERE dealer_id=? AND kind=? AND value_norm=?",
                                    (dealer_id, f.kind, norm)).fetchone()
            if existing:
                cid = existing["id"]
                if existing["status_auto"] != f.status:
                    log_change(conn, "contact", cid, "status_auto", existing["status_auto"], f.status, "自動")
                conn.execute("UPDATE contact_methods SET status_auto=?, purpose=CASE WHEN origin='手動' THEN purpose ELSE ? END, "
                             "exclusion_reason=?, source_url=COALESCE(source_url, ?), excerpt=?, last_observed_at=?, "
                             "is_current=1, not_seen_since=NULL WHERE id=?",
                             (f.status, f.purpose, f.reason or None, f.source_url, f.excerpt[:300], now, cid))
            else:
                cid = new_id(PREFIX_CONTACT)
                conn.execute(
                    "INSERT INTO contact_methods(id, dealer_id, kind, value, value_norm, purpose, status_auto, exclusion_reason, "
                    "source_url, excerpt, origin, first_observed_at, last_observed_at, is_current, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?, '自動', ?, ?, 1, ?)",
                    (cid, dealer_id, f.kind, f.value, norm, f.purpose, f.status, f.reason or None, f.source_url,
                     f.excerpt[:300], now, now, now))
                log_change(conn, "contact", cid, "created", None, f"{f.kind}:{f.value}", "自動")
            seen_ids.add(cid)
            conn.execute("INSERT INTO evidence(dealer_id, contact_method_id, research_run_id, topic, source_url, excerpt, observed_at) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (dealer_id, cid, run_id, CONTACT_KINDS[f.kind], f.source_url,
                          (f"[{f.status}] {f.reason + ' / ' if f.reason else ''}{f.excerpt}")[:300], now))
        # 今回見つからなかった自動取得の連絡方法は削除せず「今回未検出」にする
        for c in conn.execute("SELECT id, is_current FROM contact_methods WHERE dealer_id=? AND origin='自動'", (dealer_id,)).fetchall():
            if c["id"] not in seen_ids and c["is_current"]:
                conn.execute("UPDATE contact_methods SET is_current=0, not_seen_since=? WHERE id=?", (now, c["id"]))
                log_change(conn, "contact", c["id"], "is_current", "1", "0（再調査で未検出）", "自動")
        _flag_same_phone(conn, dealer_id)
        if not row["name_manual"]:
            _flag_similar_name(conn, dealer_id, result.name)


def list_evidence(conn, dealer_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT e.*, c.kind AS contact_kind, c.value AS contact_value FROM evidence e "
        "LEFT JOIN contact_methods c ON c.id=e.contact_method_id WHERE e.dealer_id=? ORDER BY e.observed_at DESC, e.id",
        (dealer_id,))]


def list_history(conn, dealer_id: str, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM change_log WHERE (entity_type='dealer' AND entity_id=?) OR "
        "(entity_type='contact' AND entity_id IN (SELECT id FROM contact_methods WHERE dealer_id=?)) "
        "ORDER BY id DESC LIMIT ?", (dealer_id, dealer_id, limit))]


def list_runs(conn, dealer_id: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM research_runs WHERE dealer_id=? ORDER BY started_at DESC LIMIT 20", (dealer_id,)):
        d = dict(r)
        d["pages"] = json.loads(r["pages_json"]) if r["pages_json"] else []
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 候補
# ---------------------------------------------------------------------------
def add_candidate(conn, url: str, source: str, name: str | None = None, region: str | None = None,
                  hint: str | None = None, source_detail: str | None = None) -> tuple[Optional[str], str]:
    try:
        url = normalize_url(url)
        check_url_syntax(url)
    except UnsafeURLError as exc:
        return None, f"除外（{exc}）"
    if is_aggregator(url):
        return None, "除外（掲載サイト・検索サイト・SNS）"
    if host_key(url).endswith(".ie"):
        return None, "除外（アイルランドのドメイン）"
    if conn.execute("SELECT 1 FROM candidates WHERE url_host=?", (host_key(url),)).fetchone():
        return None, "候補に登録済み"
    dealer = find_by_host(conn, url)
    cid = new_id(PREFIX_CANDIDATE)
    conn.execute("INSERT INTO candidates(id, url, url_host, name, region, hint, source, source_detail, status, dealer_id, "
                 "discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, url, host_key(url), name, region, hint, source, source_detail,
                  "重複" if dealer else "未処理", dealer["id"] if dealer else None, utcnow()))
    return cid, "販売店として登録済み" if dealer else "候補に追加"


def list_candidates(conn, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute("SELECT * FROM candidates WHERE status=? ORDER BY discovered_at DESC", (status,))
    else:
        rows = conn.execute("SELECT * FROM candidates ORDER BY discovered_at DESC")
    return [dict(r) for r in rows]


def register_candidates(conn, ids: Iterable[str], by: str | None = None) -> list[str]:
    dealer_ids = []
    for cid in ids:
        c = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        if not c or c["status"] not in ("未処理",):
            continue
        try:
            did, created, _ = create_dealer(conn, c["url"], "候補発見", name=None, region=None,
                                            notes=f"候補発見元: {c['source']} {c['source_detail'] or ''}".strip(), by=by)
        except RepoError as exc:
            conn.execute("UPDATE candidates SET status='除外', note=? WHERE id=?", (str(exc), cid))
            continue
        conn.execute("UPDATE candidates SET status=?, dealer_id=? WHERE id=?", ("登録済み" if created else "重複", did, cid))
        if created:
            dealer_ids.append(did)
    return dealer_ids


def exclude_candidates(conn, ids: Iterable[str], note: str = "利用者が除外") -> None:
    for cid in ids:
        conn.execute("UPDATE candidates SET status='除外', note=? WHERE id=? AND status='未処理'", (note, cid))


# ---------------------------------------------------------------------------
# ジョブ
# ---------------------------------------------------------------------------
def create_job(conn, kind: str, dealer_ids: list[str], params: dict | None = None) -> str:
    jid = new_id(PREFIX_JOB)
    now = utcnow()
    with transaction(conn):
        conn.execute("INSERT INTO jobs(id, kind, status, created_at, updated_at, params_json) VALUES(?,?, '実行中', ?, ?, ?)",
                     (jid, kind, now, now, json.dumps(params or {}, ensure_ascii=False)))
        for did in dealer_ids:
            conn.execute("INSERT INTO job_items(job_id, dealer_id) VALUES(?, ?)", (jid, did))
            if kind == "research":
                set_research_status(conn, did, "待機中")
    return jid


def job_view(conn, job_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM job_items WHERE job_id=? GROUP BY status", (job_id,))}
    d["counts"] = counts
    d["total"] = sum(counts.values())
    d["done"] = counts.get("完了", 0) + counts.get("失敗", 0)
    d["params"] = json.loads(row["params_json"] or "{}")
    return d


def list_jobs(conn, limit: int = 30) -> list[dict]:
    return [job_view(conn, r["id"]) for r in conn.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]


def job_items(conn, job_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT i.*, d.website_url, COALESCE(d.name_manual, d.name_auto) AS name FROM job_items i "
        "LEFT JOIN dealers d ON d.id=i.dealer_id WHERE i.job_id=? ORDER BY i.id", (job_id,))]


def set_job_status(conn, job_id: str, status: str, message: str | None = None) -> None:
    conn.execute("UPDATE jobs SET status=?, updated_at=?, message=COALESCE(?, message) WHERE id=?",
                 (status, utcnow(), message, job_id))


def claim_next_item(conn) -> Optional[sqlite3.Row]:
    with transaction(conn):
        row = conn.execute(
            "SELECT i.* FROM job_items i JOIN jobs j ON j.id=i.job_id WHERE j.status='実行中' AND j.kind='research' "
            "AND i.status='待機' ORDER BY j.created_at, i.id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE job_items SET status='実行中', started_at=? WHERE id=?", (utcnow(), row["id"]))
        return row


def finish_item(conn, item_id: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE job_items SET status=?, error=?, finished_at=? WHERE id=?", (status, error, utcnow(), item_id))


def recover_interrupted(conn) -> int:
    """前回終了時に実行中だったものを再開可能な状態へ戻す。"""
    with transaction(conn):
        n = conn.execute("UPDATE job_items SET status='待機', started_at=NULL WHERE status='実行中'").rowcount
        conn.execute("UPDATE jobs SET status='中断', updated_at=?, message='アプリ終了により中断。「再開」で続きから実行できます' "
                     "WHERE status IN ('実行中', '停止要求')", (utcnow(),))
        conn.execute("UPDATE dealers SET research_status='未調査' WHERE research_status IN ('待機中', '調査中') "
                     "AND last_researched_at IS NULL")
        conn.execute("UPDATE dealers SET research_status='停止' WHERE research_status IN ('待機中', '調査中')")
    return n


# ---------------------------------------------------------------------------
# 取得キャッシュ・API使用量
# ---------------------------------------------------------------------------
class DBCache(Cache):
    def __init__(self, conn_factory):
        self.conn_factory = conn_factory

    def get(self, url: str, max_age_hours: float) -> Optional[FetchResult]:
        conn = self.conn_factory()
        try:
            row = conn.execute("SELECT * FROM fetch_cache WHERE url=?", (url,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
            return None
        text = decompress(row["body"]) if row["body"] else ""
        err = "" if 200 <= (row["status_code"] or 0) < 300 else f"HTTP {row['status_code']}"
        return FetchResult(url=url, final_url=row["final_url"] or url, status_code=row["status_code"] or 0,
                           content_type=row["content_type"] or "", text=text, error=err if not text else "")

    def put(self, result: FetchResult) -> None:
        if result.status_code == 0:
            return
        conn = self.conn_factory()
        try:
            conn.execute("INSERT INTO fetch_cache(url, fetched_at, status_code, final_url, content_type, body) VALUES(?,?,?,?,?,?) "
                         "ON CONFLICT(url) DO UPDATE SET fetched_at=excluded.fetched_at, status_code=excluded.status_code, "
                         "final_url=excluded.final_url, content_type=excluded.content_type, body=excluded.body",
                         (result.url, utcnow(), result.status_code, result.final_url, result.content_type,
                          compress(result.text) if result.text and not result.error else None))
        finally:
            conn.close()


def record_api_usage(conn, provider: str, units: float, note: str = "") -> None:
    conn.execute("INSERT INTO api_usage(provider, used_at, units, note) VALUES(?,?,?,?)", (provider, utcnow(), units, note))


def api_usage_this_month(conn, provider: str) -> float:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute("SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE provider=? AND substr(used_at, 1, 7)=?",
                       (provider, month)).fetchone()
    return float(row[0])
