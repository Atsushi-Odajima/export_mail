// データ操作の集約（画面処理・調査処理から SQL を直接書かない）。PC 版 sdr/repo.py と同じ規則。

import * as C from "./classify";
import { ContactFinding, ResearchResult } from "./analyse";
import { isAggregator, validEmail } from "./extract";
import { checkUrlSyntax, hostKey, normalizeUrl, UnsafeUrlError } from "./netsafe";
import { HttpError, newId, nowIso } from "./util";

type Row = Record<string, any>;

export const MANUAL_FIELDS = ["name", "region", "address", "categories", "category_basis", "summary"] as const;
export const CONTACT_KINDS: Record<string, string> = { email: "メール", form: "問い合わせフォーム", contact_page: "問い合わせページ", phone: "電話" };
export const REVIEW_STATUSES = ["未確認", "確認済み", "要確認"];
export const CONTACT_MANUAL_STATUSES = ["公開メール確認済み", "フォーム確認済み", "問い合わせページあり・フォーム未確認", "要手動確認", "除外", "公開番号確認済み", "取得失敗"];

const s = (v: unknown) => (v === undefined ? null : v === null ? null : typeof v === "string" ? v : JSON.stringify(v));

export function changeStmt(db: D1Database, type: string, id: string, field: string, oldV: unknown, newV: unknown, origin: string, by: string | null = null): D1PreparedStatement | null {
  if (s(oldV) === s(newV)) return null;
  return db.prepare("INSERT INTO change_log(entity_type, entity_id, field, old_value, new_value, origin, changed_at, changed_by) VALUES(?,?,?,?,?,?,?,?)")
    .bind(type, id, field, s(oldV), s(newV), origin, nowIso(), by);
}

async function runAll(db: D1Database, stmts: (D1PreparedStatement | null)[]): Promise<void> {
  const list = stmts.filter((x): x is D1PreparedStatement => !!x);
  if (list.length) await db.batch(list);
}

// ---------------------------------------------------------------- 販売店
export function validateSiteUrl(input: string): string {
  let url: string;
  try {
    url = normalizeUrl(input);
    checkUrlSyntax(url);
  } catch (e) {
    throw new HttpError(400, e instanceof UnsafeUrlError ? e.message : "URLが不正です");
  }
  if (isAggregator(url)) throw new HttpError(400, "車両掲載サイト・検索サイト・SNS 等は販売店の公式サイトとして登録できません");
  if (hostKey(url).endsWith(".ie")) throw new HttpError(400, "アイルランド（.ie）のサイトは対象外です。英国の事業者であれば手動で確認してください");
  return url;
}

export async function findByHost(db: D1Database, url: string): Promise<Row | null> {
  return db.prepare("SELECT * FROM dealers WHERE website_host=? ORDER BY created_at LIMIT 1").bind(hostKey(url)).first<Row>();
}

function normName(name: string | null | undefined): string {
  if (!name) return "";
  return name.toLowerCase().replace(/\b(ltd|limited|llp|plc|the|cars?|motors?|motor company|automotive|uk|group|of)\b/g, " ").replace(/[^a-z0-9]/g, "");
}

async function flagSimilarName(db: D1Database, dealerId: string, name: string | null): Promise<void> {
  const key = normName(name);
  if (key.length < 4) return;
  const { results } = await db.prepare("SELECT id, name_auto, name_manual FROM dealers WHERE id<>? AND archived=0").bind(dealerId).all<Row>();
  for (const r of results) {
    if (normName(r.name_manual || r.name_auto) === key) {
      await runAll(db, [
        db.prepare("UPDATE dealers SET duplicate_status='重複確認待ち', duplicate_of=? WHERE id=? AND (duplicate_status IS NULL OR duplicate_status<>'別事業者と確認済み')").bind(r.id, dealerId),
        changeStmt(db, "dealer", dealerId, "duplicate_status", null, `重複確認待ち（${r.id} と社名が類似）`, "システム"),
      ]);
      return;
    }
  }
}

