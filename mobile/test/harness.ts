// 試験用の土台：本番と同じバンドル（dist/）を Miniflare（workerd）で動かし、D1 も実物（ローカル）を使う。
// 外部への通信（販売店サイト・DNS over HTTPS・Overpass 等）は FakeWeb に差し替える。
// ここで使う販売店・サイト・メールアドレスはすべて試験用の架空データ（.example は予約済みドメイン）。
import { Miniflare } from "miniflare";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export const ROOT = resolve(__dirname, "..");

export type Page = [number, string, Record<string, string>?];

export class FakeWeb {
  pages = new Map<string, Page>();
  requests: string[] = [];
  set(url: string, status: number, body: string, headers: Record<string, string> = {}) {
    this.pages.set(url, [status, body, headers]);
  }
  async handle(req: Request): Promise<Response> {
    const url = req.url;
    this.requests.push(url);
    const u = new URL(url);
    if (u.hostname === "cloudflare-dns.com") {
      const name = u.searchParams.get("name") || "";
      const type = u.searchParams.get("type");
      const ip = name.startsWith("internal.") ? "10.0.0.5" : "93.184.216.34";
      return Response.json({ Answer: type === "A" ? [{ type: 1, data: ip }] : [] });
    }
    const hit = this.pages.get(url) || this.pages.get(url.replace(/\/$/, "")) || this.pages.get(url + "/");
    if (!hit) return new Response("not found", { status: 404, headers: { "content-type": "text/html" } });
    const [status, body, headers] = hit;
    return new Response(status >= 300 && status < 400 ? null : body, { status, headers: { "content-type": "text/html; charset=utf-8", ...(headers || {}) } });
  }
}

export function fixture(name: string): string {
  return readFileSync(resolve(ROOT, "../tests/fixtures", name), "utf8");
}

export function dealerSite(web: FakeWeb, base = "https://www.test-motors.example") {
  web.set(`${base}/robots.txt`, 200, "User-agent: *\nDisallow: /private/\n", { "content-type": "text/plain" });
  web.set(`${base}/`, 200, fixture("home.html"));
  web.set(`${base}/contact-us`, 200, fixture("contact.html"));
  web.set(`${base}/about-us`, 200, fixture("about.html"));
  web.set(`${base}/used-cars`, 200, fixture("stock.html"));
  web.set(`${base}/private/secret`, 200, "<html>must not be fetched</html>");
}

// 外部への通信を Node 側の関数に渡すための補助 Worker
const OUTBOUND = "export default { fetch(req, env) { return env.NODE.fetch(req); } };";

export async function startApp(web: FakeWeb | ((req: Request) => Promise<Response>), setupToken = "test-setup-token-0123456789", persist?: string) {
  const handler = typeof web === "function" ? web : (req: Request) => web.handle(req);
  const mf = new Miniflare({
    workers: [
      {
        name: "app",
        scriptPath: resolve(ROOT, "dist/index.js"),
        modules: true,
        modulesRules: [{ type: "Text", include: ["**/*.seed"] }],
        compatibilityDate: "2026-08-01",
        compatibilityFlags: ["nodejs_compat"],
        d1Databases: { DB: "test-db" },
        bindings: { SETUP_TOKEN: setupToken, APP_ENV: "test" },
        serviceBindings: { ASSETS: async () => new Response("asset") },
        outboundService: "outbound",
      },
      { name: "outbound", modules: true, script: OUTBOUND, compatibilityDate: "2026-08-01", serviceBindings: { NODE: handler as any } },
    ],
    ...(persist ? { d1Persist: persist } : {}),
  });
  const url = await mf.ready;
  return { mf, origin: url.origin };
}

export async function startUnitWorker() {
  const mf = new Miniflare({ workers: [{ name: "unit", scriptPath: resolve(ROOT, "dist-test/unit-worker.js"), modules: true, compatibilityDate: "2026-08-01", d1Databases: { DB: "unit-db" } }] });
  await mf.ready;
  return mf;
}

/** Cookie と CSRF を扱う簡易クライアント */
export class Client {
  cookie = "";
  csrf = "";
  constructor(private mf: Miniflare, private origin: string) {}
  async call(method: string, path: string, body?: unknown, extra: Record<string, string> = {}) {
    const headers: Record<string, string> = { ...extra };
    if (this.cookie) headers.cookie = this.cookie;
    if (method === "POST") {
      headers["content-type"] = "application/json";
      headers.origin ??= this.origin;
      if (this.csrf) headers["x-csrf-token"] ??= this.csrf;
    }
    const res = await this.mf.dispatchFetch(this.origin + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const set = res.headers.get("set-cookie");
    if (set) this.cookie = set.split(";")[0];
    const type = res.headers.get("content-type") || "";
    const data: any = type.includes("json") ? await res.json() : await res.text();
    if (data && typeof data === "object" && data.user?.csrf) this.csrf = data.user.csrf;
    return { status: res.status, data, headers: res.headers };
  }
  get = (p: string) => this.call("GET", p);
  post = (p: string, b: unknown = {}, h: Record<string, string> = {}) => this.call("POST", p, b, h);
}

export async function setupAdmin(c: Client, token = "test-setup-token-0123456789") {
  const r = await c.post("/api/setup", { setup_token: token, username: "owner" });
  if (r.status !== 200) throw new Error("setup failed: " + JSON.stringify(r.data));
  return r.data.key as string;
}
