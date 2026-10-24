// 完全バックアップ・復元（ID・人の確定値・根拠・変更履歴・候補を含むデータベース単位）。
// iPhone の画面が表ごとに少しずつ取り出して 1 つの JSON ファイルにまとめる（1 回の処理を小さく保つため）。
// 復元は restore_* の一時表に読み込んでから、最後に 1 トランザクション（batch）で入れ替える。

import { currentVersion } from "./db";
import { BACKUP_TABLES, LATEST_VERSION } from "./schema";
import { HttpError, nowIso } from "./util";

export const BACKUP_FORMAT = "sdr-mobile-backup-1";
export const APP_VERSION = "2.0.0";
const PAGE = 500;

type Table = (typeof BACKUP_TABLES)[number];

function assertTable(name: string): Table {
  if (!(BACKUP_TABLES as readonly string[]).includes(name)) throw new HttpError(400, "対象外の表です");
  return name as Table;
}

export async function backupInfo(db: D1Database): Promise<Record<string, unknown>> {
  const counts: Record<string, number> = {};
  const res = await db.batch(BACKUP_TABLES.map((t) => db.prepare(`SELECT COUNT(*) AS n FROM ${t}`)));
  BACKUP_TABLES.forEach((t, i) => (counts[t] = Number((res[i].results[0] as any).n)));
  return {
    format: BACKUP_FORMAT, app: "SamuraiDealerResearch", app_version: APP_VERSION, schema_version: await currentVersion(db),
    created_at: nowIso(), counts, page_size: PAGE,
    note: "利用者・ログイン情報・APIキー・取得途中のデータは含みません",
  };
}

export async function backupRows(db: D1Database, table: string, afterRowid: number): Promise<{ rows: Record<string, unknown>[]; next: number | null }> {
  const t = assertTable(table);
  const { results } = await db.prepare(`SELECT rowid AS __rowid, * FROM ${t} WHERE rowid > ? ORDER BY rowid LIMIT ?`).bind(afterRowid, PAGE).all<Record<string, unknown>>();
  const next = results.length === PAGE ? Number(results[results.length - 1].__rowid) : null;
  for (const r of results) delete r.__rowid;
  return { rows: results, next };
}

async function columns(db: D1Database, table: string): Promise<string[]> {
  const { results } = await db.prepare(`PRAGMA table_info(${table})`).all<{ name: string }>();
  return results.map((r) => r.name);
}

export async function restoreBegin(db: D1Database, manifest: Record<string, unknown>): Promise<void> {
  if (manifest?.format !== BACKUP_FORMAT || manifest?.app !== "SamuraiDealerResearch") throw new HttpError(400, "このアプリのバックアップ形式ではありません");
  const ver = Number(manifest.schema_version);
  if (!ver || ver > LATEST_VERSION) throw new HttpError(400, "新しい版のアプリで作られたバックアップです。アプリを更新してから復元してください");
  if (ver !== (await currentVersion(db))) throw new HttpError(400, "データベースの版が異なるバックアップです。先にアプリのデータベース更新を行ってください");
  const stmts: D1PreparedStatement[] = [];
  for (const t of BACKUP_TABLES) {
    stmts.push(db.prepare(`DROP TABLE IF EXISTS restore_${t}`));
    stmts.push(db.prepare(`CREATE TABLE restore_${t} AS SELECT * FROM ${t} WHERE 0`));
  }
  await db.batch(stmts);
}

export async function restoreRows(db: D1Database, table: string, rows: Record<string, unknown>[]): Promise<number> {
  const t = assertTable(table);
  if (!Array.isArray(rows) || rows.length > 200) throw new HttpError(400, "1 回に送れる行は 200 行までです");
  const cols = await columns(db, t);
  const stmts: D1PreparedStatement[] = [];
  for (const r of rows) {
    if (!r || typeof r !== "object") throw new HttpError(400, "行の形式が不正です");
    const use = cols.filter((c) => c in r);
    if (!use.length) continue;
    stmts.push(db.prepare(`INSERT INTO restore_${t}(${use.join(",")}) VALUES(${use.map(() => "?").join(",")})`)
      .bind(...use.map((c) => (r[c] === undefined ? null : (r[c] as string | number | null)))));
  }
  if (stmts.length) await db.batch(stmts);
  return stmts.length;
}

export async function restoreCommit(db: D1Database, expected: Record<string, number>): Promise<void> {
  const counts = await db.batch(BACKUP_TABLES.map((t) => db.prepare(`SELECT COUNT(*) AS n FROM restore_${t}`)));
  BACKUP_TABLES.forEach((t, i) => {
    const n = Number((counts[i].results[0] as any).n);
    if (expected && expected[t] !== undefined && Number(expected[t]) !== n) {
      throw new HttpError(400, `${t} の行数がバックアップと一致しません（${n} / ${expected[t]}）。復元を中止しました（データは変更していません）`);
    }
  });
  const stmts: D1PreparedStatement[] = [db.prepare("PRAGMA defer_foreign_keys = on")];
  for (const t of [...BACKUP_TABLES].reverse()) stmts.push(db.prepare(`DELETE FROM ${t}`));
  stmts.push(db.prepare("DELETE FROM research_tasks"), db.prepare("DELETE FROM hosts"));
  for (const t of BACKUP_TABLES) {
    const cols = (await columns(db, t)).join(",");
    stmts.push(db.prepare(`INSERT INTO ${t}(${cols}) SELECT ${cols} FROM restore_${t}`));
  }
  for (const t of BACKUP_TABLES) stmts.push(db.prepare(`DROP TABLE restore_${t}`));
  stmts.push(db.prepare("INSERT INTO change_log(entity_type, entity_id, field, old_value, new_value, origin, changed_at) VALUES('database','restore','restored',NULL,?, 'システム', ?)").bind("バックアップから復元", nowIso()));
  await db.batch(stmts); // 1 トランザクション：途中で失敗すれば全体が巻き戻る
}
