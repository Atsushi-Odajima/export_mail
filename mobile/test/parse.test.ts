// workerd 上の HTMLRewriter による読み取り・分析の試験（PC 版 tests/fixtures の架空サイトを使用）。
import { Miniflare } from "miniflare";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fixture, startUnitWorker } from "./harness";

let mf: Miniflare;
const BASE = "https://www.test-motors.example";

beforeAll(async () => {
  mf = await startUnitWorker();
});
afterAll(async () => mf.dispose());

const post = async (path: string, body: unknown) => (await mf.dispatchFetch("http://unit" + path, { method: "POST", body: JSON.stringify(body) })).json() as Promise<any>;

describe("HTML の読み取り", () => {
  it("mailto・本文・保護表示のメール、問い合わせフォーム、郵便番号", async () => {
    const f = await post("/parse", { html: fixture("contact.html"), url: BASE + "/contact-us", role: "contact" });
    const emails = Object.fromEntries(f.emails.map((e: any) => [e.email, e.method]));
    expect(emails["sales@test-motors.example"]).toBe("mailto");
    expect(emails["info@test-motors.example"]).toBe("保護表示の復号");
    expect(emails["jobs@test-motors.example"]).toBe("本文");
    expect(f.forms.map((x: any) => x.kind)).toEqual(["問い合わせ", "ログイン"]);
    expect(f.postcodes[0].pc).toBe("LS1 4AP");
  });
  it("トップの検索欄・ニュースレターを問い合わせフォームにしない・スクリプトを本文に含めない・構造化データ", async () => {
    const f = await post("/parse", { html: fixture("home.html"), url: BASE + "/", role: "home" });
    expect(f.forms.map((x: any) => x.kind).sort()).toEqual(["ニュースレター", "検索"]);
    expect(f.text).not.toContain("alert(");
    expect(f.jsonld[0].name).toBe("Test Motors Ltd");
    expect(f.emails.map((e: any) => e.email)).not.toContain("logo@2x.png");
    expect(f.links.some((l: any) => l.h === BASE + "/contact-us" && l.a.includes("Contact"))).toBe(true);
  });
  it("外部フォームの埋め込みを検出する", async () => {
    const f = await post("/parse", { html: '<h1>Contact</h1><script src="https://js.hsforms.net/forms/v2.js"></script>', url: BASE + "/contact", role: "contact" });
    expect(f.embeds).toContain("HubSpot フォーム");
    expect(f.forms).toEqual([]);
  });
});

describe("1 社分の分析（PC 版と同じ結果）", () => {
  it("架空の販売店サイト", async () => {
    const r = await post("/analyse", {
      site: BASE + "/",
      pages: [
        { url: BASE + "/", role: "home", html: fixture("home.html") },
        { url: BASE + "/contact-us", role: "contact", html: fixture("contact.html") },
        { url: BASE + "/about-us", role: "about", html: fixture("about.html") },
        { url: BASE + "/used-cars", role: "stock", html: fixture("stock.html") },
      ],
    });
    expect(r.status).toBe("完了");
    expect(r.name).toBe("Test Motors Ltd");
    expect(r.postcode).toBe("LS1 4AP");
    expect(r.region).toContain("ヨークシャー");
    expect(r.ukCheck).toBe("英国内の住所を確認");
    expect(r.usedSales).toBe("中古車販売を確認");
    expect(r.categories).toEqual(["一般中古車"]);
    const emails = Object.fromEntries(r.contacts.filter((c: any) => c.kind === "email").map((c: any) => [c.value, c.status]));
    expect(emails["sales@test-motors.example"]).toBe("公開メール確認済み");
    expect(emails["info@test-motors.example"]).toBe("公開メール確認済み");
    expect(emails["hello@pixelagency.example"]).toBe("除外");
    expect(emails["jobs@test-motors.example"]).toBe("除外");
    expect(emails["privacy@test-motors.example"]).toBe("除外");
    const forms = r.contacts.filter((c: any) => c.kind === "form");
    expect(forms.length).toBe(1);
    expect(forms[0].value).toBe(BASE + "/contact-us");
    expect(r.evidence.some((e: any) => e[2].includes("1998年創業"))).toBe(true);
    expect(r.summary.startsWith("ヨークシャー（郵便番号 LS1 4AP）の中古車販売店です。")).toBe(true);
  });
});

describe("データベースの版", () => {
  it("試験用の版 2 で既存データを保ち、失敗した版 3 は巻き戻す", async () => {
    const r: any = await (await mf.dispatchFetch("http://unit/migrate", { method: "POST" })).json();
    expect(r.afterV2).toBe(2);
    expect(r.row).toEqual({ name_manual: "Keep Me", test_extra: null });
    expect(r.v3).toContain("更新前の状態のまま");
    expect(r.afterV3).toBe(2);
    expect(r.cols).not.toContain("half_done");
    expect(r.keep).toEqual({ name_manual: "Keep Me" });
  });
});