export async function createDealer(db: D1Database, url: string, origin: string, opts: { name?: string; region?: string; notes?: string; by?: string } = {}): Promise<{ id: string; created: boolean; note: string }> {
  const site = validateSiteUrl(url);
  const existing = await findByHost(db, site);
  if (existing) return { id: existing.id, created: false, note: `同じドメインの販売店が登録済みです（${existing.id}）` };
  const now = nowIso();
  const id = newId("DLR");
  await runAll(db, [
    db.prepare("INSERT INTO dealers(id, created_at, updated_at, website_url, website_host, name_manual, region_manual, notes, data_origin) VALUES(?,?,?,?,?,?,?,?,?)")
      .bind(id, now, now, site, hostKey(site), opts.name || null, opts.region || null, opts.notes || null, origin),
    changeStmt(db, "dealer", id, "created", null, site, origin === "利用者追加" ? "手動" : "取込", opts.by || null),
  ]);
  await flagSimilarName(db, id, opts.name || null);
  return { id, created: true, note: "登録しました" };
}

function effective(row: Row, name: string): any {
  const manual = row[`${name}_manual`];
  const v = manual !== null && manual !== undefined && manual !== "" ? manual : row[`${name}_auto`];
  if (name === "categories") {
    try {
      return v ? JSON.parse(v) : [];
    } catch {
      return [];
    }
  }
  return v;
}

export function contactView(row: Row): Row {
  return {
    ...row,
    status: row.status_manual || row.status_auto,
    status_is_manual: !!row.status_manual,
    kind_label: CONTACT_KINDS[row.kind] || row.kind,
    purpose: row.purpose || "",
  };
}

export function dealerView(row: Row, contacts: Row[]): Row {
  const d: Row = { ...row };
  for (const f of MANUAL_FIELDS) {
    d[f] = effective(row, f);
    d[`${f}_is_manual`] = row[`${f}_manual`] !== null && row[`${f}_manual`] !== undefined && row[`${f}_manual`] !== "";
  }
  d.categories_auto_list = row.categories_auto ? JSON.parse(row.categories_auto) : [];
  d.categories_manual_list = row.categories_manual ? JSON.parse(row.categories_manual) : null;
  d.contacts = contacts;
  const current = contacts.filter((c) => c.is_current);
  d.emails_ok = current.filter((c) => c.kind === "email" && c.status === C.EMAIL_OK && !String(c.purpose).startsWith("除外"));
  d.emails_review = current.filter((c) => c.kind === "email" && c.status === C.EMAIL_REVIEW);
  d.forms_ok = current.filter((c) => c.kind === "form" && c.status === "フォーム確認済み");
  d.contact_pages = current.filter((c) => (c.kind === "contact_page" || c.kind === "form") && c.status !== "フォーム確認済み");
  d.phones = current.filter((c) => c.kind === "phone");
  d.has_email = d.emails_ok.length > 0;
  d.has_form = d.forms_ok.length > 0;
  d.contact_summary = d.has_email && d.has_form ? "メール・フォーム確認済み" : d.has_email ? "公開メール確認済み" : d.has_form ? "フォーム確認済み"
    : d.contact_pages.length || d.emails_review.length ? "要手動確認" : row.research_status === "取得失敗" ? "取得失敗"
    : ["未調査", "待機中", "調査中"].includes(row.research_status) ? "未調査" : "公開メール未確認";
  d.needs_review = row.review_status !== "確認済み" || d.categories.includes(C.CAT_REVIEW) || row.duplicate_status === "重複確認待ち" || current.some((c) => c.status === "要手動確認");
  d.used_sales = row.used_sales_auto;
  const uk = row.uk_check_auto || "";
  d.eligibility = row.eligibility_manual || (row.used_sales_auto === "中古車販売を確認" && uk.startsWith("英国") && !uk.includes("英国外") ? "対象（自動判定）" : "要確認");
  return d;
}

export async function listContacts(db: D1Database, dealerId: string): Promise<Row[]> {
  const { results } = await db.prepare("SELECT * FROM contact_methods WHERE dealer_id=? ORDER BY is_current DESC, kind, created_at").bind(dealerId).all<Row>();
  return results.map(contactView);
}

export async function getDealer(db: D1Database, id: string): Promise<Row | null> {
  const row = await db.prepare("SELECT * FROM dealers WHERE id=?").bind(id).first<Row>();
  return row ? dealerView(row, await listContacts(db, id)) : null;
}

export interface Filters {
  q?: string;
  region?: string;
  category?: string;
  has_email?: boolean;
  has_form?: boolean;
  needs_review?: boolean;
  status?: string;
  origin?: string;
  include_archived?: boolean;
}

