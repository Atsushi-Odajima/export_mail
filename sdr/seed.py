"""確認済み初期データ（実在の英国販売店）の書き出し・読み込み。

初期データは販売店 ID を固定したまま配布し、利用者の DB に「無い ID だけ」追加する。
利用者が編集した販売店は上書きしない。試験用の架空データは tests/ にだけ置き、ここには入れない。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import resource_dir
from .db import transaction, utcnow

SEED_FILE = "seed/initial_dealers.json"
TABLES = {
    "dealers": "id",
    "contact_methods": "id",
    "research_runs": "id",
}


def export_seed(conn, dealer_ids: list[str], path: Path) -> dict:
    out: dict = {"format": "sdr-seed-1", "exported_at": utcnow(), "dealers": [], "contact_methods": [], "evidence": [],
                 "research_runs": [], "change_log": []}
    marks = ",".join("?" * len(dealer_ids))
    for table in ("dealers",):
        out[table] = [dict(r) for r in conn.execute(f"SELECT * FROM dealers WHERE id IN ({marks})", dealer_ids)]
    out["contact_methods"] = [dict(r) for r in conn.execute(
        f"SELECT * FROM contact_methods WHERE dealer_id IN ({marks})", dealer_ids)]
    # 配布する根拠は各販売店の最新の調査分と手動登録分だけ（開発中の試行の重複を配らない）
    latest = {r["dealer_id"]: r["id"] for r in conn.execute(
        f"SELECT dealer_id, id FROM research_runs r WHERE dealer_id IN ({marks}) AND started_at = "
        "(SELECT MAX(started_at) FROM research_runs r2 WHERE r2.dealer_id = r.dealer_id)", dealer_ids)}
    out["evidence"] = [{k: v for k, v in dict(r).items() if k != "id"} for r in conn.execute(
        f"SELECT * FROM evidence WHERE dealer_id IN ({marks}) ORDER BY id", dealer_ids)
        if r["origin"] == "手動" or r["research_run_id"] == latest.get(r["dealer_id"])]
    out["research_runs"] = [dict(r) for r in conn.execute(
        f"SELECT * FROM research_runs WHERE dealer_id IN ({marks})", dealer_ids) if r["id"] in latest.values()]
    for r in out["research_runs"]:
        r["job_id"] = None
    contact_ids = [c["id"] for c in out["contact_methods"]]
    ids = dealer_ids + contact_ids
    marks2 = ",".join("?" * len(ids))
    out["change_log"] = [{k: v for k, v in dict(r).items() if k != "id"} for r in conn.execute(
        f"SELECT * FROM change_log WHERE entity_id IN ({marks2}) ORDER BY id", ids)]
    for d in out["dealers"]:
        d["data_origin"] = "初期調査データ"
        d["parent_dealer_id"] = d["parent_dealer_id"] if d["parent_dealer_id"] in dealer_ids else None
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def seed_path() -> Path:
    return resource_dir() / SEED_FILE


def import_seed(conn, path: Path | None = None) -> int:
    path = path or seed_path()
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    with transaction(conn):
        existing = {r["id"] for r in conn.execute("SELECT id FROM dealers")}
        hosts = {r["website_host"] for r in conn.execute("SELECT website_host FROM dealers")}
        new_ids = set()
        for d in data.get("dealers", []):
            if d["id"] in existing or d["website_host"] in hosts:
                continue
            _insert(conn, "dealers", d)
            new_ids.add(d["id"])
            added += 1
        for table in ("contact_methods", "research_runs", "evidence", "change_log"):
            for row in data.get(table, []):
                owner = row.get("dealer_id") or (row.get("entity_id") if table == "change_log" else None)
                if table == "change_log":
                    if not (row["entity_id"] in new_ids or any(
                            c["id"] == row["entity_id"] and c["dealer_id"] in new_ids for c in data["contact_methods"])):
                        continue
                elif owner not in new_ids:
                    continue
                _insert(conn, table, row)
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('seed_imported_at', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utcnow(),))
    return added


def _insert(conn, table: str, row: dict) -> None:
    cols = list(row.keys())
    conn.execute(f"INSERT OR IGNORE INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                 [row[c] for c in cols])
