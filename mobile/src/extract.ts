// 抽出の規則（文字列処理だけ。HTML の読み取りは parse.ts）。PC 版 sdr/extract.py と同じ規則。

import { clean } from "./util";

export const EXCERPT_LEN = 240;
export const EMAIL_RE = /(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})(?![\w-])/g;
export const UK_POSTCODE_RE = /\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b/g;
export const UK_POSTCODE_FULL = /^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$/;
const PHONE_RE = /(?:\+44\s?\(?0?\)?\s?|\b0)(?:\d[\s-]?){9,10}\b/g;
const IMAGE_EXT = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico"];
const PLACEHOLDER_DOMAINS = new Set([
  "example.com", "example.co.uk", "domain.com", "email.com", "yourdomain.com", "yourdomain.co.uk",
  "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "test.com", "mysite.com", "company.com",
]);

// 掲載サイト・検索サイト・SNS・大手チェーン（販売店本体として登録しない）
export const AGGREGATOR_DOMAINS = [
  "autotrader.co.uk", "carwow.co.uk", "cazoo.co.uk", "motors.co.uk", "gumtree.com", "ebay.co.uk", "ebay.com",
  "pistonheads.com", "carandclassic.com", "cargurus.co.uk", "heycar.com", "heycar.co.uk", "cinch.co.uk",
  "carsite.co.uk", "exchangeandmart.co.uk", "autoscout24.com", "donedeal.ie", "carzone.ie", "yell.com",
  "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "tiktok.com",
  "google.com", "google.co.uk", "bing.com", "yelp.com", "yelp.co.uk", "tripadvisor.co.uk", "wikipedia.org",
  "find-and-update.company-information.service.gov.uk", "checkatrade.com", "trustpilot.com", "classicdriver.com",
  "carandclassic.co.uk", "collectingcars.com", "bringatrailer.com", "classiccarsforsale.co.uk", "carvana.co.uk",
  "motorpoint.co.uk", "arnoldclark.com", "carshop.co.uk", "theaa.com", "rac.co.uk", "bidtodrive.co.uk",
  "autotrader.com", "webuyanycar.com", "car-dealers.co.uk", "scoot.co.uk", "freeindex.co.uk", "thomsonlocal.com",
  "cylex-uk.co.uk", "192.com", "carsnip.com",
];

export function isAggregator(url: string): boolean {
  let host = "";
  try {
    host = new URL(url.includes("://") ? url : "https://" + url).hostname.toLowerCase();
  } catch {
    return false;
  }
  return AGGREGATOR_DOMAINS.some((d) => host === d || host.endsWith("." + d));
}

export function decodeCfEmail(hex: string): string {
  if (!/^[0-9a-fA-F]+$/.test(hex) || hex.length < 4 || hex.length % 2) return "";
  const bytes = hex.match(/../g)!.map((h) => parseInt(h, 16));
  const key = bytes[0];
  return bytes.slice(1).map((b) => String.fromCharCode(b ^ key)).join("");
}

export function validEmail(raw: string): boolean {
  const email = raw.trim().replace(/^\.+|\.+$/g, "").toLowerCase();
  const re = new RegExp("^" + EMAIL_RE.source.replace("(?<![\\w.+-])", "").replace("(?![\\w-])", "") + "$");
  if (!re.test(email)) return false;
  if (IMAGE_EXT.some((e) => email.endsWith(e))) return false;
  const [local, domain] = email.split("@");
  if (PLACEHOLDER_DOMAINS.has(domain) || domain.endsWith(".wixpress.com")) return false;
  if (/^[0-9a-f]{20,}$/.test(local)) return false;
  return true;
}

export interface FoundEmail {
  email: string;
  method: string; // mailto / 本文 / 保護表示の復号 / 構造化データ
  context: string;
  inFooter: boolean;
}

export interface FoundForm {
  kind: string; // 問い合わせ / 検索 / ログイン / ニュースレター / 在庫検索 / 不明
  reason: string;
  fields: string[];
}

export interface PageFacts {
  url: string;
  role: string;
  title: string;
  siteName: string;
  metaDescription: string;
  h1: string[];
  text: string;
  footer: string;
  links: { h: string; a: string }[];
  emails: FoundEmail[];
  phones: { n: string; a: string }[];
  forms: FoundForm[];
  embeds: string[];
  jsonld: Record<string, unknown>[];
  postcodes: { pc: string; ctx: string }[];
  truncated: boolean;
}

// ---------------------------------------------------------------- フォームの判定
const SEARCH_NAMES = new Set(["q", "s", "search", "query", "keyword", "keywords", "term", "searchterm"]);
const VEHICLE_FILTER_HINTS = ["make", "model", "price", "mileage", "fuel", "transmission", "body", "year", "minprice", "maxprice"];
const NEWSLETTER_HINTS = ["newsletter", "subscribe", "sign up", "signup", "mailing list", "mailchimp", "klaviyo"];
const MESSAGE_HINTS = ["message", "enquiry", "inquiry", "comment", "question", "details", "your-message", "msg"];

