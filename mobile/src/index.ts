// Samurai Dealer Research（iPhone 向け）— API と定期実行の入り口。
// 画面（public/）は静的ファイルとして配信され、/api/* だけをこの Worker が処理する。

import * as auth from "./auth";
import { backupInfo, backupRows, restoreBegin, restoreCommit, restoreRows } from "./backup";
import { BASES, CATEGORIES } from "./classify";
import { contactsCsv, dealersCsv, evidenceCsv, parseImport } from "./csv";
import { applyMigrations, clampSetting, currentVersion, DEFAULT_SETTINGS, ensureSchema, getMeta, getSettings, resetSchemaCache } from "./db";
import { CATEGORY_QUERIES, discoverFirecrawl, discoverOsm, manualSearchLinks, REGIONS } from "./discovery";
import { jobView, resumeJob, runSteps, startResearchJob, stopJob } from "./engine";
import { dohResolver } from "./netsafe";
import * as repo from "./repo";
import { LATEST_VERSION } from "./schema";
import { seedRestart, seedStatus, seedStep } from "./seed";
import { Env, HttpError, json, nowIso, SECURITY_HEADERS, str } from "./util";

type Row = Record<string, any>;
type Ctx = { req: Request; env: Env; url: URL; user: auth.User | null; body: any };

const MAX_BODY = 2_000_000;

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(req);
    try {
      return await handle(req, env, url, ctx);
    } catch (e) {
      if (e instanceof HttpError) return json({ error: e.message, ...e.extra }, e.status);
      console.error(e);
      return json({ error: "想定外のエラーが発生しました" }, 500);
    }
  },

  async scheduled(_event: ScheduledController, env: Env): Promise<void> {
    const schema = await ensureSchema(env.DB);
    if (schema.migrationRequired) return; // 更新前のバックアップを利用者が保存するまで進めない
    await runSteps(env.DB, { resolver: dohResolver }, 3);
  },
};

async function readBody(req: Request): Promise<any> {
  if (req.method !== "POST") return null;
  const len = Number(req.headers.get("content-length") || 0);
  if (len > MAX_BODY) throw new HttpError(413, "送信データが大きすぎます");
  const text = await req.text();
  if (text.length > MAX_BODY) throw new HttpError(413, "送信データが大きすぎます");
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "送信データの形式が正しくありません");
  }
}

async function handle(req: Request, env: Env, url: URL, ctx: ExecutionContext): Promise<Response> {
  const db = env.DB;
  const schema = await ensureSchema(db);
  const path = url.pathname;
  const method = req.method;
  if (method !== "GET" && method !== "POST") throw new HttpError(405, "許可されていない操作です");
  const token = auth.readCookie(req, auth.COOKIE);
  const user = await auth.sessionUser(db, token);
  const body = await readBody(req);
  const c: Ctx = { req, env, url, user, body };

  // ---- ログイン不要
  if (path === "/api/state" && method === "GET") {
    return json({
      app: "Samurai Dealer Research", version: "2.0.0", schema,
      setup_required: (await auth.userCount(db)) === 0,
      setup_token_configured: !!env.SETUP_TOKEN && env.SETUP_TOKEN.length >= 20,
      user: user ? { username: user.username, role: user.role, csrf: user.csrf } : null,
    });
  }
  if (method === "POST") checkOrigin(req, url);
  if (path === "/api/setup" && method === "POST") {
    const key = await auth.setupFirstAdmin(db, env.SETUP_TOKEN, str(body.setup_token), str(body.username));
    const r = await auth.login(db, str(body.username), key, ip(req));
    return json({ key, user: publicUser(r.user) }, 200, { "Set-Cookie": auth.sessionCookie(r.token) });
  }
  if (path === "/api/login" && method === "POST") {
    const r = await auth.login(db, str(body.username, 100), str(body.key, 200), ip(req));
    return json({ user: publicUser(r.user) }, 200, { "Set-Cookie": auth.sessionCookie(r.token) });
  }

  // ---- ここからログイン必須
  if (!user) throw new HttpError(401, "ログインが必要です");
  if (method === "POST") {
    const given = req.headers.get("x-csrf-token") || "";
    if (!given || given !== user.csrf) throw new HttpError(403, "画面を開き直してから操作してください（CSRF トークン不一致）");
  }
  if (path === "/api/logout" && method === "POST") {
    await auth.logout(db, token);
    return json({ ok: true }, 200, { "Set-Cookie": auth.clearCookie() });
  }
  if (path === "/api/me") return json({ user: publicUser(user), schema });

  // DB の更新が必要な間は、バックアップの取り出しと更新操作だけを受け付ける
  if (schema.migrationRequired) {
    if (path === "/api/migrate" && method === "POST") {
      requireAdmin(user);
      if (body.backup_saved !== true) throw new HttpError(400, "更新の前にバックアップを保存してください");
      const log = await applyMigrations(db, await currentVersion(db));
      resetSchemaCache();
      return json({ ok: true, log });
    }
    if (!path.startsWith("/api/backup/")) {
      throw new HttpError(409, "データベースの更新が必要です。設定画面でバックアップを保存してから更新してください", { migration_required: true, schema });
    }
  }

  const route = routes.find((r) => r.method === method && r.re.test(path));
  if (!route) throw new HttpError(404, "見つかりません");
  const m = path.match(route.re)!;
  return route.fn(c, m.slice(1), ctx);
}