export async function listDealers(db: D1Database, f: Filters = {}): Promise<Row[]> {
  return filterDealers(await loadAllDealers(db), f);
}

export async function loadAllDealers(db: D1Database): Promise<Row[]> {
  const [dealers, contacts] = await db.batch([
    db.prepare("SELECT * FROM dealers ORDER BY created_at"),
    db.prepare("SELECT * FROM contact_methods ORDER BY created_at"),
  ]);
  const by = new Map<string, Row[]>();
  for (const c of contacts.results as Row[]) {
    if (!by.has(c.dealer_id)) by.set(c.dealer_id, []);
    by.get(c.dealer_id)!.push(contactView(c));
  }
  return (dealers.results as Row[]).map((row) => dealerView(row, by.get(row.id) || []));
}

export function filterDealers(all: Row[], f: Filters = {}): Row[] {
  const q = (f.q || "").trim().toLowerCase();
  const region = (f.region || "").trim().toLowerCase();
  return all.filter((d) => {
    if (!f.include_archived && d.archived) return false;
    if (q && ![d.name, d.website_url, d.notes, d.id].join(" ").toLowerCase().includes(q)) return false;
    if (region && !String(d.region || "").toLowerCase().includes(region) && !String(d.address || "").toLowerCase().includes(region)) return false;
    if (f.category && !d.categories.includes(f.category)) return false;
    if (f.has_email && !d.has_email) return false;
    if (f.has_form && !d.has_form) return false;
    if (f.needs_review && !d.needs_review) return false;
    if (f.status && d.research_status !== f.status) return false;
    if (f.origin && d.data_origin !== f.origin) return false;
    return true;
  });
}

export function stats(dealers: Row[]): Row {
  const main = dealers.filter((d) => !d.parent_dealer_id && !d.archived);
  const cats: Record<string, number> = Object.fromEntries(C.CATEGORIES.map((c) => [c, 0]));
  for (const d of main) for (const c of d.categories) cats[c] = (cats[c] || 0) + 1;
  return {
    total: main.length,
    reviewed: main.filter((d) => d.review_status === "確認済み").length,
    with_email: main.filter((d) => d.has_email).length,
    with_form: main.filter((d) => d.has_form).length,
    with_email_or_form: main.filter((d) => d.has_email || d.has_form).length,
    unconfirmed_contact: main.filter((d) => !d.has_email && !d.has_form).length,
    failed: main.filter((d) => d.research_status === "取得失敗").length,
    categories: cats,
  };
}

export async function updateManual(db: D1Database, id: string, values: Record<string, any>, by: string | null): Promise<void> {
  const row = await db.prepare("SELECT * FROM dealers WHERE id=?").bind(id).first<Row>();
  if (!row) throw new HttpError(404, "販売店が見つかりません");
  const sets: string[] = [];
  const params: unknown[] = [];
  const logs: (D1PreparedStatement | null)[] = [];
  for (const [key, value] of Object.entries(values)) {
    let col: string;
    let nv: unknown;
    if ((MANUAL_FIELDS as readonly string[]).includes(key)) {
      col = `${key}_manual`;
      if (key === "categories") {
        const cats = (Array.isArray(value) ? value : []).filter((c: string) => C.CATEGORIES.includes(c));
        nv = cats.length ? JSON.stringify(cats) : null;
      } else if (key === "category_basis") nv = C.BASES.includes(value) ? value : null;
      else nv = String(value ?? "").trim().slice(0, 2000) || null;
    } else if (key === "notes" || key === "eligibility_manual") {
      col = key;
      nv = String(value ?? "").trim().slice(0, 5000) || null;
      if (key === "eligibility_manual" && nv && !["対象", "対象外", "要確認"].includes(nv as string)) nv = null;
    } else if (key === "website_url") {
      col = key;
      nv = validateSiteUrl(String(value));
      const other = await findByHost(db, nv as string);
      if (other && other.id !== id) throw new HttpError(400, `同じドメインの販売店が既にあります（${other.id}）`);
      sets.push("website_host=?");
      params.push(hostKey(nv as string));
    } else if (key === "parent_dealer_id") {
      col = key;
      nv = value || null;
      if (nv === id) throw new HttpError(400, "自分自身を本体に指定できません");
      if (nv && !(await db.prepare("SELECT 1 FROM dealers WHERE id=?").bind(nv).first())) throw new HttpError(400, "本体の販売店IDが見つかりません");
    } else if (key === "duplicate_status" || key === "archived") {
      col = key;
      nv = value;
    } else throw new HttpError(400, `編集できない項目です: ${key}`);
    if (row[col] !== nv) {
      logs.push(changeStmt(db, "dealer", id, col, row[col], nv, "手動", by));
      sets.push(`${col}=?`);
      params.push(nv);
    }
  }
  if (!sets.length) return;
  sets.push("updated_at=?");
  params.push(nowIso());
  await runAll(db, [db.prepare(`UPDATE dealers SET ${sets.join(", ")} WHERE id=?`).bind(...params, id), ...logs]);
}

