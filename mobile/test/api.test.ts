// API の結合試験（本番と同じバンドル・workerd・D1。外部通信は架空サイトへ差し替え）。
import { Miniflare } from "miniflare";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Client, dealerSite, FakeWeb, setupAdmin, startApp } from "./harness";

const BASE = "https://www.test-motors.example";
let mf: Miniflare;
let origin: string;
let web: FakeWeb;

beforeEach(async () => {
  web = new FakeWeb();
  dealerSite(web);
  ({ mf, origin } = await startApp(web));
});
afterEach(async () => mf.dispose());

async function admin(): Promise<Client> {
  const c = new Client(mf, origin);
  await setupAdmin(c);
  return c;
}

async function tickUntilIdle(c: Client, maxMs = 60000): Promise<void> {
  const end = Date.now() + maxMs;
  while (Date.now() < end) {
    const r = await c.post("/api/tick");
    expect(r.status).toBe(200);
    const did = r.data.steps.map((s: any) => s.did);
    if (did.includes("idle") && r.data.active_jobs === 0) return;
    if (did.includes("wait")) await new Promise((res) => setTimeout(res, 400));
  }
  throw new Error("調査が終わりませんでした");
}

describe("初期設定とログイン", () => {
  it("設定用トークンがないと管理者を作れない・既定のパスワードはない", async () => {
    const c = new Client(mf, origin);
    let r = await c.get("/api/state");
    expect(r.data.setup_required).toBe(true);
    expect(r.data.user).toBeNull();
    r = await c.post("/api/setup", { setup_token: "wrong-token-wrong-token", username: "owner" });
    expect(r.status).toBe(403);
    expect((await c.get("/api/dealers")).status).toBe(401);
    const key = await setupAdmin(c);
    expect(key).toMatch(/^([A-Z2-9]{4}-){5}[A-Z2-9]{4}$/);
    const set = (await new Client(mf, origin).post("/api/setup", { setup_token: "test-setup-token-0123456789", username: "x" }));
    expect(set.status).toBe(409); // 2 回目は作れない
  });

  it("ログイン・Cookie 属性・失敗の制限・ログアウト", async () => {
    const owner = await admin();
    const created = await owner.post("/api/users", { username: "friend" });
    expect(created.status).toBe(200);
    const friend = new Client(mf, origin);
    let r = await friend.post("/api/login", { username: "friend", key: "AAAA-BBBB" });
    expect(r.status).toBe(401);
    r = await friend.post("/api/login", { username: "friend", key: created.data.key.toLowerCase() });
    expect(r.status).toBe(200);
    const cookie = r.headers.get("set-cookie")!.toLowerCase();
    expect(cookie).toContain("httponly");
    expect(cookie).toContain("secure");
    expect(cookie).toContain("samesite=lax");
    expect((await friend.get("/api/dealers")).status).toBe(200);
    expect((await friend.post("/api/settings", { max_pages_per_site: 5 })).status).toBe(403); // 管理者のみ
    await friend.post("/api/logout");
    expect((await friend.get("/api/dealers")).status).toBe(401);
    const attacker = new Client(mf, origin);
    for (let i = 0; i < 5; i++) await attacker.post("/api/login", { username: "friend", key: "WRONG" });
    expect((await attacker.post("/api/login", { username: "friend", key: created.data.key })).status).toBe(429);
  });

  it("接続元を変えても、名前ごとの失敗回数でログインを止める", async () => {
    const owner = await admin();
    const created = await owner.post("/api/users", { username: "pin" });
    const c = new Client(mf, origin);
    for (let i = 0; i < 10; i++) {
      const r = await c.post("/api/login", { username: "pin", key: "WRONG" }, { "cf-connecting-ip": `203.0.113.${i}` });
      expect(r.status).toBe(401);
    }
    const r = await c.post("/api/login", { username: "pin", key: created.data.key }, { "cf-connecting-ip": "198.51.100.7" });
    expect(r.status).toBe(429);
    expect((await owner.get("/api/dealers")).status).toBe(200); // ログイン済みの端末は使える
  });

  it("CSRF トークンと Origin を確認する", async () => {
    const c = await admin();
    const csrf = c.csrf;
    c.csrf = "";
    expect((await c.post("/api/research/start", { scope: "unresearched" })).status).toBe(403);
    c.csrf = csrf;
    expect((await c.post("/api/research/start", { scope: "unresearched" }, { origin: "https://evil.example" })).status).toBe(403);
  });
});