function checkOrigin(req: Request, url: URL): void {
  const origin = req.headers.get("origin");
  if (origin && origin !== "null" && origin !== url.origin) throw new HttpError(403, "別のサイトからの操作は受け付けません");
  const site = req.headers.get("sec-fetch-site");
  if (site && site !== "same-origin" && site !== "none") throw new HttpError(403, "別のサイトからの操作は受け付けません");
}

const ip = (req: Request) => req.headers.get("cf-connecting-ip") || "local";
const publicUser = (u: auth.User) => ({ username: u.username, role: u.role, csrf: u.csrf });
function requireAdmin(u: auth.User | null): void {
  if (!u || u.role !== "admin") throw new HttpError(403, "管理者だけが操作できます");
}

function filtersFrom(p: URLSearchParams): repo.Filters {
  return {
    q: p.get("q") || "", region: p.get("region") || "", category: p.get("category") || "",
    has_email: p.get("has_email") === "1", has_form: p.get("has_form") === "1", needs_review: p.get("needs_review") === "1",
    status: p.get("status") || "", origin: p.get("origin") || "", include_archived: p.get("include_archived") === "1",
  };
}

function listItem(d: Row): Row {
  return {
    id: d.id, name: d.name || d.website_host, host: d.website_host, website_url: d.website_url, region: d.region, categories: d.categories,
    category_basis: d.category_basis, summary: d.summary, emails_ok: d.emails_ok.map((c: Row) => c.value),
    emails_review: d.emails_review.map((c: Row) => c.value), forms_ok: d.forms_ok.map((c: Row) => c.value),
    contact_pages: d.contact_pages.map((c: Row) => ({ url: c.value, status: c.status })), contact_summary: d.contact_summary,
    research_status: d.research_status, failure_reason: d.failure_reason, last_researched_at: d.last_researched_at,
    review_status: d.review_status, duplicate_status: d.duplicate_status, parent_dealer_id: d.parent_dealer_id, archived: d.archived,
    data_origin: d.data_origin,
  };
}

function csvResponse(text: string, name: string): Response {
  return new Response(text, {
    headers: { ...SECURITY_HEADERS, "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": `attachment; filename="${name}"` },
  });
}

async function firecrawlKey(env: Env): Promise<string> {
  if (env.FIRECRAWL_API_KEY) return env.FIRECRAWL_API_KEY;
  const r = await env.DB.prepare("SELECT value FROM secrets WHERE key='firecrawl_api_key'").first<{ value: string }>();
  return r?.value || "";
}