export async function setReview(db: D1Database, id: string, status: string, by: string | null): Promise<void> {
  if (!REVIEW_STATUSES.includes(status)) throw new HttpError(400, "確認状態が不正です");
  const row = await db.prepare("SELECT review_status FROM dealers WHERE id=?").bind(id).first<Row>();
  if (!row) throw new HttpError(404, "販売店が見つかりません");
  const now = nowIso();
  await runAll(db, [
    changeStmt(db, "dealer", id, "review_status", row.review_status, status, "手動", by),
    db.prepare("UPDATE dealers SET review_status=?, reviewed_at=?, reviewed_by=?, updated_at=? WHERE id=?").bind(status, now, by, now, id),
  ]);
}

// ---------------------------------------------------------------- 連絡方法
export function normalizeContact(kind: string, value: string): string {
  const v = value.trim();
  if (kind === "email") return v.toLowerCase();
  if (kind === "phone") {
    let d = v.replace(/[^\d+]/g, "");
    if (d.startsWith("+44")) d = "0" + d.slice(3).replace(/^0+/, "");
    return d;
  }
  return v.replace(/\/+$/, "").toLowerCase();
}

export async function addContactManual(db: D1Database, dealerId: string, input: { kind: string; value: string; source_url?: string; note?: string; person_name?: string; person_role?: string }, by: string | null): Promise<string> {
  const kind = input.kind;
  if (!(kind in CONTACT_KINDS)) throw new HttpError(400, "連絡方法の種類が不正です");
  const value = String(input.value || "").trim().slice(0, 500);
  if (!value) throw new HttpError(400, "値を入力してください");
  if (!(await db.prepare("SELECT 1 FROM dealers WHERE id=?").bind(dealerId).first())) throw new HttpError(404, "販売店が見つかりません");
  if (kind === "email" && !validEmail(value)) throw new HttpError(400, "メールアドレスの形式が正しくありません");
  for (const u of [kind === "form" || kind === "contact_page" ? value : null, input.source_url || null]) {
    if (!u) continue;
    try {
      checkUrlSyntax(normalizeUrl(u));
    } catch (e) {
      throw new HttpError(400, `URLが不正です: ${(e as Error).message}`);
    }
  }
  const norm = normalizeContact(kind, value);
  const status = ({ email: "公開メール確認済み", form: "フォーム確認済み", contact_page: "問い合わせページあり・フォーム未確認", phone: "公開番号確認済み" } as Record<string, string>)[kind];
  const now = nowIso();
  const existing = await db.prepare("SELECT id FROM contact_methods WHERE dealer_id=? AND kind=? AND value_norm=?").bind(dealerId, kind, norm).first<Row>();
  if (existing) {
    await runAll(db, [
      db.prepare("UPDATE contact_methods SET status_manual=?, human_verified_at=?, human_verified_by=?, is_current=1, person_name=COALESCE(?, person_name), person_role=COALESCE(?, person_role) WHERE id=?")
        .bind(status, now, by, input.person_name || null, input.person_role || null, existing.id),
      changeStmt(db, "contact", existing.id, "status_manual", null, status, "手動", by),
    ]);
    return existing.id;
  }
  const id = newId("CNT");
  await runAll(db, [
    db.prepare("INSERT INTO contact_methods(id, dealer_id, kind, value, value_norm, purpose, status_auto, status_manual, person_name, person_role, source_url, excerpt, origin, first_observed_at, last_observed_at, human_verified_at, human_verified_by, is_current, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)")
      .bind(id, dealerId, kind, value, norm, "手動登録", null, status, input.person_name || null, input.person_role || null, input.source_url || null, input.note || null, "手動", now, now, now, by, now),
    input.source_url || input.note
      ? db.prepare("INSERT INTO evidence(dealer_id, contact_method_id, topic, source_url, excerpt, observed_at, origin) VALUES(?,?,?,?,?,?, '手動')").bind(dealerId, id, CONTACT_KINDS[kind], input.source_url || null, input.note || null, now)
      : null,
    changeStmt(db, "contact", id, "created", null, `${kind}:${value}`, "手動", by),
  ]);
  return id;
}

