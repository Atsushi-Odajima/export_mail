// 1 社分のページ情報（PageFacts）から、名称・所在地・分類・概要・連絡方法・根拠を組み立てる。
// PC 版 sdr/research.py の analyse / choose_links と同じ規則。純粋な関数（DB・通信なし）。

import * as C from "./classify";
import { PageFacts, postcodeRegion, UK_POSTCODE_FULL } from "./extract";
import { hostKey } from "./netsafe";
import { clean, excerptAround } from "./util";

const ROLE_PATTERNS: [string, RegExp, number][] = [
  ["contact", /contact|get-in-touch|getintouch|enquir|inquir|find-us|findus|visit-us|location|directions/i, 100],
  ["about", /about|who-we-are|our-story|history|meet-the-team|our-team|why-buy|why-choose/i, 80],
  ["stock", /used-cars|usedcars|used-vehicles|our-cars|our-stock|stock|showroom|inventory|vehicles|cars-for-sale|for-sale|search-cars|current-stock|classic-cars|sports-cars|4x4|our-vehicles|cars$|\/cars\/?/i, 60],
];
const ANCHOR_HINTS: [string, RegExp][] = [
  ["contact", /\b(?:contact|get in touch|enquire|enquiries|find us|visit us)\b/i],
  ["about", /\b(?:about|who we are|our story|meet the team|why buy)\b/i],
  ["stock", /\b(?:used cars|our stock|stock|showroom|our cars|vehicles for sale|cars for sale|view all|browse|current stock|inventory)\b/i],
];
const SKIP_LINK = /privacy|cookie|terms|legal|careers|jobs|vacanc|blog|news|login|account|basket|cart|wp-admin|wp-login|feed|\.pdf$|\.jpe?g$|\.png$|facebook|twitter|instagram|youtube|tel:|mailto:|sitemap|finance-calculator|valuation|sell-your-car|part-exchange-valuation/i;
const MAX_PER_ROLE: Record<string, number> = { contact: 3, about: 2, stock: 3, other: 0 };