interface Route {
  method: "GET" | "POST";
  re: RegExp;
  fn: (c: Ctx, params: string[], ctx: ExecutionContext) => Promise<Response>;
}

const ID = "([A-Z]{3}-[0-9A-Z]{26})";
const by = (c: Ctx) => c.user?.username || null;

const routes: Route[] = [
  // ---------------------------------------------------------------- 一覧・詳細
  {
    method: "GET", re: /^\/api\/dealers$/, fn: async (c) => {
      const all = await repo.loadAllDealers(c.env.DB);
      const filtered = repo.filterDealers(all, filtersFrom(c.url.searchParams));
      return json({ dealers: filtered.map(listItem), stats: repo.stats(all), categories: CATEGORIES });
    },
  },
  {
    method: "GET", re: new RegExp(`^/api/dealers/${ID}$`), fn: async (c, [id]) => {
      const db = c.env.DB;
      const d = await repo.getDealer(db, id);
      if (!d) throw new HttpError(404, "販売店が見つかりません");
      const [evidence, history, runs] = await Promise.all([repo.listEvidence(db, id), repo.listHistory(db, id), repo.listRuns(db, id)]);
      const dup = d.duplicate_of ? await repo.getDealer(db, d.duplicate_of) : null;
      const parent = d.parent_dealer_id ? await repo.getDealer(db, d.parent_dealer_id) : null;
      const { results: branches } = await db.prepare("SELECT id, COALESCE(name_manual, name_auto) AS name FROM dealers WHERE parent_dealer_id=?").bind(id).all();
      return json({
        dealer: d, evidence, history, runs, bases: BASES, categories: CATEGORIES, contact_statuses: repo.CONTACT_MANUAL_STATUSES,
        dup: dup ? { id: dup.id, name: dup.name, host: dup.website_host } : null,
        parent: parent ? { id: parent.id, name: parent.name } : null, branches,
      });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/dealers/${ID}/edit$`), fn: async (c, [id]) => {
      const b = c.body || {};
      const values: Record<string, unknown> = {};
      for (const k of ["name", "region", "address", "summary", "notes", "eligibility_manual", "category_basis"]) if (k in b) values[k] = str(b[k], 5000);
      if (Array.isArray(b.categories)) values.categories = b.categories.map((x: unknown) => str(x, 50));
      if (b.website_url) values.website_url = str(b.website_url, 500);
      await repo.updateManual(c.env.DB, id, values, by(c));
      return json({ ok: true, message: "保存しました（人の確定値として記録。再調査で上書きされません）" });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/dealers/${ID}/review$`), fn: async (c, [id]) => {
      await repo.setReview(c.env.DB, id, str(c.body?.status, 20), by(c));
      return json({ ok: true, message: "確認状態を更新しました" });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/dealers/${ID}/research$`), fn: async (c, [id]) => {
      const job = await startResearchJob(c.env.DB, [id], "個別の再調査", by(c));
      return json({ ok: true, job_id: job, message: "再調査を開始しました。進捗画面で確認できます" });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/dealers/${ID}/contacts$`), fn: async (c, [id]) => {
      const b = c.body || {};
      await repo.addContactManual(c.env.DB, id, {
        kind: str(b.kind, 20), value: str(b.value, 500), source_url: str(b.source_url, 500) || undefined, note: str(b.note, 1000) || undefined,
        person_name: str(b.person_name, 100) || undefined, person_role: str(b.person_role, 100) || undefined,
      }, by(c));
      return json({ ok: true, message: "連絡方法を登録しました" });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/contacts/${ID}/status$`), fn: async (c, [id]) => {
      const dealerId = await repo.setContactStatus(c.env.DB, id, str(c.body?.status, 50) || null, by(c), str(c.body?.note, 500) || null);
      return json({ ok: true, dealer_id: dealerId, message: "連絡方法の状態を更新しました" });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/dealers/${ID}/duplicate$`), fn: async (c, [id]) => {
      const db = c.env.DB;
      const d = await repo.getDealer(db, id);
      if (!d) throw new HttpError(404, "販売店が見つかりません");
      const action = str(c.body?.action, 20);
      const map: Record<string, Record<string, unknown>> = {
        distinct: { duplicate_status: "別事業者と確認済み" },
        branch: { parent_dealer_id: str(c.body?.parent_id, 40) || d.duplicate_of || "", duplicate_status: "支店として関連付け" },
        unlink: { parent_dealer_id: "", duplicate_status: null },
        archive: { archived: 1, duplicate_status: "重複のため非表示" },
        unarchive: { archived: 0 },
      };
      if (!map[action]) throw new HttpError(400, "不明な操作です");
      await repo.updateManual(db, id, map[action], by(c));
      return json({ ok: true, message: "更新しました" });
    },
  },
  // ---------------------------------------------------------------- 調査
  {
    method: "POST", re: /^\/api\/research\/start$/, fn: async (c) => {
      const db = c.env.DB;
      const scope = str(c.body?.scope, 20);
      let ids: string[] = [];
      if (scope === "selected") ids = (Array.isArray(c.body?.dealer_ids) ? c.body.dealer_ids : []).map((x: unknown) => str(x, 40));
      else if (scope === "unresearched") ids = (await db.prepare("SELECT id FROM dealers WHERE research_status IN ('未調査','停止') AND archived=0").all<Row>()).results.map((r) => r.id);
      else if (scope === "failed") ids = (await db.prepare("SELECT id FROM dealers WHERE research_status IN ('取得失敗','一部取得失敗') AND archived=0").all<Row>()).results.map((r) => r.id);
      if (!ids.length) throw new HttpError(400, "調査する販売店がありません");
      const label = ({ selected: "選択した販売店", unresearched: "未調査の販売店", failed: "失敗した販売店の再実行" } as Record<string, string>)[scope] || "";
      const job = await startResearchJob(db, ids, label, by(c));
      return json({ ok: true, job_id: job, message: `${ids.length} 件の調査を開始しました` });
    },
  },
  {
    method: "POST", re: /^\/api\/tick$/, fn: async (c) => {
      const steps = await runSteps(c.env.DB, { resolver: dohResolver }, 3);
      const active = await c.env.DB.prepare("SELECT COUNT(*) AS n FROM jobs WHERE status='実行中' AND kind='research'").first<Row>();
      return json({ steps, active_jobs: Number(active?.n || 0) });
    },
  },
  { method: "GET", re: /^\/api\/jobs$/, fn: async (c) => {
      const { results } = await c.env.DB.prepare("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 30").all<Row>();
      return json({ jobs: await Promise.all(results.map((r) => jobView(c.env.DB, r.id))) });
    },
  },
  {
    method: "GET", re: new RegExp(`^/api/jobs/${ID}$`), fn: async (c, [id]) => {
      const job = await jobView(c.env.DB, id);
      if (!job) throw new HttpError(404, "ジョブが見つかりません");
      const { results: items } = await c.env.DB.prepare(
        "SELECT i.*, d.website_url, COALESCE(d.name_manual, d.name_auto, d.website_host) AS name, (SELECT COUNT(*) FROM research_tasks t WHERE t.run_id=i.run_id AND t.status IN ('完了','失敗','対象外')) AS pages_done, (SELECT COUNT(*) FROM research_tasks t WHERE t.run_id=i.run_id) AS pages_total FROM job_items i LEFT JOIN dealers d ON d.id=i.dealer_id WHERE i.job_id=? ORDER BY i.id LIMIT 500",
      ).bind(id).all<Row>();
      return json({ job, items });
    },
  },
  {
    method: "POST", re: new RegExp(`^/api/jobs/${ID}/(stop|resume|retry)$`), fn: async (c, [id, action]) => {
      const db = c.env.DB;
      if (action === "stop") await stopJob(db, id);
      else if (action === "resume") await resumeJob(db, id);
      else {
        const { results } = await db.prepare("SELECT dealer_id FROM job_items WHERE job_id=? AND status IN ('失敗','停止')").bind(id).all<Row>();
        if (!results.length) return json({ ok: true, message: "再実行する対象がありません" });
        const job = await startResearchJob(db, results.map((r) => r.dealer_id), "失敗・停止分の再実行", by(c));
        return json({ ok: true, job_id: job, message: "失敗・停止分を再実行します" });
      }
      return json({ ok: true, message: action === "stop" ? "停止しました" : "再開しました" });
    },
  },
  // ---------------------------------------------------------------- 追加・CSV 取込
  {
    method: "POST", re: /^\/api\/add\/(urls|csv)$/, fn: async (c, [kind]) => {
      const db = c.env.DB;
      let rows: { url: string; name?: string; region?: string; note?: string }[];
      let errors: string[] = [];
      if (kind === "urls") {
        rows = String(c.body?.urls || "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean).slice(0, 200).map((url) => ({ url }));
      } else {
        const parsed = parseImport(String(c.body?.text || ""));
        rows = parsed.rows;
        errors = parsed.errors;
      }
      const results: Row[] = [];
      const newIds: string[] = [];
      for (const r of rows) {
        try {
          const res = await repo.createDealer(db, r.url, kind === "urls" ? "利用者追加" : "CSV取込", { name: r.name, region: r.region, notes: r.note, by: by(c) || undefined });
          results.push({ input: r.url, id: res.id, note: res.note });
          if (res.created) newIds.push(res.id);
        } catch (e) {
          results.push({ input: r.url, id: null, note: (e as Error).message });
        }
      }
      const job = c.body?.start && newIds.length ? await startResearchJob(db, newIds, "追加したURLの調査", by(c)) : null;
      return json({ results, new_count: newIds.length, job_id: job, errors });
    },
  },
  // ---------------------------------------------------------------- 候補発見
  {
    method: "GET", re: /^\/api\/discover\/meta$/, fn: async (c) => {
      const settings = await getSettings(c.env.DB);
      const region = c.url.searchParams.get("region") || "Leeds（リーズ）";
      const category = c.url.searchParams.get("category") || "一般中古車";
      return json({
        regions: Object.keys(REGIONS), categories: Object.keys(CATEGORY_QUERIES), links: manualSearchLinks(category, region),
        firecrawl: { has_key: !!(await firecrawlKey(c.env)), used: await repo.apiUsageThisMonth(c.env.DB, "firecrawl"), cap: Number(settings.firecrawl_monthly_credit_cap), max_per_run: Number(settings.firecrawl_max_results_per_run) },
      });
    },
  },
  {
    method: "POST", re: /^\/api\/discover\/run$/, fn: async (c) => {
      const db = c.env.DB;
      const provider = str(c.body?.provider, 20);
      const region = str(c.body?.region, 100);
      const category = str(c.body?.category, 50);
      const limit = Math.max(1, Math.min(50, Number(c.body?.limit) || 20));
      if (!REGIONS[region]) throw new HttpError(400, "地域を一覧から選んでください");
      let found;
      let source: string;
      let used = 0;
      if (provider === "osm") {
        found = await discoverOsm(region, limit);
        source = "OpenStreetMap";
      } else if (provider === "firecrawl") {
        const settings = await getSettings(db);
        const cap = Number(settings.firecrawl_monthly_credit_cap);
        const usedNow = await repo.apiUsageThisMonth(db, "firecrawl");
        const n = Math.min(limit, Number(settings.firecrawl_max_results_per_run));
        const expected = Math.max(1, Math.ceil(n / 10)) * 2;
        if (usedNow + expected > cap) throw new HttpError(400, `今月の Firecrawl 使用上限（${cap} クレジット、使用済み ${usedNow}）に達するため実行しません`);
        const r = await discoverFirecrawl(await firecrawlKey(c.env), category, region, n);
        found = r.found;
        used = r.credits;
        await repo.recordApiUsage(db, "firecrawl", used, `${category} ${region}`);
        source = "Firecrawl";
      } else throw new HttpError(400, "不明な発見方法です");
      let added = 0, dup = 0, excluded = 0;
      for (const f of found) {
        const [id, note] = await repo.addCandidate(db, f.url, source, f.name, region, f.hint, f.detail);
        if (id && note === "候補に追加") added++;
        else if (id || note === "候補に登録済み") dup++;
        else excluded++;
      }
      return json({ message: `${source}: ${found.length} 件取得 → 新しい候補 ${added} 件・登録済み/重複 ${dup} 件・除外 ${excluded} 件${used ? `（消費 ${used} クレジット）` : ""}` });
    },
  },
  { method: "GET", re: /^\/api\/candidates$/, fn: async (c) => json({ candidates: await repo.listCandidates(c.env.DB, c.url.searchParams.get("status") || null) }) },
  {
    method: "POST", re: /^\/api\/candidates\/action$/, fn: async (c) => {
      const ids: string[] = (Array.isArray(c.body?.ids) ? c.body.ids : []).map((x: unknown) => str(x, 40));
      if (!ids.length) throw new HttpError(400, "候補を選んでください");
      const action = str(c.body?.action, 30);
      if (action === "exclude") {
        await repo.excludeCandidates(c.env.DB, ids);
        return json({ ok: true, message: "除外しました" });
      }
      const newIds = await repo.registerCandidates(c.env.DB, ids, by(c));
      const job = action === "register_research" && newIds.length ? await startResearchJob(c.env.DB, newIds, "候補から登録した販売店の調査", by(c)) : null;
      return json({ ok: true, job_id: job, message: `${newIds.length} 件を販売店として登録しました${job ? "。調査を開始しました" : ""}` });
    },
  },
  // ---------------------------------------------------------------- CSV 出力
  {
    method: "GET", re: /^\/api\/export\/(dealers|contacts|evidence)\.csv$/, fn: async (c, [kind]) => {
      const db = c.env.DB;
      if (kind === "dealers") return csvResponse(dealersCsv(await repo.listDealers(db, filtersFrom(c.url.searchParams))), "dealers.csv");
      const all = await repo.listDealers(db, { include_archived: true });
      if (kind === "contacts") return csvResponse(contactsCsv(all), "contacts.csv");
      const names = new Map(all.map((d) => [d.id as string, d.name as string]));
      const { results } = await db.prepare("SELECT * FROM evidence ORDER BY dealer_id, id LIMIT 20000").all<Row>();
      return csvResponse(evidenceCsv(results, names), "evidence.csv");
    },
  },
  // ---------------------------------------------------------------- 設定・利用者
  {
    method: "GET", re: /^\/api\/settings$/, fn: async (c) => {
      const db = c.env.DB;
      const { results: users } = c.user?.role === "admin"
        ? await db.prepare("SELECT id, username, role, created_at, disabled, last_login_at FROM users ORDER BY id").all<Row>()
        : { results: [] as Row[] };
      return json({
        settings: await getSettings(db), defaults: DEFAULT_SETTINGS, schema_version: await currentVersion(db), latest_version: LATEST_VERSION,
        firecrawl_key_set: !!(await firecrawlKey(c.env)), firecrawl_key_in_env: !!c.env.FIRECRAWL_API_KEY,
        firecrawl_used: await repo.apiUsageThisMonth(db, "firecrawl"), seed: await seedStatus(db),
        seed_imported_at: await getMeta(db, "seed_imported_at"), users, is_admin: c.user?.role === "admin",
      });
    },
  },
  {
    method: "POST", re: /^\/api\/settings$/, fn: async (c) => {
      requireAdmin(c.user);
      const stmts = [];
      for (const k of Object.keys(DEFAULT_SETTINGS)) {
        if (c.body?.[k] === undefined || c.body[k] === "") continue;
        stmts.push(c.env.DB.prepare("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(k, clampSetting(k, str(c.body[k], 20))));
      }
      if (stmts.length) await c.env.DB.batch(stmts);
      return json({ ok: true, message: "設定を保存しました（範囲外の値は上限・下限に合わせました）" });
    },
  },
  {
    method: "POST", re: /^\/api\/settings\/firecrawl-key$/, fn: async (c) => {
      requireAdmin(c.user);
      const key = str(c.body?.api_key, 200).trim();
      if (c.body?.clear) await c.env.DB.prepare("DELETE FROM secrets WHERE key='firecrawl_api_key'").run();
      else if (key) await c.env.DB.prepare("INSERT INTO secrets(key, value) VALUES('firecrawl_api_key', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(key).run();
      return json({ ok: true, message: "APIキーの設定を更新しました（バックアップ・CSVには含めません）" });
    },
  },
  {
    method: "POST", re: /^\/api\/users$/, fn: async (c) => {
      requireAdmin(c.user);
      const key = await auth.createUser(c.env.DB, str(c.body?.username, 40), c.body?.role === "admin" ? "admin" : "member");
      return json({ ok: true, key, message: "利用者を作成しました。ログイン用キーはこの画面でしか表示されません" });
    },
  },
  {
    method: "POST", re: /^\/api\/users\/(\d+)\/(reset|disable|enable)$/, fn: async (c, [id, action]) => {
      requireAdmin(c.user);
      const row = await c.env.DB.prepare("SELECT * FROM users WHERE id=?").bind(Number(id)).first<Row>();
      if (!row) throw new HttpError(404, "利用者が見つかりません");
      if (action === "reset") {
        const key = await auth.createUser(c.env.DB, row.username, row.role);
        return json({ ok: true, key, message: "ログイン用キーを発行し直しました（以前のキーとログイン状態は無効）" });
      }
      if (row.id === c.user!.id) throw new HttpError(400, "自分自身は無効にできません");
      await auth.setUserDisabled(c.env.DB, row.id, action === "disable");
      return json({ ok: true, message: action === "disable" ? "無効にしました" : "有効にしました" });
    },
  },
  // ---------------------------------------------------------------- バックアップ・復元・初期データ
  { method: "GET", re: /^\/api\/backup\/info$/, fn: async (c) => json(await backupInfo(c.env.DB)) },
  {
    method: "GET", re: /^\/api\/backup\/rows$/, fn: async (c) =>
      json(await backupRows(c.env.DB, c.url.searchParams.get("table") || "", Number(c.url.searchParams.get("after") || 0))),
  },
  {
    method: "POST", re: /^\/api\/restore\/(begin|rows|commit)$/, fn: async (c, [stage]) => {
      requireAdmin(c.user);
      if (stage === "begin") {
        if (c.body?.current_backup_saved !== true) throw new HttpError(400, "復元の前に現在のデータのバックアップを保存してください");
        await restoreBegin(c.env.DB, c.body?.manifest || {});
        return json({ ok: true });
      }
      if (stage === "rows") return json({ ok: true, inserted: await restoreRows(c.env.DB, str(c.body?.table, 40), c.body?.rows) });
      await restoreCommit(c.env.DB, c.body?.counts || {});
      return json({ ok: true, message: `復元しました（${nowIso()}）` });
    },
  },
  { method: "POST", re: /^\/api\/seed\/step$/, fn: async (c) => json(await seedStep(c.env.DB)) },
  {
    method: "POST", re: /^\/api\/seed\/restart$/, fn: async (c) => {
      await seedRestart(c.env.DB);
      return json({ ok: true, message: "初期データの不足分の読み込みを始めます（既にある販売店は変更しません）" });
    },
  },
];