export async function setContactStatus(db: D1Database, contactId: string, status: string | null, by: string | null, note: string | null): Promise<string> {
  const row = await db.prepare("SELECT * FROM contact_methods WHERE id=?").bind(contactId).first<Row>();
  if (!row) throw new HttpError(404, "連絡方法が見つかりません");
  if (status && !CONTACT_MANUAL_STATUSES.includes(status)) throw new HttpError(400, "状態が不正です");
  const now = nowIso();
  await runAll(db, [
    changeStmt(db, "contact", contactId, "status_manual", row.status_manual, status || null, "手動", by),
    db.prepare("UPDATE contact_methods SET status_manual=?, human_verified_at=?, human_verified_by=?, exclusion_reason=COALESCE(?, exclusion_reason) WHERE id=?")
      .bind(status || null, status ? now : null, status ? by : null, note || null, contactId),
  ]);
  return row.dealer_id;
}

// ---------------------------------------------------------------- 調査結果の反映（人の確定値は上書きしない）
export async function applyResearch(db: D1Database, dealerId: string, runId: string, r: ResearchResult): Promise<void> {
  const now = nowIso();
  const row = await db.prepare("SELECT * FROM dealers WHERE id=?").bind(dealerId).first<Row>();
  if (!row) return;
  const stmts: (D1PreparedStatement | null)[] = [
    db.prepare("UPDATE research_runs SET finished_at=?, status=?, pages_attempted=?, pages_ok=?, error_summary=?, pages_json=? WHERE id=?")
      .bind(now, r.status, r.pages.length, r.pages.filter((p) => p.status === "取得").length, r.failureReason || null, JSON.stringify(r.pages), runId),
  ];
  if (!r.ok) {
    stmts.push(db.prepare("UPDATE dealers SET research_status=?, failure_reason=?, last_researched_at=?, updated_at=? WHERE id=?").bind(r.status, r.failureReason, now, now, dealerId));
    await runAll(db, stmts); // 既存の連絡方法・自動値は残す
    return;
  }
  const auto: Record<string, unknown> = {
    name_auto: r.name, region_auto: r.region, address_auto: r.address, postcode_auto: r.postcode,
    categories_auto: JSON.stringify(r.categories), category_basis_auto: r.categoryBasis, category_scope_auto: r.categoryScope,
    summary_auto: r.summary, used_sales_auto: r.usedSales, uk_check_auto: r.ukCheck,
  };
  const sets: string[] = [];
  const params: unknown[] = [];
  for (const [col, val] of Object.entries(auto)) {
    if (row[col] !== val) {
      stmts.push(changeStmt(db, "dealer", dealerId, col, row[col], val, "自動"));
      sets.push(`${col}=?`);
      params.push(val);
    }
  }
  sets.push("research_status=?", "failure_reason=?", "last_researched_at=?", "updated_at=?");
  params.push(r.status, r.failureReason || null, now, now);
  stmts.push(db.prepare(`UPDATE dealers SET ${sets.join(", ")} WHERE id=?`).bind(...params, dealerId));
  for (const [topic, url, ex] of r.evidence) {
    stmts.push(db.prepare("INSERT INTO evidence(dealer_id, research_run_id, topic, source_url, excerpt, observed_at) VALUES(?,?,?,?,?,?)").bind(dealerId, runId, topic, url, (ex || "").slice(0, 300), now));
  }
  const { results: existing } = await db.prepare("SELECT * FROM contact_methods WHERE dealer_id=?").bind(dealerId).all<Row>();
  const byKey = new Map(existing.map((c) => [`${c.kind}|${c.value_norm}`, c]));
  const seen = new Set<string>();
  for (const f of r.contacts) stmts.push(...contactStmts(db, dealerId, runId, f, byKey, seen, now));
  for (const c of existing) {
    if (c.origin === "自動" && c.is_current && !seen.has(c.id)) {
      stmts.push(db.prepare("UPDATE contact_methods SET is_current=0, not_seen_since=? WHERE id=?").bind(now, c.id));
      stmts.push(changeStmt(db, "contact", c.id, "is_current", "1", "0（再調査で未検出）", "自動"));
    }
  }
  await runAll(db, stmts);
  await flagSamePhone(db, dealerId);
  if (!row.name_manual) await flagSimilarName(db, dealerId, r.name);
}

