"""開発用：URL一覧をアプリの調査処理で調べ、結果を表示する（画面を使わない一括実行）。

使い方:
  python scripts/research_urls.py --data-dir DIR --origin 初期調査データ urls.txt
urls.txt は 1 行 1 URL（# 以降はメモとして無視）。取得制約（robots.txt・間隔2秒以上等）はアプリと同じ。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdr import repo  # noqa: E402
from sdr.config import Paths  # noqa: E402
from sdr.db import connect, migrate  # noqa: E402
from sdr.jobs import JobManager  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--origin", default="利用者追加")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("urls")
    args = ap.parse_args()
    paths = Paths(Path(args.data_dir))
    paths.ensure()
    migrate(paths.db, paths.backups)
    conn = connect(paths.db)
    ids = []
    for line in Path(args.urls).read_text(encoding="utf-8").splitlines():
        url = line.split("#", 1)[0].strip()
        if not url:
            continue
        try:
            did, created, note = repo.create_dealer(conn, url, args.origin)
            ids.append(did)
            print(("新規 " if created else "既存 ") + did, url)
        except repo.RepoError as exc:
            print("登録不可", url, exc)
    conn.close()
    jm = JobManager(paths.db, paths.secrets)
    jid = jm.start_research(ids, "一括調査")
    conn = connect(paths.db)
    items = repo.job_items(conn, jid)
    conn.close()

    def work(item_id):
        conn = connect(paths.db)
        try:
            row = conn.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
            conn.execute("UPDATE job_items SET status='実行中' WHERE id=?", (item_id,))
        finally:
            conn.close()
        jm.run_item(row)

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(work, [i["id"] for i in items]))
    conn = connect(paths.db)
    for d in repo.list_dealers(conn):
        if d["id"] not in ids:
            continue
        print("-" * 80)
        print(d["id"], d["name"], d["website_url"], "|", d["research_status"], d["failure_reason"] or "")
        print("  地域:", d["region"], "| 分類:", d["categories"], d["category_basis"], "|", d["used_sales"], "|", d["uk_check_auto"])
        print("  概要:", d["summary"])
        for c in d["contacts"]:
            print(f"  [{c['kind']}] {c['value']} | {c['purpose']} | {c['status']} {c['exclusion_reason'] or ''}")
    conn.close()


if __name__ == "__main__":
    main()
