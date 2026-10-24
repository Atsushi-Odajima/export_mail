// 調査エンジン（Workers 無料プランの CPU 10ms 制限に合わせ、1 回の処理で 1 ステップだけ進める）。
//
// 1 ステップ = 次のどれか 1 つ
//   a) 取得の終わった調査を分析して保存する
//   b) 待機中のページを 1 つ取得して読み取る（robots.txt 未取得のサイトは robots.txt を先に取得）
//   c) 待機中の販売店の調査を開始する（同時調査数まで）
// 1 分ごとの定期実行と、進捗画面を開いている間の画面からの呼び出しで進む。
// 同一サイトへの取得間隔（既定 2 秒以上）は hosts.next_allowed_ms で全体として守る。

import { analyse, chooseLinks, PageRecord } from "./analyse";
import { getSettings } from "./db";
import { PageFacts } from "./extract";
import { fetchPage } from "./fetcher";
import { hostKey, Resolver } from "./netsafe";
import { parseHtml } from "./parse";
import { applyResearch } from "./repo";
import { robotsAllows } from "./robots";
import { newId, nowIso } from "./util";

type Row = Record<string, any>;

export interface EngineDeps {
  resolver: Resolver | null;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

export interface StepResult {
  did: "analyse" | "fetch" | "robots" | "start" | "wait" | "idle";
  detail?: string;
}

// ---------------------------------------------------------------- ジョブ操作
export async function startResearchJob(db: D1Database, dealerIds: string[], label: string, by: string | null): Promise<string> {
  const id = newId("JOB");
  const now = nowIso();
  const ids = [...new Set(dealerIds)].slice(0, 500);
  const stmts = [
    db.prepare("INSERT INTO jobs(id, kind, status, created_at, updated_at, params_json, created_by) VALUES(?, 'research', '実行中', ?, ?, ?, ?)")
      .bind(id, now, now, JSON.stringify({ label }), by),
  ];
  for (const d of ids) {
    stmts.push(db.prepare("INSERT INTO job_items(job_id, dealer_id) VALUES(?, ?)").bind(id, d));
    stmts.push(db.prepare("UPDATE dealers SET research_status='待機中' WHERE id=?").bind(d));
  }
  for (let i = 0; i < stmts.length; i += 100) await db.batch(stmts.slice(i, i + 100));
  return id;
}

export async function stopJob(db: D1Database, jobId: string): Promise<void> {
  const job = await db.prepare("SELECT status FROM jobs WHERE id=?").bind(jobId).first<Row>();
  if (!job || job.status !== "実行中") return;
  const now = nowIso();
  await db.batch([
    // 未開始の販売店は「停止」（再開で続きから）。実行中の調査は取得済みページまでで分析して保存する
    db.prepare("UPDATE dealers SET research_status=CASE WHEN last_researched_at IS NULL THEN '未調査' ELSE '停止' END WHERE id IN (SELECT dealer_id FROM job_items WHERE job_id=? AND status='待機')").bind(jobId),
    db.prepare("UPDATE job_items SET status='停止', finished_at=? WHERE job_id=? AND status='待機'").bind(now, jobId),
    db.prepare("UPDATE research_tasks SET status='停止', done_at=? WHERE status='待機' AND run_id IN (SELECT run_id FROM job_items WHERE job_id=? AND status='実行中')").bind(now, jobId),
    db.prepare("UPDATE jobs SET status='停止', updated_at=?, message='停止しました。実行中だった販売店は取得済みのページまでで保存します。「再開」で残りを実行できます' WHERE id=?").bind(now, jobId),
  ]);
}

export async function resumeJob(db: D1Database, jobId: string): Promise<void> {
  const now = nowIso();
  await db.batch([
    db.prepare("UPDATE dealers SET research_status='待機中' WHERE id IN (SELECT dealer_id FROM job_items WHERE job_id=? AND status='停止')").bind(jobId),
    db.prepare("UPDATE job_items SET status='待機', error=NULL, finished_at=NULL, run_id=NULL WHERE job_id=? AND status='停止'").bind(jobId),
    db.prepare("UPDATE jobs SET status='実行中', updated_at=?, message='再開しました' WHERE id=?").bind(now, jobId),
  ]);
}

export async function jobView(db: D1Database, jobId: string): Promise<Row | null> {
  const job = await db.prepare("SELECT * FROM jobs WHERE id=?").bind(jobId).first<Row>();
  if (!job) return null;
  const { results } = await db.prepare("SELECT status, COUNT(*) AS n FROM job_items WHERE job_id=? GROUP BY status").bind(jobId).all<Row>();
  const counts: Record<string, number> = {};
  for (const r of results) counts[r.status] = Number(r.n);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return { ...job, params: JSON.parse(job.params_json || "{}"), counts, total, done: (counts["完了"] || 0) + (counts["失敗"] || 0) };
}

// ---------------------------------------------------------------- 1 ステップ
export async function step(db: D1Database, deps: EngineDeps): Promise<StepResult> {
  const now = deps.now ? deps.now() : Date.now();
  const settings = await getSettings(db);
  // 中断された実行（取得中に処理が打ち切られた等）を 2 分後に待機へ戻す
  await db.prepare("UPDATE research_tasks SET status='待機', claimed_at=NULL WHERE status='実行中' AND claimed_at < ?").bind(new Date(now - 120000).toISOString()).run();

  const ready = await db.prepare(
    "SELECT ji.id AS item_id, ji.job_id, ji.run_id, ji.dealer_id FROM job_items ji WHERE ji.status='実行中' AND NOT EXISTS (SELECT 1 FROM research_tasks t WHERE t.run_id=ji.run_id AND t.status IN ('待機','実行中')) ORDER BY ji.id LIMIT 1",
  ).first<Row>();
  if (ready) {
    await finishRun(db, ready);
    return { did: "analyse", detail: ready.dealer_id };
  }

  const task = await db.prepare(
    "SELECT t.* FROM research_tasks t LEFT JOIN hosts h ON h.host=t.host WHERE t.status='待機' AND COALESCE(h.next_allowed_ms, 0) <= ? ORDER BY t.id LIMIT 1",
  ).bind(now).first<Row>();
  if (task) {
    const claimed = await db.prepare("UPDATE research_tasks SET status='実行中', claimed_at=? WHERE id=? AND status='待機'").bind(new Date(now).toISOString(), task.id).run();
    if (!claimed.meta.changes) return { did: "wait" };
    return runTask(db, task, settings, deps, now);
  }

  const active = await db.prepare("SELECT COUNT(*) AS n FROM job_items WHERE status='実行中'").first<Row>();
  if (Number(active?.n || 0) < Number(settings.global_concurrency)) {
    const next = await db.prepare(
      "SELECT ji.id, ji.dealer_id, ji.job_id FROM job_items ji JOIN jobs j ON j.id=ji.job_id WHERE j.status='実行中' AND j.kind='research' AND ji.status='待機' ORDER BY j.created_at, ji.id LIMIT 1",
    ).first<Row>();
    if (next) {
      await startRun(db, next);
      return { did: "start", detail: next.dealer_id };
    }
    await closeFinishedJobs(db);
  }
  const waiting = await db.prepare("SELECT COUNT(*) AS n FROM research_tasks WHERE status='待機'").first<Row>();
  return Number(waiting?.n || 0) ? { did: "wait" } : { did: "idle" };
}

async function startRun(db: D1Database, item: Row): Promise<void> {
  const dealer = await db.prepare("SELECT id, website_url FROM dealers WHERE id=?").bind(item.dealer_id).first<Row>();
  const now = nowIso();
  if (!dealer) {
    await db.prepare("UPDATE job_items SET status='失敗', error='販売店が見つかりません', finished_at=? WHERE id=?").bind(now, item.id).run();
    return;
  }
  const runId = newId("RUN");
  await db.batch([
    db.prepare("INSERT INTO research_runs(id, dealer_id, job_id, started_at, status) VALUES(?,?,?,?, '調査中')").bind(runId, dealer.id, item.job_id, now),
    db.prepare("UPDATE job_items SET status='実行中', run_id=?, started_at=? WHERE id=?").bind(runId, now, item.id),
    db.prepare("UPDATE dealers SET research_status='調査中', updated_at=? WHERE id=?").bind(now, dealer.id),
    db.prepare("INSERT INTO research_tasks(run_id, dealer_id, url, host, role, created_at) VALUES(?,?,?,?, 'home', ?)")
      .bind(runId, dealer.id, dealer.website_url, new URL(dealer.website_url).hostname, now),
  ]);
}

async function runTask(db: D1Database, task: Row, settings: Record<string, string>, deps: EngineDeps, now: number): Promise<StepResult> {
  const intervalMs = Math.max(2, Number(settings.min_interval_sec)) * 1000;
  const host = task.host as string;
  const opts = { maxBytes: Number(settings.max_bytes), timeoutMs: Number(settings.timeout_sec) * 1000, resolver: deps.resolver, fetchImpl: deps.fetchImpl };
  const hostRow = await db.prepare("SELECT * FROM hosts WHERE host=?").bind(host).first<Row>();
  const robotsFresh = hostRow && hostRow.robots_fetched_at && now - Date.parse(hostRow.robots_fetched_at) < 24 * 3600 * 1000;

  if (!robotsFresh) {
    // robots.txt を先に取得（このサイトへの 1 回分の取得として数える）
    const base = new URL(task.url);
    const r = await fetchPage(`${base.protocol}//${base.host}/robots.txt`, { ...opts, acceptAnyType: true, maxBytes: 500000 });
    let status: string;
    let txt = "";
    if (r.status === 0) status = `error:${r.error}`;
    else if (r.status >= 500 || r.status === 429) status = `error:robots.txt が一時エラー（HTTP ${r.status}）`;
    else if (r.status >= 400 && !r.error.startsWith("アクセス制限")) status = "none"; // robots.txt なし＝制限なし
    else if (r.error) status = `error:${r.error}`;
    else {
      status = "ok";
      txt = r.text;
    }
    await db.batch([
      db.prepare("INSERT INTO hosts(host, next_allowed_ms, robots_status, robots_txt, robots_fetched_at) VALUES(?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET next_allowed_ms=excluded.next_allowed_ms, robots_status=excluded.robots_status, robots_txt=excluded.robots_txt, robots_fetched_at=excluded.robots_fetched_at")
        .bind(host, now + intervalMs, status, txt.slice(0, 200000), new Date(now).toISOString()),
      db.prepare("UPDATE research_tasks SET status='待機', claimed_at=NULL WHERE id=?").bind(task.id),
    ]);
    return { did: "robots", detail: host };
  }

  const finish = async (status: string, error: string, facts: PageFacts | null, nextAllowed: number | null) => {
    const stmts = [
      db.prepare("UPDATE research_tasks SET status=?, error=?, facts=?, done_at=? WHERE id=?").bind(status, error || null, facts ? JSON.stringify(facts) : null, nowIso(), task.id),
    ];
    if (nextAllowed !== null) stmts.push(db.prepare("UPDATE hosts SET next_allowed_ms=? WHERE host=?").bind(nextAllowed, host));
    await db.batch(stmts);
  };

  if (String(hostRow!.robots_status).startsWith("error:")) {
    await finish("失敗", `robots.txt を確認できないため取得を見送りました（${String(hostRow!.robots_status).slice(6)}）`, null, null);
    return { did: "fetch", detail: "robots-error" };
  }
  if (hostRow!.robots_status === "ok" && !robotsAllows(hostRow!.robots_txt || "", task.url)) {
    await finish("対象外", "robots.txt で取得が許可されていません", null, null);
    return { did: "fetch", detail: "robots-disallow" };
  }

  const res = await fetchPage(task.url, opts);
  const nextAllowed = Date.now() + intervalMs;
  if (res.error || !res.text) {
    await finish("失敗", res.error || `HTTP ${res.status}`, null, nextAllowed);
    return { did: "fetch", detail: res.error };
  }
  const finalUrl = res.finalUrl || task.url;
  if (task.role !== "home" && hostKey(finalUrl) !== hostKey(task.url)) {
    await finish("失敗", "別のサイトへ転送されたため読み取りません", null, nextAllowed);
    return { did: "fetch" };
  }
  const facts = await parseHtml(res.text, finalUrl, task.role, task.role === "stock" ? 60000 : 30000);
  facts.truncated = facts.truncated || res.truncated;
  const stmts: D1PreparedStatement[] = [];
  if (task.role === "home") {
    const limit = Math.max(0, Number(settings.max_pages_per_site) - 1);
    const links = chooseLinks(facts, finalUrl, limit);
    const ts = nowIso();
    for (const [href, role] of links) {
      stmts.push(db.prepare("INSERT INTO research_tasks(run_id, dealer_id, url, host, role, created_at) VALUES(?,?,?,?,?,?)").bind(task.run_id, task.dealer_id, href, new URL(href).hostname, role, ts));
    }
    facts.links = []; // 保存量を減らす（リンク選択は済んだ）
  } else {
    facts.links = [];
  }
  await finish("完了", "", facts, nextAllowed);
  if (stmts.length) await db.batch(stmts);
  return { did: "fetch", detail: task.url };
}

async function finishRun(db: D1Database, item: Row): Promise<void> {
  const { results: tasks } = await db.prepare("SELECT id, url, role, status, error, facts FROM research_tasks WHERE run_id=? ORDER BY id").bind(item.run_id).all<Row>();
  const dealer = await db.prepare("SELECT website_url FROM dealers WHERE id=?").bind(item.dealer_id).first<Row>();
  const pages: PageRecord[] = tasks.map((t) => ({
    url: t.url, role: t.role,
    status: t.status === "完了" ? "取得" : t.status === "対象外" ? "robots.txtで対象外" : t.status === "停止" ? "停止" : "失敗",
    error: t.error || "",
  }));
  const infos: PageFacts[] = tasks.filter((t) => t.facts).map((t) => JSON.parse(t.facts));
  const stopped = tasks.some((t) => t.status === "停止");
  const result = analyse(infos, pages, dealer?.website_url || "", stopped);
  await applyResearch(db, item.dealer_id, item.run_id, result);
  const now = nowIso();
  const itemStatus = result.status === "停止" ? "停止" : result.ok ? "完了" : "失敗";
  await db.batch([
    db.prepare("UPDATE job_items SET status=?, error=?, finished_at=? WHERE id=?").bind(itemStatus, result.failureReason || null, now, item.item_id),
    db.prepare("DELETE FROM research_tasks WHERE run_id=?").bind(item.run_id), // ページ本文は残さない（根拠の抜粋だけ保存済み）
  ]);
  await closeFinishedJobs(db);
}

async function closeFinishedJobs(db: D1Database): Promise<void> {
  const { results } = await db.prepare(
    "SELECT j.id FROM jobs j WHERE j.status='実行中' AND j.kind='research' AND NOT EXISTS (SELECT 1 FROM job_items i WHERE i.job_id=j.id AND i.status IN ('待機','実行中'))",
  ).all<Row>();
  for (const r of results) {
    const v = await jobView(db, r.id);
    const c = v?.counts || {};
    await db.prepare("UPDATE jobs SET status='完了', updated_at=?, message=? WHERE id=?")
      .bind(nowIso(), `完了 ${c["完了"] || 0} 件・失敗 ${c["失敗"] || 0} 件・停止 ${c["停止"] || 0} 件`, r.id).run();
  }
}

/** 定期実行・画面からの呼び出し用：時間・回数の上限内で数ステップ進める */
export async function runSteps(db: D1Database, deps: EngineDeps, maxSteps: number): Promise<StepResult[]> {
  const out: StepResult[] = [];
  for (let i = 0; i < maxSteps; i++) {
    const r = await step(db, deps);
    out.push(r);
    if (r.did === "idle" || r.did === "wait" || r.did === "fetch" || r.did === "robots" || r.did === "analyse") break; // CPU を使う処理は 1 回に 1 つ
  }
  return out;
}