function contactStmts(db: D1Database, dealerId: string, runId: string, f: ContactFinding, byKey: Map<string, Row>, seen: Set<string>, now: string): (D1PreparedStatement | null)[] {
  const norm = normalizeContact(f.kind, f.value);
  const ex = byKey.get(`${f.kind}|${norm}`);
  const out: (D1PreparedStatement | null)[] = [];
  let id: string;
  if (ex) {
    id = ex.id;
    out.push(changeStmt(db, "contact", id, "status_auto", ex.status_auto, f.status, "自動"));
    out.push(db.prepare("UPDATE contact_methods SET status_auto=?, purpose=CASE WHEN origin='手動' THEN purpose ELSE ? END, exclusion_reason=?, source_url=COALESCE(source_url, ?), excerpt=?, last_observed_at=?, is_current=1, not_seen_since=NULL WHERE id=?")
      .bind(f.status, f.purpose, f.reason || null, f.sourceUrl, f.excerpt.slice(0, 300), now, id));
  } else {
    id = newId("CNT");
    byKey.set(`${f.kind}|${norm}`, { id });
    out.push(db.prepare("INSERT INTO contact_methods(id, dealer_id, kind, value, value_norm, purpose, status_auto, exclusion_reason, source_url, excerpt, origin, first_observed_at, last_observed_at, is_current, created_at) VALUES(?,?,?,?,?,?,?,?,?,?, '自動', ?, ?, 1, ?)")
      .bind(id, dealerId, f.kind, f.value, norm, f.purpose, f.status, f.reason || null, f.sourceUrl, f.excerpt.slice(0, 300), now, now, now));
    out.push(changeStmt(db, "contact", id, "created", null, `${f.kind}:${f.value}`, "自動"));
  }
  seen.add(id);
  out.push(db.prepare("INSERT INTO evidence(dealer_id, contact_method_id, research_run_id, topic, source_url, excerpt, observed_at) VALUES(?,?,?,?,?,?,?)")
    .bind(dealerId, id, runId, CONTACT_KINDS[f.kind], f.sourceUrl, `[${f.status}] ${f.reason ? f.reason + " / " : ""}${f.excerpt}`.slice(0, 300), now));
  return out;
}

async function flagSamePhone(db: D1Database, dealerId: string): Promise<void> {
  const other = await db.prepare(
    "SELECT c2.dealer_id FROM contact_methods c1 JOIN contact_methods c2 ON c2.kind='phone' AND c2.value_norm=c1.value_norm AND c2.dealer_id<>c1.dealer_id JOIN dealers d ON d.id=c2.dealer_id AND d.archived=0 WHERE c1.dealer_id=? AND c1.kind='phone' LIMIT 1",
  ).bind(dealerId).first<Row>();
  if (!other) return;
  const cur = await db.prepare("SELECT duplicate_status, parent_dealer_id FROM dealers WHERE id=?").bind(dealerId).first<Row>();
  if (cur && cur.duplicate_status === null && cur.parent_dealer_id === null) {
    await runAll(db, [
      db.prepare("UPDATE dealers SET duplicate_status='重複確認待ち', duplicate_of=? WHERE id=?").bind(other.dealer_id, dealerId),
      changeStmt(db, "dealer", dealerId, "duplicate_status", null, `重複確認待ち（${other.dealer_id} と同じ電話番号）`, "システム"),
    ]);
  }
}

export async function listEvidence(db: D1Database, dealerId: string): Promise<Row[]> {
  const { results } = await db.prepare(
    "SELECT e.*, c.kind AS contact_kind, c.value AS contact_value FROM evidence e LEFT JOIN contact_methods c ON c.id=e.contact_method_id WHERE e.dealer_id=? ORDER BY e.observed_at DESC, e.id LIMIT 400",
  ).bind(dealerId).all<Row>();
  return results;
}

