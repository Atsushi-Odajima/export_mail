// データベースの版管理（D1）。
// - 版は schema_meta.schema_version に記録する。既存の版は書き換えず、変更は新しい版として MIGRATIONS に追加する。
// - 各版は D1 の batch（1 トランザクション）で適用し、失敗すれば全体が巻き戻る。
// - 既にデータがある DB の更新は、画面で「バックアップを保存してから更新」を経て実行する（自動では更新しない）。
// - 表の構造は PC 版（Python）の版 1 と互換。販売店 ID・連絡方法 ID は同じ形式。

export interface Migration {
  version: number;
  name: string;
  statements: string[];
}

const V1 = `
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE dealers (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  website_url TEXT NOT NULL, website_host TEXT NOT NULL,
  name_auto TEXT, name_manual TEXT, region_auto TEXT, region_manual TEXT,
  address_auto TEXT, address_manual TEXT, postcode_auto TEXT,
  categories_auto TEXT, categories_manual TEXT,
  category_basis_auto TEXT, category_basis_manual TEXT, category_scope_auto TEXT,
  summary_auto TEXT, summary_manual TEXT, used_sales_auto TEXT, uk_check_auto TEXT, eligibility_manual TEXT,
  research_status TEXT NOT NULL DEFAULT '未調査', failure_reason TEXT, last_researched_at TEXT,
  review_status TEXT NOT NULL DEFAULT '未確認', reviewed_at TEXT, reviewed_by TEXT, notes TEXT,
  parent_dealer_id TEXT REFERENCES dealers(id), duplicate_status TEXT, duplicate_of TEXT,
  data_origin TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_dealers_host ON dealers(website_host);
CREATE TABLE contact_methods (
  id TEXT PRIMARY KEY, dealer_id TEXT NOT NULL REFERENCES dealers(id), kind TEXT NOT NULL,
  value TEXT NOT NULL, value_norm TEXT NOT NULL, purpose TEXT, status_auto TEXT, status_manual TEXT,
  exclusion_reason TEXT, person_name TEXT, person_role TEXT, source_url TEXT, excerpt TEXT,
  origin TEXT NOT NULL, first_observed_at TEXT, last_observed_at TEXT, human_verified_at TEXT,
  human_verified_by TEXT, is_current INTEGER NOT NULL DEFAULT 1, not_seen_since TEXT, created_at TEXT NOT NULL,
  UNIQUE (dealer_id, kind, value_norm)
);
CREATE INDEX idx_contacts_dealer ON contact_methods(dealer_id);
CREATE TABLE evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, dealer_id TEXT NOT NULL REFERENCES dealers(id),
  contact_method_id TEXT REFERENCES contact_methods(id), research_run_id TEXT, topic TEXT NOT NULL,
  source_url TEXT, excerpt TEXT, observed_at TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '自動'
);
CREATE INDEX idx_evidence_dealer ON evidence(dealer_id);
CREATE TABLE research_runs (
  id TEXT PRIMARY KEY, dealer_id TEXT NOT NULL REFERENCES dealers(id), job_id TEXT,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  pages_attempted INTEGER NOT NULL DEFAULT 0, pages_ok INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT, pages_json TEXT
);
CREATE INDEX idx_runs_dealer ON research_runs(dealer_id);
CREATE TABLE change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, field TEXT NOT NULL,
  old_value TEXT, new_value TEXT, origin TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT
);
CREATE INDEX idx_change_entity ON change_log(entity_type, entity_id);
CREATE TABLE candidates (
  id TEXT PRIMARY KEY, url TEXT NOT NULL, url_host TEXT NOT NULL, name TEXT, region TEXT, hint TEXT,
  source TEXT NOT NULL, source_detail TEXT, status TEXT NOT NULL DEFAULT '未処理', dealer_id TEXT, note TEXT,
  discovered_at TEXT NOT NULL
);
CREATE INDEX idx_candidates_host ON candidates(url_host);
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  params_json TEXT, message TEXT, created_by TEXT
);
CREATE TABLE job_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id), dealer_id TEXT,
  status TEXT NOT NULL DEFAULT '待機', error TEXT, started_at TEXT, finished_at TEXT, run_id TEXT
);
CREATE INDEX idx_job_items_job ON job_items(job_id, status);
CREATE TABLE research_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, dealer_id TEXT NOT NULL, url TEXT NOT NULL,
  host TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT '待機', error TEXT, facts TEXT,
  created_at TEXT NOT NULL, claimed_at TEXT, done_at TEXT
);
CREATE INDEX idx_tasks_status ON research_tasks(status, id);
CREATE INDEX idx_tasks_run ON research_tasks(run_id);
CREATE TABLE hosts (
  host TEXT PRIMARY KEY, next_allowed_ms INTEGER NOT NULL DEFAULT 0, robots_status TEXT, robots_txt TEXT,
  robots_fetched_at TEXT
);
CREATE TABLE api_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, used_at TEXT NOT NULL, units REAL NOT NULL, note TEXT
);
CREATE TABLE secrets (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member', created_at TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
  last_login_at TEXT
);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE login_failures (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, at_ms INTEGER NOT NULL);
CREATE INDEX idx_login_failures ON login_failures(key, at_ms);
`;

export function splitSql(sql: string): string[] {
  return sql
    .split(";")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export const MIGRATIONS: Migration[] = [
  { version: 1, name: "初期構造（販売店・連絡方法・根拠・履歴・候補・調査タスク・利用者）", statements: splitSql(V1) },
];

export const LATEST_VERSION = Math.max(...MIGRATIONS.map((m) => m.version));

// バックアップ・復元の対象（利用者・ログイン情報・取得キャッシュ・調査途中のタスク・APIキーは含めない）
// 並び順は復元時の挿入順（親 → 子）。削除は逆順。
export const BACKUP_TABLES = [
  "settings", "dealers", "contact_methods", "evidence", "research_runs", "change_log",
  "candidates", "jobs", "job_items", "api_usage",
] as const;