export interface RawForm {
  attrs: string; // action id class name role
  role: string;
  text: string;
  inputs: { type: string; names: string }[];
}

export function classifyForm(f: RawForm): FoundForm {
  const fields = f.inputs.filter((i) => !["hidden", "submit", "button", "image", "reset"].includes(i.type));
  const types = fields.map((i) => i.type);
  const names = fields.map((i) => i.names.toLowerCase().trim());
  const joined = names.join(" ");
  const attrs = f.attrs.toLowerCase();
  const text = f.text.toLowerCase();
  if (types.includes("password")) return { kind: "ログイン", reason: "パスワード欄があるためログイン用と判断", fields: names };
  if (f.role === "search" || types.includes("search") || (types.length <= 2 && names.some((n) => SEARCH_NAMES.has(n.split(" ")[0])))) {
    return { kind: "検索", reason: "検索欄の項目名・役割を検出", fields: names };
  }
  const hasTextarea = types.includes("textarea");
  const hasEmail = types.includes("email") || joined.includes("email") || joined.includes("e-mail");
  const hasPhone = types.includes("tel") || joined.includes("phone") || joined.includes("telephone");
  const hasMessage = hasTextarea || MESSAGE_HINTS.some((h) => joined.includes(h));
  if (NEWSLETTER_HINTS.some((h) => attrs.includes(h) || text.includes(h)) && !hasTextarea && types.length <= 3) {
    return { kind: "ニュースレター", reason: "メール登録（ニュースレター）用の文言を検出", fields: names };
  }
  const vehicleHits = VEHICLE_FILTER_HINTS.filter((h) => joined.includes(h)).length;
  if (vehicleHits >= 2 && !hasTextarea) return { kind: "在庫検索", reason: "メーカー・車種・価格などの絞り込み項目を検出", fields: names };
  if (hasMessage && (hasEmail || hasPhone) && types.length >= 2) {
    return { kind: "問い合わせ", reason: "本文欄と連絡先欄（メール・電話）を持つ送信フォーム", fields: names };
  }
  if (attrs.includes("search") && types.length <= 3) return { kind: "検索", reason: "送信先・属性に search を含む", fields: names };
  return { kind: "不明", reason: "問い合わせフォームと判断できる項目がそろっていない", fields: names };
}

// ---------------------------------------------------------------- 本文からの補完
export function emailsFromText(text: string, footer: string, known: Set<string>): FoundEmail[] {
  const deob = text.replace(/\s*[[(]\s*at\s*[\])]\s*/gi, "@").replace(/\s*[[(]\s*dot\s*[\])]\s*/gi, ".");
  const out: FoundEmail[] = [];
  for (const m of deob.matchAll(EMAIL_RE)) {
    const email = m[1].toLowerCase().replace(/\.+$/, "");
    if (known.has(email) || !validEmail(email)) continue;
    known.add(email);
    const idx = m.index ?? 0;
    out.push({ email, method: "本文", context: deob.slice(Math.max(0, idx - 100), idx + email.length + 100), inFooter: footer.toLowerCase().includes(email) });
  }
  return out;
}

export function postcodesFromText(text: string): { pc: string; ctx: string }[] {
  const out: { pc: string; ctx: string }[] = [];
  const seen = new Set<string>();
  for (const m of text.matchAll(UK_POSTCODE_RE)) {
    const area = m[1].match(/^[A-Z]+/)![0];
    if (!(area in POSTCODE_AREAS)) continue;
    const pc = `${m[1]} ${m[2]}`;
    if (seen.has(pc)) continue;
    seen.add(pc);
    const idx = m.index ?? 0;
    out.push({ pc, ctx: text.slice(Math.max(0, idx - 140), idx + m[0].length) });
    if (out.length >= 10) break;
  }
  return out;
}

export function phonesFromText(text: string): { n: string; a: string }[] {
  const out: { n: string; a: string }[] = [];
  for (const m of text.slice(0, 50000).matchAll(PHONE_RE)) {
    out.push({ n: clean(m[0]), a: "" });
    if (out.length >= 3) break;
  }
  return out;
}

// ---------------------------------------------------------------- 郵便番号エリア → (ポストタウン, 地方)
const LON = "ロンドン", SE = "イングランド南東部", SW = "イングランド南西部", E = "イングランド東部", EM = "イースト・ミッドランズ",
  WM = "ウェスト・ミッドランズ", Y = "ヨークシャー", NW = "イングランド北西部", NE = "イングランド北東部",
  SC = "スコットランド", WA = "ウェールズ", NI = "北アイルランド", CD = "王室属領（英国外・要確認）";