export async function listHistory(db: D1Database, dealerId: string): Promise<Row[]> {
  const { results } = await db.prepare(
    "SELECT * FROM change_log WHERE (entity_type='dealer' AND entity_id=?) OR (entity_type='contact' AND entity_id IN (SELECT id FROM contact_methods WHERE dealer_id=?)) ORDER BY id DESC LIMIT 200",
  ).bind(dealerId, dealerId).all<Row>();
  return results;
}

export async function listRuns(db: D1Database, dealerId: string): Promise<Row[]> {
  const { results } = await db.prepare("SELECT * FROM research_runs WHERE dealer_id=? ORDER BY started_at DESC LIMIT 10").bind(dealerId).all<Row>();
  return results.map((r) => ({ ...r, pages: r.pages_json ? JSON.parse(r.pages_json) : [] }));
}

// ---------------------------------------------------------------- 候補
export async function addCandidate(db: D1Database, url: string, source: string, name: string | null, region: string | null, hint: string | null, detail: string | null): Promise<[string | null, string]> {
  let u: string;
  try {
    u = normalizeUrl(url);
    checkUrlSyntax(u);
  } catch (e) {
    return [null, `除外（${(e as Error).message}）`];
  }
  if (isAggregator(u)) return [null, "除外（掲載サイト・検索サイト・SNS）"];
  if (hostKey(u).endsWith(".ie")) return [null, "除外（アイルランドのドメイン）"];
  if (await db.prepare("SELECT 1 FROM candidates WHERE url_host=?").bind(hostKey(u)).first()) return [null, "候補に登録済み"];
  const dealer = await findByHost(db, u);
  const id = newId("CAN");
  await db.prepare("INSERT INTO candidates(id, url, url_host, name, region, hint, source, source_detail, status, dealer_id, discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)")
    .bind(id, u, hostKey(u), name, region, hint, source, detail, dealer ? "重複" : "未処理", dealer ? dealer.id : null, nowIso()).run();
  return [id, dealer ? "販売店として登録済み" : "候補に追加"];
}

export async function listCandidates(db: D1Database, status: string | null): Promise<Row[]> {
  const stmt = status
    ? db.prepare("SELECT * FROM candidates WHERE status=? ORDER BY discovered_at DESC LIMIT 500").bind(status)
    : db.prepare("SELECT * FROM candidates ORDER BY discovered_at DESC LIMIT 500");
  return (await stmt.all<Row>()).results;
}

export async function registerCandidates(db: D1Database, ids: string[], by: string | null): Promise<string[]> {
  const out: string[] = [];
  for (const cid of ids.slice(0, 100)) {
    const c = await db.prepare("SELECT * FROM candidates WHERE id=?").bind(cid).first<Row>();
    if (!c || c.status !== "未処理") continue;
    try {
      const r = await createDealer(db, c.url, "候補発見", { notes: `候補発見元: ${c.source} ${c.source_detail || ""}`.trim(), by: by || undefined });
      await db.prepare("UPDATE candidates SET status=?, dealer_id=? WHERE id=?").bind(r.created ? "登録済み" : "重複", r.id, cid).run();
      if (r.created) out.push(r.id);
    } catch (e) {
      await db.prepare("UPDATE candidates SET status='除外', note=? WHERE id=?").bind((e as Error).message, cid).run();
    }
  }
  return out;
}

export async function excludeCandidates(db: D1Database, ids: string[]): Promise<void> {
  await runAll(db, ids.slice(0, 200).map((id) => db.prepare("UPDATE candidates SET status='除外', note='利用者が除外' WHERE id=? AND status='未処理'").bind(id)));
}

export async function recordApiUsage(db: D1Database, provider: string, units: number, note: string): Promise<void> {
  await db.prepare("INSERT INTO api_usage(provider, used_at, units, note) VALUES(?,?,?,?)").bind(provider, nowIso(), units, note).run();
}

export async function apiUsageThisMonth(db: D1Database, provider: string): Promise<number> {
  const month = new Date().toISOString().slice(0, 7);
  const r = await db.prepare("SELECT COALESCE(SUM(units), 0) AS n FROM api_usage WHERE provider=? AND substr(used_at, 1, 7)=?").bind(provider, month).first<Row>();
  return Number(r?.n || 0);
}
