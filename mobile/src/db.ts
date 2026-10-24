// D1 の接続補助・版管理・設定。

import { LATEST_VERSION, MIGRATIONS } from "./schema";
import { HttpError, nowIso } from "./util";

export const DEFAULT_SETTINGS: Record<string, string> = {
  max_pages_per_site: "10",
  min_interval_sec: "2",
  global_concurrency: "2",
  timeout_sec: "15",
  max_bytes: "1000000",
  cache_hours: "24",
  firecrawl_monthly_credit_cap: "200",
  firecrawl_max_results_per_run: "20",
};

// サイトに負荷をかけないための許容範囲（範囲外は上限・下限に合わせる）
export const SETTING_LIMITS: Record<string, [number, number]> = {
  max_pages_per_site: [1, 15],
  min_interval_sec: [2, 60],
  global_concurrency: [1, 3],
  timeout_sec: [5, 30],
  max_bytes: [100000, 2000000],
  cache_hours: [0, 720],
  firecrawl_monthly_credit_cap: [0, 1000],
  firecrawl_max_results_per_run: [1, 50],
};

export async function currentVersion(db: D1Database): Promise<number> {
  const t = await db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").first();
  if (!t) return 0;
  const row = await db.prepare("SELECT value FROM schema_meta WHERE key='schema_version'").first<{ value: string }>();
  return row ? Number(row.value) : 0;
}

let checkedVersion: number | null = null; // 同じ isolate 内では毎回確認しない

export interface SchemaState {
  version: number;
  latest: number;
  migrationRequired: boolean;
}

/** 空の DB なら初期構造を作る。既存 DB の版が古い場合は自動では更新せず、状態だけ返す。 */
export async function ensureSchema(db: D1Database): Promise<SchemaState> {
  if (checkedVersion === LATEST_VERSION) return { version: LATEST_VERSION, latest: LATEST_VERSION, migrationRequired: false };
  let version = await currentVersion(db);
  if (version === 0) {
    await applyMigrations(db, 0);
    version = LATEST_VERSION;
  }
  if (version > LATEST_VERSION) {
    throw new HttpError(500, `データベースの版（${version}）がこのアプリ（${LATEST_VERSION}）より新しいため開けません。アプリを更新してください。`);
  }
  if (version === LATEST_VERSION) checkedVersion = version;
  return { version, latest: LATEST_VERSION, migrationRequired: version < LATEST_VERSION };
}

/** 未適用の版を 1 つずつトランザクション（batch）で適用する。失敗した版は巻き戻る。 */
export async function applyMigrations(db: D1Database, from: number, migrations = MIGRATIONS): Promise<string[]> {
  const log: string[] = [];
  for (const m of migrations.filter((x) => x.version > from).sort((a, b) => a.version - b.version)) {
    const stmts = m.statements.map((s) => db.prepare(s));
    stmts.push(
      db.prepare("INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(String(m.version)),
      db.prepare("INSERT INTO change_log(entity_type, entity_id, field, old_value, new_value, origin, changed_at) VALUES('database','schema','schema_version',?,?,'システム',?)")
        .bind(String(m.version - 1), String(m.version), nowIso()),
    );
    if (m.version === 1) {
      stmts.push(db.prepare("INSERT OR IGNORE INTO schema_meta(key, value) VALUES('created_at', ?)").bind(nowIso()));
    }
    try {
      await db.batch(stmts);
    } catch (e) {
      throw new HttpError(500, `データベース更新（版 ${m.version}: ${m.name}）に失敗したため、更新前の状態のままです: ${(e as Error).message}`);
    }
    log.push(`版 ${m.version} を適用: ${m.name}`);
  }
  checkedVersion = null;
  return log;
}

export function resetSchemaCache(): void {
  checkedVersion = null;
}

export async function getSettings(db: D1Database): Promise<Record<string, string>> {
  const out = { ...DEFAULT_SETTINGS };
  const { results } = await db.prepare("SELECT key, value FROM settings").all<{ key: string; value: string }>();
  for (const r of results) out[r.key] = r.value;
  return out;
}

export function clampSetting(key: string, value: string): string {
  if (!(key in SETTING_LIMITS)) throw new HttpError(400, `不明な設定です: ${key}`);
  const n = Number(value);
  if (!Number.isFinite(n)) throw new HttpError(400, `${key} には数値を入力してください`);
  const [lo, hi] = SETTING_LIMITS[key];
  return String(Math.round(Math.min(Math.max(n, lo), hi)));
}

export async function getMeta(db: D1Database, key: string): Promise<string | null> {
  const row = await db.prepare("SELECT value FROM schema_meta WHERE key=?").bind(key).first<{ value: string }>();
  return row ? row.value : null;
}

export async function setMeta(db: D1Database, key: string, value: string): Promise<void> {
  await db.prepare("INSERT INTO schema_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(key, value).run();
}