describe("初期データ・一覧・CSV", () => {
  it("初期データを 1 社ずつ取り込み、既存データを上書きしない", async () => {
    const c = await admin();
    let done = 0;
    for (let i = 0; i < 40 && done < 30; i++) done = (await c.post("/api/seed/step")).data.done;
    const list = await c.get("/api/dealers");
    expect(list.data.stats.total).toBe(30);
    expect(list.data.stats.with_email_or_form).toBe(28);
    const first = list.data.dealers[0];
    await c.post(`/api/dealers/${first.id}/edit`, { notes: "利用者のメモ" });
    await c.post("/api/seed/restart");
    for (let i = 0; i < 31; i++) await c.post("/api/seed/step");
    const d = await c.get(`/api/dealers/${first.id}`);
    expect(d.data.dealer.notes).toBe("利用者のメモ");
    expect((await c.get("/api/dealers")).data.stats.total).toBe(30);
    const filtered = await c.get("/api/dealers?category=" + encodeURIComponent("クラシックカー"));
    expect(filtered.data.dealers.length).toBe(3);
    const raw = await mf.dispatchFetch(origin + "/api/export/dealers.csv?has_email=1", { headers: { cookie: c.cookie } });
    const bytes = new Uint8Array(await raw.arrayBuffer());
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]); // Excel 用の BOM
    expect(new TextDecoder().decode(bytes)).toContain("販売店ID");
  });
});

