// 確認済み初期データ（実在の英国販売店）の取り込み。1 回の処理で 1 社ずつ（CPU 制限のため）。
// 販売店 ID を固定したまま、DB に無い ID・ドメインだけを追加する（利用者が編集した販売店は上書きしない）。
// 試験用の架空データは test/ にだけ置き、ここには入れない。

import { getMeta, setMeta } from "./db";
import { SEED_FILES } from "./seed-files";
import { nowIso } from "./util";

export const SEED_TOTAL = SEED_FILES.length;

function insertStmt(db: D1Database, table: string, row: Record<string, unknown>, dropId = false): D1PreparedStatement {
  const cols = Object.keys(row).filter((c) => !(dropId && c === "id"));
  return db.prepare(`INSERT OR IGNORE INTO ${table}(${cols.join(",")}) VALUES(${cols.map(() => "?").join(",")})`)
    .bind(...cols.map((c) => (row[c] === undefined ? null : (row[c] as string | number | null))));
}

/** 次の 1 社を取り込む。戻り値は {done: 処理済み数, total, added: 追加したか} */
export async function seedStep(db: D1Database): Promise<{ done: number; total: number; added: boolean }> {
  const cursor = Number((await getMeta(db, "seed_cursor")) || 0);
  if (cursor >= SEED_TOTAL) return { done: SEED_TOTAL, total: SEED_TOTAL, added: false };
  const part = JSON.parse(SEED_FILES[cursor]);
  const d = part.dealer;
  const exists = await db.prepare("SELECT 1 FROM dealers WHERE id=? OR website_host=?").bind(d.id, d.website_host).first();
  let added = false;
  if (!exists) {
    const stmts: D1PreparedStatement[] = [insertStmt(db, "dealers", { ...d, data_origin: "初期調査データ" })];
    for (const c of part.contact_methods) stmts.push(insertStmt(db, "contact_methods", c));
    for (const r of part.research_runs) stmts.push(insertStmt(db, "research_runs", r));
    for (const e of part.evidence) stmts.push(insertStmt(db, "evidence", e, true));
    for (const c of part.change_log) stmts.push(insertStmt(db, "change_log", c, true));
    await db.batch(stmts); // 1 社分を 1 トランザクションで（途中で失敗したら何も入らない）
    added = true;
  }
  await setMeta(db, "seed_cursor", String(cursor + 1));
  if (cursor + 1 >= SEED_TOTAL) await setMeta(db, "seed_imported_at", nowIso());
  return { done: cursor + 1, total: SEED_TOTAL, added };
}

export async function seedStatus(db: D1Database): Promise<{ done: number; total: number }> {
  return { done: Math.min(Number((await getMeta(db, "seed_cursor")) || 0), SEED_TOTAL), total: SEED_TOTAL };
}

/** 「不足分を読み込む」：最初からもう一度たどる（既にある販売店は変更しない） */
export async function seedRestart(db: D1Database): Promise<void> {
  await setMeta(db, "seed_cursor", "0");
}
