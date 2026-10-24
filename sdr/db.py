"""SQLite 接続とデータベースの版管理（移行処理）。

- 版は schema_meta.schema_version に記録する。
- 起動時に未適用の移行を順番に適用する。適用前に DB ファイルを backups/ へ複製する。
- 各移行は 1 トランザクションで実行し、失敗したら巻き戻して起動を中止する
  （データは移行前の状態のまま残る。復旧手順は README「更新に失敗したとき」）。
- 将来のメール機能のテーブルは、新しい移行関数を MIGRATIONS に追加して作る。
"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import __version__


class MigrationError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# 移行（版 1 が今回の初期構造）
# ---------------------------------------------------------------------------

SCHEMA_V1 = """
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 販売事業者（1事業者1行。支店は parent_dealer_id で本体に関連付ける）
-- *_auto は自動取得値、*_manual は人の確定値（NULL なら未確定）。表示は manual を優先。
CREATE TABLE dealers (
    id TEXT PRIMARY KEY,                    -- DLR-xxxx（変更しない）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    website_url TEXT NOT NULL,
    website_host TEXT NOT NULL,             -- 重複判定用（www. を除いた小文字ホスト）
    name_auto TEXT, name_manual TEXT,
    region_auto TEXT, region_manual TEXT,
    address_auto TEXT, address_manual TEXT,
    postcode_auto TEXT,
    categories_auto TEXT,                   -- JSON 配列
    categories_manual TEXT,                 -- JSON 配列 / NULL
    category_basis_auto TEXT, category_basis_manual TEXT,
    category_scope_auto TEXT,               -- 分類で調べた範囲
    summary_auto TEXT, summary_manual TEXT,
    used_sales_auto TEXT,                   -- 中古車販売の確認状況（自動）
    uk_check_auto TEXT,                     -- 英国内の確認状況（自動）
    eligibility_manual TEXT,                -- 人の判断：対象 / 対象外 / NULL
    research_status TEXT NOT NULL DEFAULT '未調査',
    failure_reason TEXT,
    last_researched_at TEXT,
    review_status TEXT NOT NULL DEFAULT '未確認',
    reviewed_at TEXT,
    reviewed_by TEXT,
    notes TEXT,
    parent_dealer_id TEXT REFERENCES dealers(id),
    duplicate_status TEXT,                  -- NULL / 重複確認待ち / 別事業者と確認済み
    duplicate_of TEXT,
    data_origin TEXT NOT NULL,              -- 初期調査データ / 利用者追加 / CSV取込 / 候補発見 / テスト
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_dealers_host ON dealers(website_host);

-- 連絡方法（販売店本体と別記録。1社に複数登録可）
CREATE TABLE contact_methods (
    id TEXT PRIMARY KEY,                    -- CNT-xxxx（変更しない）
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    kind TEXT NOT NULL,                     -- email / form / contact_page / phone
    value TEXT NOT NULL,
    value_norm TEXT NOT NULL,
    purpose TEXT,                           -- 一般・営業窓口候補 / 部門別 / 除外候補
    status_auto TEXT,
    status_manual TEXT,
    exclusion_reason TEXT,
    person_name TEXT,                       -- 公開され確認できた場合のみ
    person_role TEXT,
    source_url TEXT,
    excerpt TEXT,
    origin TEXT NOT NULL,                   -- 自動 / 手動 / 取込
    first_observed_at TEXT,
    last_observed_at TEXT,
    human_verified_at TEXT,
    human_verified_by TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,  -- 最新の調査でも確認できたか（古い値も削除しない）
    not_seen_since TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (dealer_id, kind, value_norm)
);

-- 出典と根拠（観測日時を保存。人の確認日時は各本体側に持つ）
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    contact_method_id TEXT REFERENCES contact_methods(id),
    research_run_id TEXT,
    topic TEXT NOT NULL,
    source_url TEXT,
    excerpt TEXT,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '自動'
);
CREATE INDEX idx_evidence_dealer ON evidence(dealer_id);

CREATE TABLE research_runs (
    id TEXT PRIMARY KEY,
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    job_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    pages_attempted INTEGER NOT NULL DEFAULT 0,
    pages_ok INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    pages_json TEXT
);
CREATE INDEX idx_runs_dealer ON research_runs(dealer_id);

