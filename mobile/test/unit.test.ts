// 規則（分類・メール判定・安全確認・robots.txt・CSV）の試験。架空データのみ。
import { describe, expect, it } from "vitest";
import { addressFromContext, chooseLinks, foreignJsonLdAddress } from "../src/analyse";
import * as C from "../src/classify";
import { dealersCsv, parseImport, safeCell, toCsv } from "../src/csv";
import { classifyForm, decodeCfEmail, isAggregator, validEmail } from "../src/extract";
import { assertSafeUrl, checkUrlSyntax, isPublicIp, UnsafeUrlError } from "../src/netsafe";
import { robotsAllows } from "../src/robots";
import { generateLoginKey, normalizeKey } from "../src/auth";

const page = (role: string, text: string, headline = ""): C.PageText => ({ url: `https://x.example/${role}`, role, headline, text });

describe("メールの判定", () => {
  const host = "www.test-motors.example";
  it("店舗のメールと制作会社・求人・個人情報窓口を区別する", () => {
    expect(C.judgeEmail("sales@test-motors.example", host).status).toBe(C.EMAIL_OK);
    const agency = C.judgeEmail("hello@pixelagency.example", host, "Website by Pixel Agency hello@pixelagency.example");
    expect(agency.status).toBe(C.EMAIL_EXCLUDED);
    expect(agency.reason).toContain("制作会社");
    expect(C.judgeEmail("jobs@test-motors.example", host).status).toBe(C.EMAIL_EXCLUDED);
    expect(C.judgeEmail("dpo@test-motors.example", host).reason).toBe("個人情報・プライバシー窓口");
    expect(C.judgeEmail("office@other-domain.example", host).status).toBe(C.EMAIL_REVIEW);
    expect(C.judgeEmail("testmotors@gmail.com", host).status).toBe(C.EMAIL_OK);
    expect(C.judgeEmail("noreply@test-motors.example", host).status).toBe(C.EMAIL_EXCLUDED);
  });
  it("画像名・例示用アドレスをメールと誤認しない", () => {
    expect(validEmail("logo@2x.png")).toBe(false);
    expect(validEmail("name@example.com")).toBe(false);
    expect(validEmail("canvas-confetti@1.9.3")).toBe(false);
    expect(validEmail("sales@dealer.co.uk")).toBe(true);
    expect(decodeCfEmail("2a43444c456a5e4f595e0747455e455859044f524b475a464f")).toBe("info@test-motors.example");
  });
});

describe("フォームの判定", () => {
  const f = (inputs: [string, string][], extra: Partial<{ attrs: string; role: string; text: string }> = {}) =>
    classifyForm({ attrs: extra.attrs || "", role: extra.role || "", text: extra.text || "", inputs: inputs.map(([type, names]) => ({ type, names })) });
  it("問い合わせフォームと検索・ログイン・ニュースレター・在庫検索を区別する", () => {
    expect(f([["text", "name"], ["email", "email"], ["textarea", "message"]]).kind).toBe("問い合わせ");
    expect(f([["text", "q"]], { role: "search" }).kind).toBe("検索");
    expect(f([["text", "s"]], { attrs: "/search" }).kind).toBe("検索");
    expect(f([["text", "user"], ["password", "pw"]]).kind).toBe("ログイン");
    expect(f([["email", "email"]], { text: "Subscribe to our newsletter" }).kind).toBe("ニュースレター");
    expect(f([["select", "make"], ["select", "model"], ["select", "maxprice"]]).kind).toBe("在庫検索");
  });
});

