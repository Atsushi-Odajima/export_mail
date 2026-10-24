// 試験専用の補助 Worker（本番には含めない）。workerd 上の HTMLRewriter で読み取り・分析を直接試す。
import { analyse, PageRecord } from "../src/analyse";
import { parseHtml } from "../src/parse";
import { applyMigrations, currentVersion } from "../src/db";
import { MIGRATIONS } from "../src/schema";

export default {
  async fetch(req: Request, env: { DB: D1Database }): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/migrate") {
      // 版 1 を作ってデータを入れ、試験用の版 2（列の追加）と失敗する版 3 を適用する
      const db = env.DB;
      const out: Record<string, unknown> = {};
      await applyMigrations(db, 0);
      await db.prepare("INSERT INTO dealers(id, created_at, updated_at, website_url, website_host, name_manual, data_origin) VALUES('DLR-TEST','t','t','https://a.example/','a.example','Keep Me','テスト')").run();
      const v2 = [...MIGRATIONS, { version: 2, name: "試験用の列追加", statements: ["ALTER TABLE dealers ADD COLUMN test_extra TEXT"] }];
      out.v2log = await applyMigrations(db, 1, v2);
      out.afterV2 = await currentVersion(db);
      out.row = await db.prepare("SELECT name_manual, test_extra FROM dealers WHERE id='DLR-TEST'").first();
      const v3 = [...v2, { version: 3, name: "失敗する移行", statements: ["ALTER TABLE dealers ADD COLUMN half_done TEXT", "SELECT * FROM no_such_table"] }];
      try {
        await applyMigrations(db, 2, v3);
        out.v3 = "applied";
      } catch (e) {
        out.v3 = (e as Error).message;
      }
      out.afterV3 = await currentVersion(db);
      out.cols = (await db.prepare("PRAGMA table_info(dealers)").all<{ name: string }>()).results.map((r) => r.name);
      out.keep = await db.prepare("SELECT name_manual FROM dealers WHERE id='DLR-TEST'").first();
      return Response.json(out);
    }
    const body: any = await req.json();
    if (url.pathname === "/parse") return Response.json(await parseHtml(body.html, body.url, body.role || "home"));
    if (url.pathname === "/analyse") {
      const infos = [];
      for (const p of body.pages) infos.push(await parseHtml(p.html, p.url, p.role));
      const records: PageRecord[] = body.pages.map((p: any) => ({ url: p.url, role: p.role, status: "取得", error: "" }));
      return Response.json(analyse(infos, records, body.site, false));
    }
    return new Response("not found", { status: 404 });
  },
};