describe("URL 追加から調査完了まで", () => {
  it("取得制約を守って調査し、根拠・連絡方法を保存する", async () => {
    const c = await admin();
    const add = await c.post("/api/add/urls", { urls: `${BASE}/\nhttp://127.0.0.1/\nhttps://www.autotrader.co.uk/x\nhttps://internal.example/`, start: true });
    expect(add.data.new_count).toBe(2); // internal.example は登録できるが、取得時に内部アドレスとして拒否される
    expect(add.data.results[1].note).toContain("内部");
    expect(add.data.results[2].note).toContain("掲載サイト");
    await tickUntilIdle(c);
    const list = await c.get("/api/dealers");
    const d = list.data.dealers.find((x: any) => x.host === "test-motors.example");
    expect(d.research_status).toBe("完了");
    expect(d.name).toBe("Test Motors Ltd");
    expect(d.emails_ok).toEqual(expect.arrayContaining(["sales@test-motors.example", "info@test-motors.example"]));
    expect(d.emails_ok).not.toContain("hello@pixelagency.example");
    expect(d.forms_ok).toEqual([BASE + "/contact-us"]);
    const bad = list.data.dealers.find((x: any) => x.host === "internal.example");
    expect(bad.research_status).toBe("取得失敗");
    expect(bad.failure_reason).toContain("内部");
    expect(web.requests).not.toContain(BASE + "/private/secret"); // robots.txt
    expect(web.requests.some((u) => new URL(u).hostname === "internal.example")).toBe(false); // 名前解決の段階で拒否
    // 取得間隔（同一サイトへの連続アクセスが 2 秒以上空いている）は engine の hosts.next_allowed_ms で確認
    const detail = await c.get(`/api/dealers/${d.id}`);
    expect(detail.data.evidence.length).toBeGreaterThan(5);
    expect(detail.data.runs[0].pages.length).toBeGreaterThanOrEqual(4);
  });

  it("手動修正・URL 変更・再調査で ID と人の確定値を保つ", async () => {
    const c = await admin();
    const add = await c.post("/api/add/urls", { urls: BASE + "/", start: true });
    const id = add.data.results[0].id;
    await tickUntilIdle(c);
    const before = (await c.get(`/api/dealers/${id}`)).data.dealer;
    const sales = before.contacts.find((x: any) => x.value === "sales@test-motors.example");
    await c.post(`/api/dealers/${id}/edit`, { name: "確認済みの名称", categories: ["スポーツカー"], summary: "人が書いた概要", website_url: "https://test-motors-new.example/" });
    await c.post(`/api/contacts/${sales.id}/status`, { status: "要手動確認", note: "電話で確認予定" });
    dealerSite(web, "https://test-motors-new.example");
    web.set("https://test-motors-new.example/contact-us", 200, (web.pages.get("https://test-motors-new.example/contact-us")![1]).replaceAll("sales@test-motors.example", "sales2@test-motors.example"));
    await c.post(`/api/dealers/${id}/research`);
    await tickUntilIdle(c);
    const after = (await c.get(`/api/dealers/${id}`)).data;
    expect(after.dealer.research_status, after.dealer.failure_reason).toBe("完了");
    expect(after.dealer.id).toBe(id);
    expect(after.dealer.name).toBe("確認済みの名称");
    expect(after.dealer.name_auto).toBe("Test Motors Ltd");
    expect(after.dealer.categories).toEqual(["スポーツカー"]);
    const old = after.dealer.contacts.find((x: any) => x.value === "sales@test-motors.example");
    expect(old.id).toBe(sales.id);
    expect(old.status).toBe("要手動確認");
    expect(old.is_current).toBe(0);
    expect(after.dealer.contacts.find((x: any) => x.value === "sales2@test-motors.example").is_current).toBe(1);
    expect(after.history.some((h: any) => h.field === "website_url" && h.origin === "手動")).toBe(true);
    expect(after.evidence.every((e: any) => e.dealer_id === id)).toBe(true);
  });

  it("停止と再開", async () => {
    const c = await admin();
    for (let i = 0; i < 3; i++) dealerSite(web, `https://dealer${i}.example`);
    const add = await c.post("/api/add/urls", { urls: [0, 1, 2].map((i) => `https://dealer${i}.example/`).join("\n"), start: true });
    const job = add.data.job_id;
    await c.post("/api/tick"); // 1 社目を開始
    await c.post(`/api/jobs/${job}/stop`);
    await tickUntilIdle(c);
    let j = (await c.get(`/api/jobs/${job}`)).data.job;
    expect(j.status).toBe("停止");
    expect(j.counts["停止"]).toBeGreaterThanOrEqual(2);
    await c.post(`/api/jobs/${job}/resume`);
    await tickUntilIdle(c, 120000);
    j = (await c.get(`/api/jobs/${job}`)).data.job;
    expect(j.status).toBe("完了");
    expect(j.counts["完了"]).toBe(3);
  });
});

describe("候補発見", () => {
  it("OpenStreetMap の応答から候補を作り、登録できる", async () => {
    await mf.dispose();
    const osm = { elements: [
      { type: "node", id: 1, tags: { shop: "car", name: "Alpha Cars", website: "https://alpha-cars.example/", second_hand: "only" } },
      { type: "node", id: 2, tags: { shop: "car", name: "New Only", website: "https://new-only.example/", second_hand: "no" } },
      { type: "node", id: 3, tags: { shop: "car", name: "Listing", website: "https://www.autotrader.co.uk/x" } },
    ] };
    ({ mf, origin } = await startApp(async (req) => (new URL(req.url).hostname.startsWith("overpass") ? Response.json(osm) : web.handle(req))));
    const c = await admin();
    const r = await c.post("/api/discover/run", { provider: "osm", region: "Leeds（リーズ）", category: "一般中古車", limit: 10 });
    expect(r.data.message).toContain("新しい候補 1 件");
    const cands = (await c.get("/api/candidates?status=" + encodeURIComponent("未処理"))).data.candidates;
    expect(cands.map((x: any) => x.url)).toEqual(["https://alpha-cars.example/"]);
    const reg = await c.post("/api/candidates/action", { ids: [cands[0].id], action: "register" });
    expect(reg.data.message).toContain("1 件");
  });

  it("Firecrawl は月間上限を超える前に止める", async () => {
    const c = await admin();
    await c.post("/api/settings", { firecrawl_monthly_credit_cap: 1 });
    await c.post("/api/settings/firecrawl-key", { api_key: "fc-test" });
    const r = await c.post("/api/discover/run", { provider: "firecrawl", region: "Leeds（リーズ）", category: "一般中古車", limit: 10 });
    expect(r.status).toBe(400);
    expect(r.data.error).toContain("上限");
    expect(web.requests.some((u) => u.includes("firecrawl"))).toBe(false);
  });
});

