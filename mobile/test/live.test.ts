// 実サイトでの確認（手動実行のみ）：LIVE=1 npm test -- live
// 本番と同じバンドルを workerd で動かし、外部通信だけ Node の fetch で実際のサイトへ送る。
// 結果は PC 版で確認済みの初期データ（../sdr/seed/initial_dealers.json）の公開メールと照合する。
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { Miniflare } from "miniflare";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Client, ROOT, setupAdmin, startApp } from "./harness";

const LIVE = process.env.LIVE === "1";
const SITES = (process.env.LIVE_SITES || "https://www.woodhallcars.co.uk/,https://www.ascarsleedsltd.co.uk/,https://burnsidecarstore.co.uk/,https://www.gmpcars.co.uk/,https://www.ng9motors.co.uk/,https://www.heritageclassic.com/,https://www.911virgin.com/,https://mikehardinglandrovers.co.uk/")
  .split(",").map((s) => s.trim()).filter(Boolean);

let mf: Miniflare;
let origin: string;

async function nodeFetch(req: Request): Promise<Response> {
  const res = await fetch(req.url, { method: req.method, headers: req.headers, redirect: "manual" });
  // workerd へ返すときに圧縮済みの長さが合わなくなるのを避けるため、本文を読み直して返す
  const body = res.status >= 300 && res.status < 400 ? null : await res.arrayBuffer();
  const headers = new Headers(res.headers);
  headers.delete("content-encoding");
  headers.delete("content-length");
  return new Response(body, { status: res.status, headers });
}

describe.skipIf(!LIVE)("実サイトでの調査（Workers 版）", () => {
  beforeAll(async () => {
    ({ mf, origin } = await startApp(nodeFetch));
  });
  afterAll(async () => mf?.dispose());

  it("PC 版の確認済みデータと同じ公開メールを見つける", async () => {
    const c = new Client(mf, origin);
    await setupAdmin(c);
    const add = await c.post("/api/add/urls", { urls: SITES.join("\n"), start: true });
    expect(add.data.new_count).toBe(SITES.length);
    const end = Date.now() + 15 * 60_000;
    for (;;) {
      const r = await c.post("/api/tick");
      if (r.data.active_jobs === 0) break;
      if (Date.now() > end) throw new Error("時間内に終わりませんでした");
      if (r.data.steps.every((s: any) => s.did === "wait" || s.did === "idle")) await new Promise((res) => setTimeout(res, 500));
    }
    const seed = JSON.parse(readFileSync(resolve(ROOT, "../sdr/seed/initial_dealers.json"), "utf8"));
    const list = (await c.get("/api/dealers")).data.dealers;
    const rows: any[] = [];
    for (const d of list) {
      const s = seed.dealers.find((x: any) => x.website_host === d.host);
      const expected = seed.contact_methods
        .filter((m: any) => m.dealer_id === s?.id && m.kind === "email" && (m.status_manual || m.status_auto) === "公開メール確認済み")
        .map((m: any) => m.value).sort();
      rows.push({ host: d.host, status: d.research_status, reason: d.failure_reason, categories: d.categories, region: d.region, found: [...d.emails_ok].sort(), expected, forms: d.forms_ok.length, summary: d.summary });
    }
    writeFileSync(resolve(ROOT, "test-output/live-result.json"), JSON.stringify(rows, null, 2));
    console.table(rows.map((r) => ({ host: r.host, status: r.status, found: r.found.join(" "), expected: r.expected.join(" "), forms: r.forms })));
    // 取得できたサイトでは、PC 版で確認済みのメールを Workers 版でも見つけること
    for (const r of rows.filter((x) => x.status === "完了")) for (const e of r.expected) expect(r.found).toContain(e);
  }, 20 * 60_000);
});
