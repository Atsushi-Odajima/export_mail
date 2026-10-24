"""開発用：初期データ（実在の英国販売店）の人による照合結果を反映し、配布用データと報告書を出力する。

照合手順（全件）：
  1. 候補の出典（AA Cars の販売店ページに載った公式サイトリンク、Web 検索結果、911uk.com の販売店一覧）から
     公式サイト URL を得る（URL の推測はしない）。
  2. アプリの調査処理（robots.txt・取得間隔 2 秒以上・最大 10 ページ）で公式サイトを取得。
  3. 詳細画面の根拠（抜粋・出典 URL）を人が読み、名称・公式サイト・英国所在・中古車販売・分類根拠・連絡方法を照合。
  4. 誤り・不足は「人の確定値」として修正し、判断内容をメモに残す（下の DECISIONS が記録）。
使い方:
  python scripts/curate_initial_data.py --db 調査DB --extra-failed-db 予備調査DB
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdr import csv_io, repo, seed  # noqa: E402
from sdr.db import connect  # noqa: E402

BY = "初期データ照合（開発時・2026-10-24）"
SRC_AA = "AA Cars の販売店ページに掲載された公式サイトリンク"
SRC_SEARCH = "Web 検索結果（販売店名・掲載情報）"
SRC_911 = "911uk.com の販売店一覧（car-dealers）のリンク"

# host: (候補の出典, 人の確定値, 連絡方法の確定 {値: (状態, 理由)}, 確認状態, 照合メモ)
DECISIONS: dict[str, dict] = {
    "woodhallcars.co.uk": {"src": SRC_SEARCH},
    "ascarsleedsltd.co.uk": {"src": SRC_SEARCH, "manual": {"address": "Unit 2, Green Side Farm, Warm Lane, Leeds, West Yorkshire LS19 7DW"}},
    "whiterosecarsales.co.uk": {"src": SRC_AA, "manual": {"address": "408 Oakwood Lane, Oaktree Business Centre, Leeds, West Yorkshire LS8 3LG"}},
    "bkgeneralandprestigecars.co.uk": {"src": SRC_AA, "manual": {"address": "Unit 1-3 Station Rd Ind. Estate, Station Road, Leeds, West Yorkshire LS25 7QH"}, "note": "在庫ページの一部が 404 のため分類根拠は要手動確認のまま（会社紹介に used car sales site の記載）"},
    "angelmotorcompany.com": {"src": SRC_AA, "manual": {"address": "46 Belle Isle Rd, Belle Isle, Leeds, West Yorkshire LS10 2DF"}},
    "burnsidecarstore.co.uk": {"src": SRC_AA, "manual": {"address": "41A East Kilbride Road, Glasgow G73 5DU"}},
    "crawfordautomobiles.co.uk": {"src": SRC_AA},
    "garrowhillcarsales.co.uk": {"src": SRC_AA},
    "gmpcars.co.uk": {"src": SRC_AA, "manual": {"name": "GMP Cars", "address": "Preston, Lancashire PR3 3BN（通り名は取得ページに記載なし）"},
                      "note": "店名：トップページの「GMP Cars is a used car dealer in Preston」の記載に合わせて修正"},
    "ng9motors.co.uk": {"src": SRC_AA},
    "yorkcarandcommercial.co.uk": {"src": SRC_AA, "note": "公開メールは確認できず、問い合わせページに送信フォームも確認できない（電話のみ確認）"},
    "ultima4x4.co.uk": {"src": SRC_SEARCH,
                        "contacts": {"sales@ulitima4x4.co.uk": ("除外", "公式サイト内の表記ゆれ（ulitima）と見られる別ドメイン。正しい表記は sales@ultima4x4.co.uk")},
                        "note": "店名に 4x4 を含むが、確認した在庫ページ（24 件）は特定分野に偏っていないため一般中古車のまま"},
    "prestigemotorco.com": {"src": SRC_AA, "note": "公式サイトに「Used car and van Dealer」の記載。問い合わせページは取得タイムアウトのため未確認"},
    "jdmautos.co.uk": {"src": SRC_AA, "manual": {"region": "イングランド南東部・Southampton（会社紹介ページの記載。郵便番号は未確認）"},
                       "note": "地域：会社紹介ページの「local to Southampton … our showroom」の記載から人が入力"},
    "1stchoicecarspreston.co.uk": {"src": SRC_AA, "manual": {"address": "The Old Car Wash, Chorley Road, Walton le Dale, Preston PR5 4JN"}},
    "cardiffmotors.com": {"src": SRC_AA},
    "mount-vernon.co.uk": {"src": SRC_AA, "note": "在庫ページを取得できず、分類根拠は要手動確認（公式サイトに Used Car Dealer の記載）"},
    "heritageclassic.com": {"src": SRC_SEARCH},
    "classicmotorhub.com": {"src": SRC_SEARCH, "note": "問い合わせは来店案内ページのみ確認（送信フォームは未確認）"},
    "rhclassics.co.uk": {"src": SRC_SEARCH, "manual": {"address": "12 Main Street, Twyford, Melton Mowbray LE14 2HU"}},
    "cameronsportscars.com": {"src": SRC_911,
                              "contacts": {"sales@cameronsc.co.uk": ("公開メール確認済み", "公式サイト上部の「Email Us」リンク（mailto）に表示。ドメインは社名の略称（cameronsc）")},
                              "note": "問い合わせページは WordPress のフォームプラグイン（Gravity Forms）の埋め込みのため要手動確認"},
    "harbourcars.com": {"src": SRC_911, "review": "要確認",
                        "note": "公式サイトに「selling used Porsches and Mercedes-Benz」の記載はあるが、専門店の明記と在庫ページは取得できず分類は要確認のまま"},
    "stirlings.co.uk": {"src": SRC_911, "note": "所在地（郵便番号）は取得したページに記載がなく不明のまま"},
    "jrmotorcompany.co.uk": {"src": SRC_911, "manual": {"name": "JR Motor Company"},
                             "note": "店名：問い合わせページの「JR Motor Company Limited」に合わせて修正"},
    "911virgin.com": {"src": SRC_911, "note": "運営会社：Prestige and Performance Cars Ltd（問い合わせページの記載）"},
    "kemp4x4s.co.uk": {"src": SRC_SEARCH, "manual": {"name": "John Kemp 4x4"},
                       "note": "店名：トップページの「John Kemp 4×4 Specialists」に合わせて修正（自動取得は支店名だった）。service@ は整備窓口"},
    "barryashworth4x4.co.uk": {"src": SRC_SEARCH},
    "beckside4x4.co.uk": {"src": SRC_SEARCH, "manual": {"categories": ["ミニバン・SUV", "一般中古車"]},
                          "note": "店名に「4X4 Centre」。公式サイトに Used Cars, Bikes, Commercials の記載もあるため一般中古車も付与"},
    "mikehardinglandrovers.co.uk": {"src": SRC_SEARCH},
    "landrovercentre.com": {"src": SRC_SEARCH,
                            "contacts": {"orders@lrcparts.com": ("要手動確認", "部品販売（LRC Parts）の注文窓口の可能性")},
                            "note": "公開メールは部品販売の窓口のみ。車両販売の窓口は未確認"},
}

# 調査したが初期データに入れなかったもの（隠さず報告する）
EXCLUDED_REASONS = {
    "nobukojapan.com": "所在地が東京（構造化データ）の日本の輸出業者で、英国の販売店ではない",
    "motovia.co.uk": "公式サイトが作成途中（Content Coming Soon）で、中古車販売を確認できない",
    "pearcebros.com": "主な事業がバンのレンタル・整備で、問い合わせ・在庫ページは取得タイムアウト。中古車販売店か要確認",
    "leedsmotorcarsltd.co.uk": "ボット確認ページ（One moment, please...）が表示され取得できない（予備調査ではこのページを誤って読み込んだため、検出処理を追加した）",
    "sgpetch.co.uk": "複数ブランドの正規ディーラー（新車・中古車）。独立系の候補を優先し今回は含めない",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--extra-failed-db", action="append", default=[])
    args = ap.parse_args()
    conn = connect(Path(args.db))
    dealers = {d["website_host"]: d for d in repo.list_dealers(conn, {"include_archived": True})}
    ids = []
    for host, dec in DECISIONS.items():
        d = dealers[host]
        did = d["id"]
        ids.append(did)
        values = dict(dec.get("manual", {}))
        note = f"候補の出典：{dec['src']}。照合：名称・公式サイト・英国所在・中古車販売・分類根拠・連絡方法を根拠ページで確認。"
        if dec.get("note"):
            note += dec["note"]
        values["notes"] = note
        conn.execute("BEGIN IMMEDIATE")
        repo.update_manual(conn, did, values, BY)
        conn.execute("COMMIT")
        for c in d["contacts"]:
            if c["value"] in dec.get("contacts", {}):
                status, reason = dec["contacts"][c["value"]]
                repo.set_contact_status(conn, c["id"], status, BY, reason)
        repo.set_review(conn, did, dec.get("review", "確認済み"), BY)
    final = [repo.get_dealer(conn, i) for i in ids]

    seed_path = ROOT / "sdr" / "seed" / "initial_dealers.json"
    seed.export_seed(conn, ids, seed_path)
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "initial_dealers.csv").write_bytes(csv_io.dealers_csv(final))
    # 連絡方法・根拠は初期データ分だけ
    tmp = [d for d in repo.list_dealers(conn, {"include_archived": True}) if d["id"] in ids]
    rows_c, rows_e = [], []
    for d in tmp:
        for c in d["contacts"]:
            rows_c.append([c["id"], d["id"], d["name"], c["kind_label"], c["value"], c["purpose"], c["status"],
                           "はい" if c["status_is_manual"] else "", c["exclusion_reason"], c["source_url"], c["excerpt"],
                           c["first_observed_at"], c["last_observed_at"], c["human_verified_at"],
                           "はい" if c["is_current"] else "いいえ"])
        latest = repo.list_runs(conn, d["id"])[0]["id"]
        for e in repo.list_evidence(conn, d["id"]):
            if e["research_run_id"] == latest or e["origin"] == "手動":
                rows_e.append([d["id"], d["name"], e["topic"], e["source_url"], e["excerpt"], e["observed_at"]])
    (data_dir / "initial_contacts.csv").write_bytes(csv_io.to_csv(
        ["連絡方法ID", "販売店ID", "店名", "種類", "値", "用途", "状態", "人が確定", "除外・確認理由", "取得元URL", "根拠の抜粋",
         "初回観測(UTC)", "最終観測(UTC)", "人の確認日時(UTC)", "最新の調査で確認"], rows_c))
    (data_dir / "initial_evidence.csv").write_bytes(csv_io.to_csv(
        ["販売店ID", "店名", "項目", "出典URL", "根拠の抜粋", "観測日時(UTC)"], rows_e))

    # 取得できなかった・除外したサイト
    failed = []
    seen_hosts = set(DECISIONS)
    for db in [args.db] + args.extra_failed_db:
        c2 = connect(Path(db))
        for d in repo.list_dealers(c2, {"include_archived": True}):
            h = d["website_host"]
            if h in seen_hosts:
                continue
            seen_hosts.add(h)
            if h in EXCLUDED_REASONS:
                failed.append([h, d["website_url"], "照合で除外", EXCLUDED_REASONS[h], d["last_researched_at"]])
            elif d["research_status"] == "取得失敗":
                failed.append([h, d["website_url"], "取得失敗", d["failure_reason"], d["last_researched_at"]])
        c2.close()
    failed.sort(key=lambda r: (r[2], r[0]))
    (data_dir / "unreachable_or_excluded.csv").write_bytes(csv_io.to_csv(
        ["ドメイン", "公式サイト", "区分", "理由", "調査日時(UTC)"], failed))

    write_report(final, failed)
    conn.close()


def write_report(final: list[dict], failed: list[list]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with_email = [d for d in final if d["has_email"]]
    with_form = [d for d in final if d["has_form"]]
    with_any = [d for d in final if d["has_email"] or d["has_form"]]
    cats = Counter(c for d in final for c in d["categories"])
    lines = [
        "# 初期データ（実在の英国中古車販売店）調査報告",
        "",
        f"作成：{now}。アプリの調査処理で公式サイトを取得し、全件の根拠を人が照合した結果です（`scripts/curate_initial_data.py` に照合内容を記録）。",
        "アプリ初回起動時に `sdr/seed/initial_dealers.json` から読み込まれます（データ区分「初期調査データ」）。",
        "",
        "## 集計",
        "",
        "| 項目 | 件数 |",
        "|---|---|",
        f"| 登録した販売事業者（実在・英国・中古車販売を公式サイトで確認） | {len(final)} |",
        f"| 公開メールまたは問い合わせフォームを確認できた販売店 | **{len(with_any)}** |",
        f"| 公開メールを確認（公式サイトで公開表示） | {len(with_email)} |",
        f"| 問い合わせフォームを確認 | {len(with_form)} |",
        f"| 連絡方法が未確認（電話・問い合わせページのみ等） | {len(final) - len(with_any)} |",
        f"| 人の確認状態「確認済み」 | {sum(1 for d in final if d['review_status'] == '確認済み')}（残りは要確認） |",
        f"| 調査したが取得できなかった・照合で除外したサイト | {len(failed)}（`data/unreachable_or_excluded.csv`） |",
        "",
        "分類内訳（1社で複数あり）：" + "／".join(f"{k} {cats.get(k, 0)}" for k in
                                       ["一般中古車", "スポーツカー", "ミニバン・SUV", "クラシックカー", "その他", "要確認"]),
        "",
        "分類根拠の内訳：" + "／".join(f"{k} {v}" for k, v in Counter(d["category_basis"] or "なし" for d in final).items()),
        "",
        "注意：公開メールは「公式サイトで公開表示を確認した」ことだけを示します。到達可能性・営業送信の許可は確認していません。",
        "「確認した在庫からの傾向」は確認した在庫ページの範囲での判断で、全在庫は調べていません。",
        "",
        "## 一覧",
        "",
        "| # | 店名 | 地域 | 分類（根拠） | 公開メール | フォーム | 確認 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, d in enumerate(final, 1):
        emails = "<br>".join(c["value"] for c in d["emails_ok"]) or "未確認"
        form = "確認済み" if d["has_form"] else ("ページのみ" if d["contact_pages"] else "未確認")
        lines.append(f"| {i} | [{d['name']}]({d['website_url']}) | {d['region'] or '不明'} | {'・'.join(d['categories'])}（{d['category_basis']}） | {emails} | {form} | {d['review_status']} |")
    lines += ["", "## 取得できなかった・除外したサイト（隠さず記録）", "",
              "アクセス制限（HTTP 403・ボット確認ページ）は回避していません。接続できないサイトは robots.txt を確認できないため取得を見送っています。"
              "これらはブラウザーで人が確認すれば手動で登録できます。", "",
              "| ドメイン | 区分 | 理由 |", "|---|---|---|"]
    for r in failed:
        lines.append(f"| {r[0]} | {r[2]} | {(r[3] or '').replace('|', '／')} |")
    lines += ["", "## 照合の手順", "",
              "1. 候補の出典（AA Cars の販売店ページの公式サイトリンク、Web 検索結果、911uk.com の販売店一覧）から公式サイト URL を得た（URL の推測なし）。",
              "2. アプリの調査処理で公式サイトを取得（robots.txt 遵守・取得間隔 2 秒以上・1 サイト最大 10 ページ）。",
              "3. 根拠の抜粋と出典 URL を読み、名称・公式サイト・英国所在（郵便番号）・中古車販売の記載・分類根拠・連絡方法を照合。",
              "4. 誤りは人の確定値で修正し、判断をメモに記録（店名の修正 3 件、所在地の整形 8 件、地域の補記 1 件、分類の追加 1 件、連絡方法の確定 3 件）。",
              "5. 英国外の事業者・作成途中のサイト・主業が異なるものは除外。",
              "", "初期データの登録（この報告）と、アプリ自身による新規候補の発見・調査の検証は別に行いました（docs/VERIFICATION.md）。"]
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "INITIAL_DATA_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"登録 {len(final)} / メールまたはフォーム {len(with_any)} / メール {len(with_email)} / フォーム {len(with_form)} / 除外・失敗 {len(failed)}")
    print(dict(cats))


if __name__ == "__main__":
    main()