export function chooseLinks(facts: PageFacts, siteUrl: string, limit: number): [string, string][] {
  const site = hostKey(siteUrl);
  const scored = new Map<string, [number, string]>();
  for (const { h, a } of facts.links) {
    if (!/^https?:\/\//.test(h) || hostKey(h) !== site) continue;
    const path = new URL(h).pathname.toLowerCase();
    if (SKIP_LINK.test(h) || path === "" || path === "/") continue;
    let role = "other";
    let score = 0;
    for (const [r, pat, s] of ROLE_PATTERNS) {
      if (pat.test(path)) {
        role = r;
        score = s;
        break;
      }
    }
    if (role === "other") {
      for (const [r, pat] of ANCHOR_HINTS) {
        if (pat.test(a)) {
          role = r;
          score = ({ contact: 95, about: 75, stock: 55 } as Record<string, number>)[r];
          break;
        }
      }
    }
    if (role === "other") continue;
    score -= (path.match(/\//g) || []).length * 3 + (/\d{4,}/.test(path) ? 10 : 0);
    const key = h.replace(/\/+$/, "");
    const prev = scored.get(key);
    if (!prev || prev[0] < score) scored.set(key, [score, role]);
  }
  const ordered = [...scored.entries()].sort((x, y) => y[1][0] - x[1][0]);
  const picked: [string, string][] = [];
  const perRole: Record<string, number> = {};
  for (const [href, [, role]] of ordered) {
    if ((perRole[role] || 0) >= (MAX_PER_ROLE[role] || 0)) continue;
    perRole[role] = (perRole[role] || 0) + 1;
    picked.push([href, role]);
    if (picked.length >= limit) break;
  }
  return picked;
}

const UK_COUNTRY = new Set(["gb", "uk", "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland"]);

function firstAddress(obj: Record<string, unknown>): Record<string, unknown> | null {
  let addr = obj.address as unknown;
  if (Array.isArray(addr)) addr = addr[0];
  return addr && typeof addr === "object" ? (addr as Record<string, unknown>) : null;
}

export function foreignJsonLdAddress(jsonld: Record<string, unknown>[]): string | null {
  for (const obj of jsonld) {
    const addr = firstAddress(obj);
    if (!addr) continue;
    let country = addr.addressCountry as unknown;
    if (country && typeof country === "object") country = (country as Record<string, unknown>).name || (country as Record<string, unknown>)["@id"];
    const c = clean(String(country || "")).toLowerCase();
    const pc = clean(String(addr.postalCode || "")).toUpperCase();
    const text = clean(["streetAddress", "addressLocality", "postalCode", "addressCountry"].filter((k) => addr[k]).map((k) => String(addr[k])).join(", "));
    if (c && !UK_COUNTRY.has(c)) return text;
    const ukLike = UK_POSTCODE_FULL.test(pc) || /^[A-Z]{1,2}\d[A-Z\d]?$/.test(pc);
    if (pc && !ukLike) return text;
  }
  return null;
}

const ADDRESS_MARKERS = /(?:address|find us at|located at|visit us at|where to find us|our location|showroom)\s*:?\s*/gi;

export function addressFromContext(ctx: string): string {
  let text = clean(ctx);
  const marks = [...text.matchAll(ADDRESS_MARKERS)];
  if (marks.length) {
    const last = marks[marks.length - 1];
    text = text.slice((last.index ?? 0) + last[0].length);
  } else {
    const parts = text.split(/[.!?:|]\s/);
    text = parts[parts.length - 1];
  }
  text = text.replace(/\S+@\S+/g, " ");
  text = text.replace(/(?:\+44\s?\(?0?\)?\s?|\b0)(?:\d[\s-]?){9,10}\b/g, " ");
  text = text.replace(/\b(?:WhatsApp|Tel|Phone|Call us|Contact us|Contact|Email|E-mail|Mobile)\b:?/gi, " ");
  return clean(text).slice(-160).replace(/^[ ,-]+|[ ,-]+$/g, "");
}

export interface PageRecord {
  url: string;
  role: string;
  status: string; // 取得 / 失敗 / robots.txtで対象外 / 停止
  error: string;
}

export interface ContactFinding {
  kind: "email" | "form" | "contact_page" | "phone";
  value: string;
  purpose: string;
  status: string;
  sourceUrl: string;
  excerpt: string;
  reason: string;
}

export interface ResearchResult {
  ok: boolean;
  status: string; // 完了 / 一部取得失敗 / 取得失敗 / 停止
  failureReason: string;
  pages: PageRecord[];
  name: string | null;
  region: string | null;
  address: string | null;
  postcode: string | null;
  categories: string[];
  categoryBasis: string | null;
  categoryScope: string | null;
  summary: string | null;
  usedSales: string | null;
  ukCheck: string | null;
  contacts: ContactFinding[];
  evidence: C.Ev[];
}

export function emptyResult(): ResearchResult {
  return {
    ok: false, status: "取得失敗", failureReason: "", pages: [], name: null, region: null, address: null, postcode: null,
    categories: [], categoryBasis: null, categoryScope: null, summary: null, usedSales: null, ukCheck: null, contacts: [], evidence: [],
  };
}

/** infos[0] はトップページ。pages は取得の記録。 */
export function analyse(infos: PageFacts[], pages: PageRecord[], siteUrl: string, stopped: boolean): ResearchResult {
  const result = emptyResult();
  result.pages = pages;
  const home = infos.find((i) => i.role === "home");
  if (!home) {
    const first = pages[0];
    result.status = stopped ? "停止" : "取得失敗";
    result.failureReason = stopped ? "利用者の操作で停止しました" : `トップページを取得できませんでした: ${first?.error || "不明"}`;
    return result;
  }
  const ordered = [home, ...infos.filter((i) => i !== home)];
  const siteHost = hostKey(siteUrl);
  const ptexts: C.PageText[] = ordered.map((i) => ({
    url: i.url, role: i.role, headline: [i.title, i.siteName, i.metaDescription, i.h1.join(" ")].join(" "), text: i.text,
  }));

  const jsonld = ordered.flatMap((i) => i.jsonld);
  result.name = C.pickName(jsonld, home.siteName, home.title, siteHost);
  result.evidence.push(["名称", home.url, (home.siteName || home.title).slice(0, 240)]);

  let address: string | null = null;
  let postcode: string | null = null;
  let addrSrc: [string, string] | null = null;
  for (const i of ordered) {
    for (const obj of i.jsonld) {
      const addr = firstAddress(obj);
      if (addr && addr.postalCode) {
        address = clean(["streetAddress", "addressLocality", "addressRegion", "postalCode"].filter((k) => addr[k]).map((k) => String(addr[k])).join(", "));
        postcode = clean(String(addr.postalCode)).toUpperCase();
        addrSrc = [i.url, `構造化データ: ${address}`];
        break;
      }
    }
    if (address) break;
  }
  const allCodes = ordered.flatMap((i) => i.postcodes.map((p) => ({ ...p, url: i.url, role: i.role })));
  if (!address && allCodes.length) {
    const rank: Record<string, number> = { contact: 0, about: 1, home: 2 };
    const sorted = [...allCodes].sort((a, b) => (rank[a.role] ?? 3) - (rank[b.role] ?? 3));
    postcode = sorted[0].pc;
    address = addressFromContext(sorted[0].ctx);
    addrSrc = [sorted[0].url, sorted[0].ctx.slice(-240)];
  }
  if (postcode) {
    const pr = postcodeRegion(postcode);
    if (pr) result.region = `${pr[2]}・${pr[1]}エリア（郵便番号 ${pr[0]}）`;
    result.postcode = postcode;
    result.address = address;
    if (addrSrc) result.evidence.push(["所在地", addrSrc[0], addrSrc[1]]);
  }

  const phonesAll = ordered.flatMap((i) => i.phones.map((p) => p.n));
  const codesForUk = allCodes.length ? allCodes.map((c) => c.pc) : postcode ? [postcode] : [];
  const allText = ordered.map((i) => i.text.slice(0, 50000)).join(" ");
  let [uk, ukNote] = C.detectUk(codesForUk, phonesAll, allText, siteHost);
  const foreign = foreignJsonLdAddress(jsonld);
  if (foreign) {
    uk = "英国外の住所の記載あり・要確認";
    ukNote = `構造化データの住所: ${foreign}`;
    if (addrSrc && addrSrc[1].startsWith("構造化データ")) {
      result.region = null;
      result.postcode = null;
    }
  }
  result.ukCheck = uk;
  if (ukNote) result.evidence.push(["英国所在", addrSrc ? addrSrc[0] : home.url, ukNote]);

  const [used, usedEv] = C.detectUsedSales(ptexts);
  result.usedSales = used;
  if (usedEv) result.evidence.push(["中古車販売", usedEv[0], usedEv[1]]);

  const cls = C.classifyDealer(ptexts);
  result.categories = cls.categories;
  result.categoryBasis = cls.basis;
  result.categoryScope = cls.scope;
  result.evidence.push(...cls.evidence);

  const features = C.detectFeatures(ptexts);
  for (const [label, url, ex] of features) result.evidence.push(["特徴", url, `${label}: ${ex}`]);
  const regionOnly = result.postcode ? postcodeRegion(result.postcode)?.[2] ?? null : null;
  result.summary = C.buildSummary(regionOnly, result.postcode, used, cls, features.map((f) => f[0]));

  const seen = new Set<string>();
  for (const i of ordered) {
    for (const e of i.emails) {
      if (seen.has(e.email)) continue;
      seen.add(e.email);
      const j = C.judgeEmail(e.email, siteHost, e.context);
      result.contacts.push({ kind: "email", value: e.email, purpose: j.purpose, status: j.status, sourceUrl: i.url, excerpt: `${e.method}: ${e.context.slice(0, 200)}`, reason: j.reason });
    }
  }

  const roleRank: Record<string, number> = { contact: 0, home: 1, about: 2, stock: 3 };
  const formPages: [number, PageFacts, { reason: string; fields: string[] }, boolean][] = [];
  const contactPages: PageFacts[] = [];
  for (const i of ordered) {
    const cf = i.forms.filter((f) => f.kind === "問い合わせ");
    if (cf.length) {
      const vehicle = i.role === "stock" || /\/(?:cars?|vehicles?|product|stock|used-cars?)\/[^/]+|enquiry\/\d+/i.test(i.url);
      formPages.push([(roleRank[i.role] ?? 4) + (vehicle ? 5 : 0), i, cf[0], vehicle]);
    } else if (i.role === "contact") contactPages.push(i);
  }
  if (formPages.length) {
    formPages.sort((a, b) => a[0] - b[0]);
    const [, i, f, vehicle] = formPages[0];
    let note = `${f.reason}（項目: ${f.fields.slice(0, 6).map((n) => n.split(" ")[0]).filter(Boolean).join(", ")}）`;
    if (vehicle) note = "車両ごとの問い合わせフォーム。" + note;
    result.contacts.push({ kind: "form", value: i.url, purpose: "問い合わせフォーム", status: "フォーム確認済み", sourceUrl: i.url, excerpt: note, reason: "" });
  } else {
    for (const i of contactPages.slice(0, 1)) {
      let note: string;
      let status: string;
      if (i.embeds.length) {
        note = "外部フォームの埋め込みを検出: " + i.embeds.join("・") + "（中身は画面で未確認）";
        status = "要手動確認";
      } else {
        const kinds = i.forms.length ? [...new Set(i.forms.map((f) => f.kind))].sort().join("・") : "なし";
        note = `問い合わせページはあるが送信フォームを確認できません（検出したフォーム: ${kinds}）`;
        status = "問い合わせページあり・フォーム未確認";
      }
      result.contacts.push({ kind: "contact_page", value: i.url, purpose: "問い合わせページ", status, sourceUrl: i.url, excerpt: note, reason: "" });
    }
    if (!contactPages.length) {
      for (const p of pages.filter((p) => p.role === "contact" && p.status === "失敗").slice(0, 1)) {
        result.contacts.push({ kind: "contact_page", value: p.url, purpose: "問い合わせページ", status: "要手動確認", sourceUrl: p.url, excerpt: `問い合わせページを取得できませんでした: ${p.error}`, reason: "" });
      }
    }
  }

  const phonesSeen = new Set<string>();
  for (const i of ordered) {
    for (const { n, a } of i.phones) {
      const norm = n.replace(/[^\d+]/g, "");
      if (phonesSeen.has(norm) || phonesSeen.size >= 2) continue;
      phonesSeen.add(norm);
      result.contacts.push({ kind: "phone", value: n, purpose: "電話", status: "公開番号確認済み", sourceUrl: i.url, excerpt: n ? excerptAround(i.text, n.slice(-6), 160) : a, reason: "" });
    }
  }

  const failed = pages.filter((p) => p.status === "失敗");
  result.ok = true;
  if (stopped) {
    result.status = "停止";
    result.failureReason = "利用者の操作で停止しました（取得済みページまで保存）";
  } else if (failed.length) {
    result.status = "一部取得失敗";
    result.failureReason = "一部のページを取得できませんでした: " + failed.slice(0, 3).map((p) => `${p.url}（${p.error}）`).join("; ");
  } else {
    result.status = "完了";
  }
  return result;
}
