"""CSV の入出力（CSV は入出力用。正本はデータベース）。

出力：UTF-8（BOM 付き）・CRLF で Excel の文字化けを防ぐ。
「= + - @ タブ 改行」で始まる値は先頭に ' を付け、数式として実行されないようにする。
"""
from __future__ import annotations

import csv
import io

from . import repo

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
MAX_IMPORT_BYTES = 2_000_000
MAX_IMPORT_ROWS = 2000

URL_HEADERS = {"url", "website", "website_url", "公式サイト", "公式サイトurl", "サイト", "ホームページ"}
NAME_HEADERS = {"name", "店名", "販売店名", "会社名"}
REGION_HEADERS = {"region", "地域", "所在地", "town", "city"}
NOTE_HEADERS = {"note", "notes", "メモ", "備考"}


def safe_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def to_csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    writer.writerow([safe_cell(h) for h in header])
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def dealers_csv(dealers: list[dict]) -> bytes:
    header = ["販売店ID", "店名", "公式サイト", "地域", "所在地", "分類", "分類根拠", "分類で確認した範囲", "日本語概要",
              "公開メール（確認済み）", "要確認のメール", "問い合わせフォーム", "問い合わせページ（フォーム未確認等）", "電話",
              "連絡方法の状態", "中古車販売の確認", "英国内の確認", "調査状態", "取得失敗の理由", "最終調査日時(UTC)",
              "人の確認状態", "人の確認日時(UTC)", "重複確認", "本体の販売店ID", "データ区分", "メモ"]
    rows = []
    for d in dealers:
        rows.append([
            d["id"], d["name"], d["website_url"], d["region"], d["address"], "・".join(d["categories"]),
            d["category_basis"], d["category_scope_auto"], d["summary"],
            " ".join(c["value"] for c in d["emails_ok"]), " ".join(c["value"] for c in d["emails_review"]),
            " ".join(c["value"] for c in d["forms_ok"]),
            " ".join(f"{c['value']}（{c['status']}）" for c in d["contact_pages"]),
            " / ".join(c["value"] for c in d["phones"]), d["contact_summary"], d["used_sales"], d["uk_check_auto"],
            d["research_status"], d["failure_reason"], d["last_researched_at"], d["review_status"], d["reviewed_at"],
            d["duplicate_status"], d["parent_dealer_id"], d["data_origin"], d["notes"],
        ])
    return to_csv(header, rows)


def contacts_csv(conn) -> bytes:
    header = ["連絡方法ID", "販売店ID", "店名", "種類", "値", "用途", "状態", "状態は人が確定", "除外・確認理由", "担当者名", "役職",
              "取得元URL", "根拠の抜粋", "取得経路", "初回観測(UTC)", "最終観測(UTC)", "人の確認日時(UTC)", "最新の調査で確認"]
    rows = []
    for d in repo.list_dealers(conn, {"include_archived": True}):
        for c in d["contacts"]:
            rows.append([c["id"], d["id"], d["name"], c["kind_label"], c["value"], c["purpose"], c["status"],
                         "はい" if c["status_is_manual"] else "", c["exclusion_reason"], c["person_name"], c["person_role"],
                         c["source_url"], c["excerpt"], c["origin"], c["first_observed_at"], c["last_observed_at"],
                         c["human_verified_at"], "はい" if c["is_current"] else "いいえ（古い値・履歴として保持）"])
    return to_csv(header, rows)


def evidence_csv(conn) -> bytes:
    header = ["販売店ID", "店名", "項目", "出典URL", "根拠の抜粋", "観測日時(UTC)", "取得経路", "調査ID"]
    rows = []
    for d in repo.list_dealers(conn, {"include_archived": True}):
        for e in repo.list_evidence(conn, d["id"]):
            rows.append([d["id"], d["name"], e["topic"], e["source_url"], e["excerpt"], e["observed_at"], e["origin"],
                         e["research_run_id"]])
    return to_csv(header, rows)


def parse_import(data: bytes) -> tuple[list[dict], list[str]]:
    """取込用 CSV を読む。戻り値 (行, エラー)。"""
    if len(data) > MAX_IMPORT_BYTES:
        return [], ["ファイルが大きすぎます（2MB まで）"]
    text = None
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return [], ["文字コードを読み取れません（UTF-8 または Shift_JIS で保存してください）"]
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], ["空のファイルです"]
    header = [h.strip().lstrip("'").lower() for h in rows[0]]

    def col(names):
        for i, h in enumerate(header):
            if h in names:
                return i
        return None
    iu, iname, ireg, inote = col(URL_HEADERS), col(NAME_HEADERS), col(REGION_HEADERS), col(NOTE_HEADERS)
    body = rows[1:]
    if iu is None:  # 見出しなし：1列目を URL とみなす
        iu, body = 0, rows
    errors, out = [], []
    for n, row in enumerate(body[:MAX_IMPORT_ROWS], start=2 if body is not rows else 1):
        if not row or iu >= len(row) or not row[iu].strip():
            continue
        get = lambda i: row[i].strip().lstrip("'") if i is not None and i < len(row) else ""  # noqa: E731
        out.append({"line": n, "url": get(iu), "name": get(iname), "region": get(ireg), "note": get(inote)})
    if len(body) > MAX_IMPORT_ROWS:
        errors.append(f"{MAX_IMPORT_ROWS} 行を超えた分は読み込みませんでした")
    return out, errors