describe("バックアップ・復元・DB の版", () => {
  async function fullBackup(c: Client) {
    const info = (await c.get("/api/backup/info")).data;
    const tables: Record<string, any[]> = {};
    for (const t of Object.keys(info.counts)) {
      tables[t] = [];
      let after = 0;
      for (;;) {
        const r = (await c.get(`/api/backup/rows?table=${t}&after=${after}`)).data;
        tables[t].push(...r.rows);
        if (r.next === null) break;
        after = r.next;
      }
    }
    return { info, tables };
  }

  it("ID・人の確定値・根拠・履歴を保ったまま復元し、API キーは含めない", async () => {
    const c = await admin();
    for (let i = 0; i < 3; i++) await c.post("/api/seed/step");
    await c.post("/api/settings/firecrawl-key", { api_key: "fc-secret-value" });
    const list = (await c.get("/api/dealers")).data.dealers;
    const id = list[0].id;
    await c.post(`/api/dealers/${id}/edit`, { summary: "確定した概要" });
    const before = (await c.get(`/api/dealers/${id}`)).data;
    const backup = await fullBackup(c);
    expect(JSON.stringify(backup)).not.toContain("fc-secret-value");
    expect(backup.tables.users).toBeUndefined();
    // データを変えてから復元
    await c.post(`/api/dealers/${id}/edit`, { summary: "壊れた値" });
    expect((await c.post("/api/restore/begin", { manifest: backup.info })).status).toBe(400); // 現在のデータの保存が先
    expect((await c.post("/api/restore/begin", { manifest: backup.info, current_backup_saved: true })).status).toBe(200);
    for (const [t, rows] of Object.entries(backup.tables)) {
      for (let i = 0; i < rows.length; i += 200) expect((await c.post("/api/restore/rows", { table: t, rows: rows.slice(i, i + 200) })).status).toBe(200);
    }
    expect((await c.post("/api/restore/commit", { counts: backup.info.counts })).status).toBe(200);
    const after = (await c.get(`/api/dealers/${id}`)).data;
    expect(after.dealer.summary).toBe("確定した概要");
    expect(after.dealer.contacts.map((x: any) => x.id)).toEqual(before.dealer.contacts.map((x: any) => x.id));
    expect(after.evidence.length).toBe(before.evidence.length);
    expect(after.history.length).toBeGreaterThanOrEqual(before.history.length);
    expect((await c.get("/api/me")).status).toBe(200); // 利用者・ログインは復元の影響を受けない
  });

  it("行数が合わないバックアップは入れ替えない", async () => {
    const c = await admin();
    await c.post("/api/seed/step");
    const backup = await fullBackup(c);
    await c.post("/api/restore/begin", { manifest: backup.info, current_backup_saved: true });
    const r = await c.post("/api/restore/commit", { counts: { ...backup.info.counts, dealers: 99 } });
    expect(r.status).toBe(400);
    expect((await c.get("/api/dealers")).data.stats.total).toBe(1);
  });

  it("他形式・新しい版のバックアップは受け付けない", async () => {
    const c = await admin();
    expect((await c.post("/api/restore/begin", { manifest: { format: "other" }, current_backup_saved: true })).status).toBe(400);
    expect((await c.post("/api/restore/begin", { manifest: { format: "sdr-mobile-backup-1", app: "SamuraiDealerResearch", schema_version: 99 }, current_backup_saved: true })).status).toBe(400);
  });
});