describe("分類", () => {
  it("公式の専門店表記", () => {
    const cls = C.classifyDealer([page("home", "We are classic car specialists based in Kent with classic cars for sale.", "Example Classics | Classic Car Specialists")]);
    expect(cls.categories).toEqual([C.CAT_CLASSIC]);
    expect(cls.basis).toBe(C.BASIS_OFFICIAL);
  });
  it("SUV が数台あるだけで SUV 専門にしない", () => {
    const stock = ["2018 Ford Fiesta £8,995", "2017 Vauxhall Corsa £6,495", "2019 Nissan Qashqai £13,995", "2016 VW Golf £9,250", "2015 Toyota Yaris £7,995", "2020 Kia Picanto £8,250"].join(" ");
    const cls = C.classifyDealer([page("home", "Used car dealer in Leeds", "Used car dealer"), page("stock", stock)]);
    expect(cls.categories).toEqual([C.CAT_GENERAL]);
    expect(cls.basis).toBe(C.BASIS_TREND);
    expect(cls.scope).toContain("全在庫は未確認");
  });
  it("在庫の傾向（専門の明記なし）", () => {
    const models = ["Range Rover Sport £24,995", "Land Rover Discovery 4 £19,995", "Volvo XC90 £21,500", "Kia Sorento £15,995", "Toyota RAV4 £17,250", "Range Rover Evoque £16,995", "Land Rover Defender 110 £39,995"];
    const cls = C.classifyDealer([page("home", "Welcome to our showroom"), page("stock", models.join(" "))]);
    expect(cls.categories).toEqual([C.CAT_SUV]);
    expect(cls.basis).toBe(C.BASIS_TREND);
  });
  it("判断できなければ要確認", () => {
    const cls = C.classifyDealer([page("home", "Welcome to our website. Opening hours Monday to Friday.")]);
    expect(cls.categories).toEqual([C.CAT_REVIEW]);
    expect(C.buildSummary(null, null, "未確認", cls, [])).toContain("要確認");
  });
  it("メーカー専門店・車とバンの販売・文をまたぐ一致", () => {
    expect(C.classifyDealer([page("home", "Independent Low Mileage Porsche Specialists. Cars for sale.")]).categories).toEqual([C.CAT_SPORTS]);
    expect(C.classifyDealer([page("home", "Used cars for sale in Edinburgh", "Used car and van Dealer in Midlothian")]).categories).toEqual([C.CAT_GENERAL]);
    expect(C.classifyDealer([page("about", "transport of classic cars, motorcycles and modern high performance machines. People At The Hub Sales team")]).categories).not.toContain(C.CAT_SPORTS);
    expect(C.classifyDealer([page("about", "Family run, independent, new and used 4×4 vehicle specialists.")]).categories).toContain(C.CAT_SUV);
  });
  it("概要は確認した事実だけで 3 文以内", () => {
    const s = C.buildSummary("ヨークシャー", "LS1 4AP", "中古車販売を確認", { categories: [C.CAT_GENERAL], basis: C.BASIS_TREND, scope: "", evidence: [], otherLabel: "" }, ["下取り（Part Exchange）"]);
    expect(s.startsWith("ヨークシャー（郵便番号 LS1 4AP）の中古車販売店です。")).toBe(true);
    expect((s.match(/。/g) || []).length).toBeLessThanOrEqual(3);
  });
  it("アイルランド・英国外を英国扱いしない", () => {
    expect(C.detectUk([], ["+353 1 000 0000"], "Co. Dublin, Republic of Ireland", "cars.example.ie")[0]).toContain("英国外");
    expect(C.detectUk(["BT1 1AA"], [], "Belfast, Northern Ireland", "ni.example")[0]).toBe("英国内の住所を確認");
    expect(foreignJsonLdAddress([{ address: { postalCode: "151-0063", addressLocality: "Tokyo" } }])).toBeTruthy();
    expect(foreignJsonLdAddress([{ address: { postalCode: "CM9", addressCountry: "GB" } }])).toBeNull();
  });
});

describe("住所・リンク選択", () => {
  it("郵便番号の前から住所部分だけを取り出す", () => {
    expect(addressFromContext("mail.com Contact us on: 07933 447710 Find us at: Woodhall Cars Calverley Site, A6120 Ring Rd, Calverley, Leeds, LS13 1LY"))
      .toBe("Woodhall Cars Calverley Site, A6120 Ring Rd, Calverley, Leeds, LS13 1LY");
  });
  it("同じサイトの問い合わせ・会社紹介・在庫ページを選び、プライバシー等は除く", () => {
    const facts: any = { links: [
      { h: "https://www.a.example/contact-us", a: "Contact" }, { h: "https://www.a.example/about", a: "About" },
      { h: "https://www.a.example/used-cars", a: "Stock" }, { h: "https://www.a.example/privacy-policy", a: "Privacy" },
      { h: "https://other.example/contact", a: "Contact" }, { h: "https://www.a.example/used-cars/ford-fiesta-12345", a: "Fiesta" },
    ] };
    const picked = chooseLinks(facts, "https://www.a.example/", 9).map(([h]) => h);
    expect(picked[0]).toBe("https://www.a.example/contact-us");
    expect(picked).not.toContain("https://www.a.example/privacy-policy");
    expect(picked).not.toContain("https://other.example/contact");
  });
});

