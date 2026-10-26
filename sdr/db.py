"""SQLite 接続とデータベースの版管理（移行処理）。

- 版は schema_meta.schema_version に記録する。
- 起動時に未適用の移行を順番に適用する。適用前に DB ファイルを backups/ へ複製する。
- 各移行は 1 トランザクションで実行し、失敗したら巻き戻して起動を中止する
  （データは移行前の状態のまま残る。復旧手順は README「更新に失敗したとき」）。
- 版 2 で営業（担当者・案件・文面・承認・送信アカウント・会話・予約・送信試行・連絡拒否・AI 利用・操作履歴）を追加した。
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


SCHEMA_V2 = """
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member';

-- 確認できた担当者（不明でも販売店・案件は登録できる）
CREATE TABLE people (
    id TEXT PRIMARY KEY,                    -- PSN-ULID
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    name TEXT, role TEXT, email TEXT, phone TEXT, source_url TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_people_dealer ON people(dealer_id);

-- 営業案件（同じ販売店に複数可。調査状態・送信停止とは別の状態）
CREATE TABLE deals (
    id TEXT PRIMARY KEY,                    -- DEA-ULID
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    person_id TEXT REFERENCES people(id),
    title TEXT NOT NULL,
    owner TEXT,
    stage TEXT NOT NULL DEFAULT '未着手',
    next_action_at TEXT, next_action TEXT, notes TEXT,
    send_allowed INTEGER NOT NULL DEFAULT 0, -- 人が判断した「この案件で営業メールを送ってよいか」
    send_basis TEXT,                         -- その判断の根拠（例：法人の公開営業窓口、事前の了承）
    created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_deals_dealer ON deals(dealer_id);

-- 自社情報（署名・紹介文。未入力の項目は文面で差し込み未完了として検出する）
CREATE TABLE company_profile (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE templates (
    id TEXT PRIMARY KEY,                    -- TPL-ULID
    name TEXT NOT NULL,
    step TEXT NOT NULL,                     -- initial / followup1 / followup2 / reactivation / reply
    category TEXT NOT NULL,                 -- 一般中古車 / スポーツカー / ミニバン・SUV / クラシックカー / 共通
    subject TEXT NOT NULL, body TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE template_versions (
    template_id TEXT NOT NULL REFERENCES templates(id), version INTEGER NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT,
    PRIMARY KEY (template_id, version)
);

-- 下書き（版を持つ。承認は特定の版の内容ハッシュに結び付く）
CREATE TABLE drafts (
    id TEXT PRIMARY KEY,                    -- DRF-ULID
    deal_id TEXT NOT NULL REFERENCES deals(id),
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    step TEXT NOT NULL,
    account_id TEXT,
    to_contact_id TEXT,
    to_address TEXT NOT NULL DEFAULT '', cc TEXT NOT NULL DEFAULT '', bcc TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,                   -- 手動 / 定型文 / AI / AI手動貼付
    template_id TEXT, template_version INTEGER, ai_usage_id INTEGER,
    basis_json TEXT,                        -- 文面作成に使った事実と根拠
    summary_ja TEXT,                        -- 日本語の内容確認（AI 利用時のみ。翻訳済みとは表示しない）
    reply_to_message_id TEXT,               -- 返信下書きの元になった受信メッセージ
    status TEXT NOT NULL DEFAULT '下書き',
    created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_drafts_deal ON drafts(deal_id);
CREATE TABLE draft_versions (
    draft_id TEXT NOT NULL REFERENCES drafts(id), version INTEGER NOT NULL,
    to_address TEXT, cc TEXT, bcc TEXT, subject TEXT, body TEXT, source TEXT, account_id TEXT,
    created_at TEXT NOT NULL, created_by TEXT,
    PRIMARY KEY (draft_id, version)
);
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,                    -- ATT-ULID
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
    stored_name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    draft_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    approved_by TEXT, approved_at TEXT NOT NULL,
    revoked_at TEXT, revoked_reason TEXT
);
CREATE INDEX idx_approvals_draft ON approvals(draft_id);

-- 送信アカウント（認証情報は DB に置かず、OS の資格情報保管に置く）
CREATE TABLE mail_accounts (
    id TEXT PRIMARY KEY,                    -- ACC-ULID
    provider TEXT NOT NULL,                 -- test / smtp_imap / gmail / graph
    label TEXT NOT NULL,
    from_address TEXT NOT NULL, from_name TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    signature TEXT,
    daily_limit INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '未接続',
    status_detail TEXT,
    last_sync_at TEXT, last_sync_ok_at TEXT, sync_error TEXT, sync_cursor TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE threads (
    id TEXT PRIMARY KEY,                    -- THR-ULID
    account_id TEXT, dealer_id TEXT, deal_id TEXT,
    subject TEXT, external_thread_id TEXT,
    last_message_at TEXT, created_at TEXT NOT NULL
);
CREATE INDEX idx_threads_ext ON threads(account_id, external_thread_id);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,                    -- MSG-ULID
    thread_id TEXT REFERENCES threads(id),
    account_id TEXT NOT NULL,
    direction TEXT NOT NULL,                -- out / in
    message_id_header TEXT, in_reply_to TEXT, references_hdr TEXT,
    external_id TEXT, external_thread_id TEXT,
    from_addr TEXT, to_addrs TEXT, cc_addrs TEXT, subject TEXT, body_text TEXT,
    sent_at TEXT, received_at TEXT,
    dealer_id TEXT, deal_id TEXT, draft_id TEXT, schedule_id TEXT,
    kind TEXT NOT NULL,                     -- 送信 / 返信 / 自動応答 / 不達通知 / 配信停止要求 / 拒否の疑い / 未分類
    link_method TEXT,                       -- 返信参照 / 会話ID / 送信元アドレス一致 / 送信元ドメイン一致（要確認） / 手動
    needs_action INTEGER NOT NULL DEFAULT 0,
    handled_at TEXT, handled_by TEXT, note TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_messages_ext ON messages(account_id, external_id);
CREATE INDEX idx_messages_mid ON messages(message_id_header);
CREATE INDEX idx_messages_deal ON messages(deal_id);

-- 予約（一連の予定＝sequence）。本文は承認済みの下書き版を送る
CREATE TABLE sequences (
    id TEXT PRIMARY KEY,                    -- SEQ-ULID
    deal_id TEXT NOT NULL REFERENCES deals(id),
    name TEXT NOT NULL,
    stop_on_reply INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '有効',
    created_by TEXT, created_at TEXT NOT NULL
);
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,                    -- SCH-ULID
    sequence_id TEXT REFERENCES sequences(id),
    deal_id TEXT NOT NULL REFERENCES deals(id),
    dealer_id TEXT NOT NULL REFERENCES dealers(id),
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    account_id TEXT NOT NULL,
    step TEXT NOT NULL,
    send_at_utc TEXT NOT NULL,
    input_tz TEXT NOT NULL,
    status TEXT NOT NULL,                   -- 承認待ち / 予約済み / 送信中 / 送信済み / 送信前に失敗 / 結果確認待ち / 保留 / 一時停止 / 取消
    hold_reason TEXT,
    claimed_by TEXT, claimed_at TEXT,
    sent_message_id TEXT,
    created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_schedules_due ON schedules(status, send_at_utc);
CREATE INDEX idx_schedules_dealer ON schedules(dealer_id);

-- 送信試行（同じ内容を二重に送らないための記録。idempotency_key は一意）
CREATE TABLE send_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    schedule_id TEXT, draft_id TEXT NOT NULL, draft_version INTEGER NOT NULL,
    account_id TEXT NOT NULL, instance_id TEXT NOT NULL,
    test_mode INTEGER NOT NULL DEFAULT 0,
    actual_recipients TEXT,
    message_id_header TEXT, provider_message_id TEXT, provider_thread_id TEXT,
    result TEXT NOT NULL,                   -- 送信中 / 受付済み / 送信前に失敗 / 結果不明 / 照合で受付確認 / 照合で未送信確認 / 人が送信済みと判断 / 人が未送信と判断
    error TEXT,
    started_at TEXT NOT NULL, finished_at TEXT
);

-- 連絡拒否・送信停止（範囲ごと。無効な宛先と会社全体の拒否は別の範囲）
CREATE TABLE suppressions (
    id TEXT PRIMARY KEY,                    -- SUP-ULID
    scope TEXT NOT NULL,                    -- 会社 / 個人 / アドレス / ドメイン / 無効な宛先
    dealer_id TEXT, person_id TEXT, address TEXT, domain TEXT,
    status TEXT NOT NULL,                   -- 確定 / 確認待ち / 解除
    reason TEXT, source TEXT NOT NULL,      -- 手動 / 返信 / 不達通知 / 停止リンク
    message_id TEXT,
    created_at TEXT NOT NULL, created_by TEXT,
    resolved_at TEXT, resolved_by TEXT
);
CREATE INDEX idx_suppressions_addr ON suppressions(address);
CREATE INDEX idx_suppressions_dealer ON suppressions(dealer_id);
CREATE TABLE unsubscribe_tokens (
    token_hash TEXT PRIMARY KEY, dealer_id TEXT, address TEXT NOT NULL,
    created_at TEXT NOT NULL, used_at TEXT
);

CREATE TABLE ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL, model TEXT, purpose TEXT, dealer_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL
);

-- 操作履歴（秘密情報・本文は残さない）
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, detail TEXT
);
CREATE INDEX idx_audit_at ON audit_log(at);

-- 稼働状態（送信モード・送信担当の稼働環境・照合待ち等）
CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- テスト用メール接続先の受信箱・送信箱（外部へは出さない）
CREATE TABLE test_mailbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    folder TEXT NOT NULL,                   -- sent / inbox
    raw TEXT NOT NULL,
    message_id_header TEXT,
    created_at TEXT NOT NULL
);
"""


def _migrate_v2(conn: sqlite3.Connection) -> None:
    for stmt in _split_sql(SCHEMA_V2):
        conn.execute(stmt)
    # 既存の共有モード利用者のうち最初の 1 人を管理者にする（既定パスワードは作らない）
    conn.execute("UPDATE users SET role='admin' WHERE id=(SELECT MIN(id) FROM users)")


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

# 版番号は 1 から連番。既存の移行は書き換えず、変更は新しい版として追加する。
MIGRATIONS: list[Migration] = [
    (1, "初期構造（販売店・連絡方法・根拠・履歴・候補・ジョブ・利用者）", _migrate_v1),
    (2, "営業機能（担当者・案件・文面・承認・メール接続・会話・予約・送信試行・連絡拒否・AI利用・操作履歴）", _migrate_v2),
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