export const POSTCODE_AREAS: Record<string, [string, string]> = {
  AB: ["Aberdeen", SC], AL: ["St Albans", E], B: ["Birmingham", WM], BA: ["Bath", SW], BB: ["Blackburn", NW],
  BD: ["Bradford", Y], BH: ["Bournemouth", SW], BL: ["Bolton", NW], BN: ["Brighton", SE], BR: ["Bromley", LON],
  BS: ["Bristol", SW], BT: ["Belfast", NI], CA: ["Carlisle", NW], CB: ["Cambridge", E], CF: ["Cardiff", WA],
  CH: ["Chester", NW], CM: ["Chelmsford", E], CO: ["Colchester", E], CR: ["Croydon", LON], CT: ["Canterbury", SE],
  CV: ["Coventry", WM], CW: ["Crewe", NW], DA: ["Dartford", SE], DD: ["Dundee", SC], DE: ["Derby", EM],
  DG: ["Dumfries", SC], DH: ["Durham", NE], DL: ["Darlington", NE], DN: ["Doncaster", Y], DT: ["Dorchester", SW],
  DY: ["Dudley", WM], E: ["London E", LON], EC: ["London EC", LON], EH: ["Edinburgh", SC], EN: ["Enfield", LON],
  EX: ["Exeter", SW], FK: ["Falkirk", SC], FY: ["Blackpool", NW], G: ["Glasgow", SC], GL: ["Gloucester", SW],
  GU: ["Guildford", SE], GY: ["Guernsey", CD], HA: ["Harrow", LON], HD: ["Huddersfield", Y], HG: ["Harrogate", Y],
  HP: ["Hemel Hempstead", SE], HR: ["Hereford", WM], HS: ["Outer Hebrides", SC], HU: ["Hull", Y], HX: ["Halifax", Y],
  IG: ["Ilford", LON], IM: ["Isle of Man", CD], IP: ["Ipswich", E], IV: ["Inverness", SC], JE: ["Jersey", CD],
  KA: ["Kilmarnock", SC], KT: ["Kingston upon Thames", SE], KW: ["Kirkwall", SC], KY: ["Kirkcaldy", SC],
  L: ["Liverpool", NW], LA: ["Lancaster", NW], LD: ["Llandrindod Wells", WA], LE: ["Leicester", EM],
  LL: ["Llandudno", WA], LN: ["Lincoln", EM], LS: ["Leeds", Y], LU: ["Luton", E], M: ["Manchester", NW],
  ME: ["Rochester", SE], MK: ["Milton Keynes", SE], ML: ["Motherwell", SC], N: ["London N", LON],
  NE: ["Newcastle upon Tyne", NE], NG: ["Nottingham", EM], NN: ["Northampton", EM], NP: ["Newport", WA],
  NR: ["Norwich", E], NW: ["London NW", LON], OL: ["Oldham", NW], OX: ["Oxford", SE], PA: ["Paisley", SC],
  PE: ["Peterborough", E], PH: ["Perth", SC], PL: ["Plymouth", SW], PO: ["Portsmouth", SE], PR: ["Preston", NW],
  RG: ["Reading", SE], RH: ["Redhill", SE], RM: ["Romford", LON], S: ["Sheffield", Y], SA: ["Swansea", WA],
  SE: ["London SE", LON], SG: ["Stevenage", E], SK: ["Stockport", NW], SL: ["Slough", SE], SM: ["Sutton", LON],
  SN: ["Swindon", SW], SO: ["Southampton", SE], SP: ["Salisbury", SW], SR: ["Sunderland", NE],
  SS: ["Southend-on-Sea", E], ST: ["Stoke-on-Trent", WM], SW: ["London SW", LON], SY: ["Shrewsbury", WM],
  TA: ["Taunton", SW], TD: ["Galashiels", SC], TF: ["Telford", WM], TN: ["Tonbridge", SE], TQ: ["Torquay", SW],
  TR: ["Truro", SW], TS: ["Middlesbrough", NE], TW: ["Twickenham", LON], UB: ["Southall", LON], W: ["London W", LON],
  WA: ["Warrington", NW], WC: ["London WC", LON], WD: ["Watford", E], WF: ["Wakefield", Y], WN: ["Wigan", NW],
  WR: ["Worcester", WM], WS: ["Walsall", WM], WV: ["Wolverhampton", WM], YO: ["York", Y], ZE: ["Shetland", SC],
};

export function postcodeRegion(postcode: string): [string, string, string] | null {
  const m = postcode.toUpperCase().match(/^([A-Z]{1,2})\d/);
  if (!m || !(m[1] in POSTCODE_AREAS)) return null;
  const [town, region] = POSTCODE_AREAS[m[1]];
  return [m[1], town, region];
}