describe("安全確認", () => {
  const bad = ["http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/", "http://example.com:22/", "http://router.local/",
    "http://192.168.1.1/", "http://metadata.google.internal/", "http://100.64.0.1/", "http://intranet/", "http://[::ffff:127.0.0.1]/", "http://2130706433/"];
  for (const u of bad) {
    it(`拒否: ${u}`, () => {
      expect(() => checkUrlSyntax(u)).toThrow(UnsafeUrlError);
    });
  }
  it("公開URLは許可・名前解決後の内部アドレスは拒否", async () => {
    expect(checkUrlSyntax("https://www.test-motors.example/contact")).toBe("www.test-motors.example");
    await expect(assertSafeUrl("https://rebind.example/", async () => ["10.1.2.3"])).rejects.toThrow(UnsafeUrlError);
    await expect(assertSafeUrl("https://ok.example/", async () => ["93.184.216.34"])).resolves.toBeUndefined();
    expect(isPublicIp("8.8.8.8")).toBe(true);
    expect(isPublicIp("fd00::1")).toBe(false);
  });
  it("掲載サイト・SNS を販売店として扱わない", () => {
    expect(isAggregator("https://www.autotrader.co.uk/dealers/x")).toBe(true);
    expect(isAggregator("https://www.dealer.example/")).toBe(false);
  });
});

describe("robots.txt", () => {
  it("不許可・許可・ワイルドカード・自分向けグループ", () => {
    const txt = "User-agent: *\nDisallow: /private/\nAllow: /private/ok\nDisallow: /*.pdf$\n\nUser-agent: OtherBot\nDisallow: /";
    expect(robotsAllows(txt, "https://a.example/private/x")).toBe(false);
    expect(robotsAllows(txt, "https://a.example/private/ok")).toBe(true);
    expect(robotsAllows(txt, "https://a.example/file.pdf")).toBe(false);
    expect(robotsAllows(txt, "https://a.example/")).toBe(true);
    expect(robotsAllows("User-agent: SamuraiDealerResearch\nDisallow: /\n\nUser-agent: *\nDisallow:", "https://a.example/")).toBe(false);
  });
});

describe("CSV", () => {
  it("BOM・CRLF・数式の無効化", () => {
    const csv = toCsv(["a"], [["=HYPERLINK(\"x\")"], ["+cmd"], ["普通"]]);
    expect(csv.startsWith("﻿")).toBe(true);
    expect(csv).toContain("\"'=HYPERLINK");
    expect(csv).toContain("\"'+cmd\"");
    expect(csv).toContain("\r\n");
    expect(safeCell("@x")).toBe("'@x");
  });
  it("取込（見出しあり・なし）", () => {
    expect(parseImport("URL,店名,地域\r\nhttps://a.example/,A Cars,Leeds\r\n,空,\r\n").rows).toEqual([{ line: 2, url: "https://a.example/", name: "A Cars", region: "Leeds", note: "" }]);
    expect(parseImport("https://c.example/\nhttps://d.example/\n").rows.map((r) => r.url)).toEqual(["https://c.example/", "https://d.example/"]);
    expect(parseImport('公式サイト,メモ\n"https://e.example/","a, ""b"""\n').rows[0].note).toBe('a, "b"');
  });
  it("一覧 CSV の見出し", () => {
    expect(dealersCsv([])).toContain("販売店ID");
  });
});

describe("ログイン用キー", () => {
  it("十分に長い乱数で、表記ゆれを吸収する", () => {
    const k = generateLoginKey();
    expect(k).toMatch(/^([A-Z2-9]{4}-){5}[A-Z2-9]{4}$/);
    expect(normalizeKey(k.toLowerCase().replace(/-/g, " "))).toBe(k.replace(/-/g, ""));
    expect(generateLoginKey()).not.toBe(k);
  });
});