-- 変更履歴（古い値を失わないため）
CREATE TABLE change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    origin TEXT NOT NULL,                   -- 自動 / 手動 / 取込 / システム
    changed_at TEXT NOT NULL,
    changed_by TEXT
);
CREATE INDEX idx_change_entity ON change_log(entity_type, entity_id);

-- 候補（発見処理の結果。調査とは別）
CREATE TABLE candidates (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    url_host TEXT NOT NULL,
    name TEXT,
    region TEXT,
    hint TEXT,
    source TEXT NOT NULL,                   -- OpenStreetMap / Firecrawl / 手動URL / CSV
    source_detail TEXT,
    status TEXT NOT NULL DEFAULT '未処理',  -- 未処理 / 登録済み / 除外 / 重複
    dealer_id TEXT,
    note TEXT,
    discovered_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                     -- research / discover
    status TEXT NOT NULL,                   -- 実行中 / 停止要求 / 停止 / 中断 / 完了
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    params_json TEXT,
    message TEXT
);

CREATE TABLE job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    dealer_id TEXT,
    status TEXT NOT NULL DEFAULT '待機',    -- 待機 / 実行中 / 完了 / 失敗 / 停止
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX idx_job_items_job ON job_items(job_id);

-- 取得キャッシュ（バックアップには含めない）
CREATE TABLE fetch_cache (
    url TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    status_code INTEGER,
    final_url TEXT,
    content_type TEXT,
    body BLOB
);

-- 外部APIの使用量（無料枠の上限管理）
CREATE TABLE api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    used_at TEXT NOT NULL,
    units REAL NOT NULL,
    note TEXT
);

-- 共有モードの利用者とセッション
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def _migrate_v1(conn: sqlite3.Connection) -> None:
    # executescript は暗黙に COMMIT するため、1 文ずつ実行してトランザクション内に収める
    for stmt in _split_sql(SCHEMA_V1):
        conn.execute(stmt)


def _split_sql(script: str) -> list[str]:
    statements, buf = [], []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0].rstrip()
        if not stripped.strip():
            continue
        buf.append(stripped)
        if stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

# 版番号は 1 から連番。既存の移行は書き換えず、変更は新しい版として追加する。
MIGRATIONS: list[Migration] = [
    (1, "初期構造（販売店・連絡方法・根拠・履歴・候補・ジョブ・利用者）", _migrate_v1),
]


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if not row:
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def latest_version(migrations: list[Migration] | None = None) -> int:
    return max(v for v, _, _ in (migrations or MIGRATIONS))


def migrate(db_path: Path, backup_dir: Path, migrations: list[Migration] | None = None) -> list[str]:
    """未適用の移行を適用する。戻り値は実行内容の記録。"""
    migrations = sorted(migrations or MIGRATIONS, key=lambda m: m[0])
    log: list[str] = []
    existed = db_path.exists() and db_path.stat().st_size > 0
    conn = connect(db_path)
    try:
        version = current_version(conn)
        target = migrations[-1][0]
        if version > target:
            raise MigrationError(
                f"データベースの版（{version}）がこのアプリの対応版（{target}）より新しいため開けません。"
                "新しい版のアプリを使ってください。"
            )
        pending = [m for m in migrations if m[0] > version]
        if not pending:
            return log
        if existed and version > 0:
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = backup_dir / f"pre-migration-v{version}-{stamp}.sqlite3"
            _sqlite_copy(conn, dest)
            log.append(f"更新前のバックアップ: {dest}")
        for ver, name, fn in pending:
            try:
                with transaction(conn):
                    fn(conn)
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(ver),),
                    )
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES('app_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (__version__,),
                    )
                    conn.execute(
                        "INSERT INTO change_log(entity_type, entity_id, field, old_value, new_value, origin, changed_at) "
                        "VALUES('database', 'schema', 'schema_version', ?, ?, 'システム', ?)",
                        (str(ver - 1), str(ver), utcnow()),
                    )
            except Exception as exc:  # 巻き戻し済み
                raise MigrationError(
                    f"データベース更新（版 {ver}: {name}）に失敗したため、更新前の状態に戻しました: {exc}"
                ) from exc
            log.append(f"版 {ver} を適用: {name}")
        if version == 0:
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('created_at', ?)", (utcnow(),)
            )
        return log
    finally:
        conn.close()


def _sqlite_copy(conn: sqlite3.Connection, dest: Path) -> None:
    dest_conn = sqlite3.connect(str(dest))
    try:
        conn.backup(dest_conn)
    finally:
        dest_conn.close()
