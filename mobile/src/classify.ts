// ルールによる分類・メール判定・日本語概要（外部AIなし）。PC 版 sdr/classify.py と同じ規則。
// - 「公式に専門店と明記」は公式サイトの文言に専門・販売の明記がある場合だけ。
// - 在庫の傾向は確認したページの範囲だけで判断し、範囲を記録する。専門店とは書かない。
// - 判断できないものは「要確認」「不明」とし、もっともらしい説明で埋めない。

import { postcodeRegion } from "./extract";
import { clean, excerptAround } from "./util";

export const CAT_GENERAL = "一般中古車";
export const CAT_SPORTS = "スポーツカー";
export const CAT_SUV = "ミニバン・SUV";
export const CAT_CLASSIC = "クラシックカー";
export const CAT_OTHER = "その他";
export const CAT_REVIEW = "要確認";
export const CATEGORIES = [CAT_GENERAL, CAT_SPORTS, CAT_SUV, CAT_CLASSIC, CAT_OTHER, CAT_REVIEW];

export const BASIS_OFFICIAL = "公式に専門店と明記";
export const BASIS_TREND = "確認した在庫からの傾向";
export const BASIS_MANUAL = "要手動確認";
export const BASES = [BASIS_OFFICIAL, BASIS_TREND, BASIS_MANUAL];

const SPEC = "(?:specialist|specialists|specialising|specializing|specialise|specialize|dealer|dealers|dealership|sales|centre|center|supermarket|for sale)";
const GAP = "[^.|!?]{0,40}?"; // 同じ文の中だけで対応させる
const SPORTS_MARQUES = "(?:porsche|ferrari|lamborghini|mclaren|lotus|aston martin|tvr|caterham|maserati|morgan|noble|ariel)";
const re = (s: string) => new RegExp(s, "i");

const SPECIALIST_PATTERNS: Record<string, RegExp[]> = {
  [CAT_CLASSIC]: [
    re("\\b(?:classic|vintage|historic|heritage)(?: (?:and|&) (?:classic|vintage|sports|prestige|historic|modern classic))?(?: sports)? (?:car|cars|vehicle|vehicles|motor car|motor cars|motors)\\b" + GAP + "\\b" + SPEC + "\\b"),
    re("\\b(?:specialist|specialists|dealers?|specialising|specializing)\\b (?:in|of) (?:the )?(?:sale of )?(?:classic|vintage|historic)\\b"),
    re("\\bclassic (?:car|cars) (?:dealer|dealers|dealership|specialists?)\\b"),
  ],
  [CAT_SPORTS]: [
    re("\\b(?:sports|performance|high[- ]performance|sportscar|sportscars)(?: (?:and|&) (?:prestige|performance|sports|classic))? (?:car|cars|vehicles)\\b" + GAP + "\\b(?:specialist|specialists|dealer|dealers|dealership|sales)\\b"),
    re("\\bsupercars? (?:specialist|specialists|dealer|dealers|dealership|sales)\\b"),
    re("\\b(?:specialist|specialists|dealers?|specialising|specializing)\\b (?:in|of) (?:the )?(?:sale of )?(?:prestige (?:and|&) )?(?:sports|performance|supercars?)\\b"),
    re("\\b" + SPORTS_MARQUES + "(?: (?:and|&) [a-z-]+)? (?:specialist|specialists|dealer|dealers)\\b"),
    re("\\b(?:specialist|specialists|specialising|specializing|specialise|specialize)\\b (?:in|on) (?:the )?(?:sale of )?(?:used |pre-owned |the )?" + SPORTS_MARQUES + "\\b"),
  ],
  [CAT_SUV]: [
    re("(?:\\b4[x×]4s?\\b|\\b4 x 4\\b|\\bsuvs?\\b|\\bmpvs?\\b|\\bpeople carriers?\\b|\\b7[- ]seaters?\\b|\\bseven[- ]seaters?\\b|\\bland rover\\b|\\brange rover\\b)" + GAP + "\\b(?:specialist|specialists|dealer|dealers|dealership|centre|center)\\b"),
    re("\\b(?:specialist|specialists|specialising|specializing)\\b (?:in|of) (?:the )?(?:sale of )?(?:4[x×]4s?|suvs?|mpvs?|people carriers|land rovers?|range rovers?)\\b"),
  ],
  [CAT_OTHER]: [
    re("\\b(?:wheelchair accessible vehicles?|wavs?|motorhomes?|campervans?|camper vans?|commercial vehicles?)\\b" + GAP + "\\b(?:specialist|specialists|dealer|dealers|sales|centre)\\b"),
    re("\\bvan (?:specialist|specialists|centre|center)\\b"),
    re("\\b(?:japanese|jdm) (?:car |vehicle )?imports?\\b" + GAP + "\\b(?:specialist|specialists|dealer|dealers|sales)\\b"),
    re("\\b(?:specialist|specialists)\\b (?:in|of) (?:japanese|jdm) imports?\\b"),
  ],
};

const OTHER_LABELS: [RegExp, string][] = [
  [/japanese|jdm/i, "日本からの輸入車"],
  [/wheelchair|\bwavs?\b/i, "福祉車両（車椅子対応車）"],
  [/motorhome|campervan|camper van/i, "キャンピングカー"],
  [/commercial|\bvans?\b/i, "商用車・バン"],
];

const GENERAL_PATTERNS = [
  /\bused (?:car|cars|vehicle|vehicles) (?:dealer|dealers|dealership|sales|supermarket|centre|center|specialists?)\b/i,
  /\bquality used (?:cars|vehicles)\b/i,
  /\bused cars? for sale\b/i,
];

const USED_PATTERNS = [
  /\bused (?:car|cars|vehicle|vehicles|stock)\b/i,
  /\bpre[- ]?owned\b/i,
  /\bsecond[- ]hand (?:car|cars|vehicles?)\b/i,
  /\bapproved used\b/i,
  /\b(?:classic|sports|prestige|performance) cars? for sale\b/i,
  /\b(?:cars|vehicles) for sale\b/i,
  /\bnearly new\b/i,
  /\b(?:our|current|view|browse) (?:stock|showroom|cars|vehicles)\b/i,
];
const NEW_ONLY_HINT = /\bnew (?:car|cars) (?:offers|deals|range)\b/i;
const PRICE_RE = /£\s?\d{1,3}(?:,\d{3})+|£\s?\d{4,6}\b/g;
const SPORTS_MODELS = new RegExp(
  "\\b(?:porsche (?:911|boxster|cayman|718)|ferrari|lamborghini|mclaren|lotus (?:elise|exige|evora|emira)|aston martin|" +
    "mazda mx-?5|nissan (?:gt-?r|370z|350z)|bmw (?:m2|m3|m4|m5|m8|z4)|audi (?:r8|rs\\s?\\d|tt rs)|toyota (?:gr86|gt86|supra)|" +
    "jaguar f-?type|mercedes(?:-benz)?(?:-amg)? (?:amg gt|sl\\b|slk)|corvette|caterham|ariel atom|ford mustang|honda (?:s2000|nsx|civic type r))",
  "gi",
);
const SUV_MODELS = new RegExp(
  "\\b(?:range rover|land rover (?:discovery|defender|freelander)|discovery sport|volvo xc(?:40|60|90)|kia (?:sorento|sportage)|" +
    "toyota (?:rav4|land cruiser|alphard|vellfire|estima)|nissan (?:qashqai|x-?trail|juke)|hyundai (?:tucson|santa fe)|" +
    "ford (?:galaxy|s-?max|kuga)|vw (?:touran|sharan|tiguan)|volkswagen (?:touran|sharan|tiguan|touareg)|citroen (?:berlingo|c4 picasso|grand c4)|" +
    "bmw x[1-7]|audi q[2-8]|mercedes(?:-benz)? (?:glc|gle|gls|v-class)|skoda kodiaq|seat alhambra|mitsubishi (?:outlander|shogun|l200)|" +
    "jeep|honda cr-?v|mazda cx-?\\d|peugeot (?:3008|5008)|dacia duster)\\b",
  "gi",
);
const CLASSIC_YEAR = /\b19[2-8]\d\b\s+(?:[A-Z][A-Za-z-]+)/g;

const FEATURES: [RegExp, string][] = [
  [/\bpart[- ]exchange/i, "下取り（Part Exchange）"],
  [/\bfinance\b/i, "ファイナンス"],
  [/\bwarrant(?:y|ies)\b/i, "保証"],
  [/\b(?:nationwide delivery|deliver(?:y|ies)? nationwide|uk[- ]wide delivery|delivery across the uk)\b/i, "全国配送"],
  [/\b(?:worldwide shipping|export(?:ing)? worldwide|international shipping|we export)\b/i, "海外発送・輸出"],
  [/\bfamily[- ](?:run|owned)\b/i, "家族経営"],
  [/\bmotor ombudsman\b/i, "The Motor Ombudsman の記載"],
  [/\brac approved\b/i, "RAC Approved の記載"],
  [/\baa (?:dealer promise|approved|cars)\b/i, "AA の認定等の記載"],
];
const ESTABLISHED = new RegExp(
  "\\b(?:established|est\\.|founded|trading since|in business since|family business since|family[- ](?:run|owned) (?:business )?since|dealing (?:in [a-z ]+ )?since|" +
    "serving [a-z ]{0,40}since|business since)\\s+(?:in\\s+)?((?:19|20)\\d{2})\\b",
  "i",
);
const IRELAND_HINT = /(?<!northern )\b(?:republic of ireland|co\. (?:dublin|cork|galway|kerry|limerick|meath|kildare|wicklow|louth|wexford|mayo|donegal)|eircode)\b|\+353/i;

// ---------------------------------------------------------------- メールの判定
const FREE_MAIL = new Set([
  "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.co.uk", "live.com",
  "yahoo.com", "yahoo.co.uk", "btinternet.com", "btconnect.com", "sky.com", "aol.com", "icloud.com",
  "me.com", "virginmedia.com", "talktalk.net", "ntlworld.com", "msn.com", "mail.com", "protonmail.com",
]);
const EXCLUDE_LOCAL: [RegExp, string][] = [
  [/^(?:jobs?|careers?|recruit(?:ment|ing)?|vacanc(?:y|ies)|hr|cv|apply)\b/i, "求人・採用用のアドレス"],
  [/^(?:privacy|dpo|gdpr|data[._-]?protection|dataprotection|data[._-]?controller)\b/i, "個人情報・プライバシー窓口"],
  [/^(?:no[._-]?reply|do[._-]?not[._-]?reply|mailer-daemon|postmaster|abuse|webmaster|hostmaster|bounce)/i, "自動送信・技術窓口"],
];
const DEPARTMENT_LOCAL: [RegExp, string][] = [
  [/^(?:accounts?|invoices?|billing|payroll|finance)\b/i, "経理・ファイナンス"],
  [/^(?:service|servicing|parts|workshop|bodyshop|mot|repairs?|aftersales)\b/i, "整備・部品"],
  [/^(?:complaints?)\b/i, "苦情窓口"],
  [/^(?:press|media|marketing)\b/i, "広報"],
];
const GENERAL_LOCAL = /^(?:info|sales|enquir(?:y|ies)|inquir(?:y|ies)|hello|contact|office|admin|mail|team|reception|cars|showroom|used|usedcars|general|help|customer\w*)\b/i;
const AGENCY_CONTEXT = /\b(?:website|site|web site|web design|designed|developed|built|created|powered|hosted|marketing)\s+(?:by|and hosted by)\b|\bdigital agency\b|\bweb design\b/i;
const CONTEXT_EXCLUDE: [RegExp, string][] = [
  [/\b(?:careers?|vacanc(?:y|ies)|recruitment|job opportunities|apply now|send your cv)\b/i, "求人の案内の近くに記載"],
  [/\b(?:privacy policy|data protection officer|subject access request|gdpr)\b/i, "個人情報の案内の近くに記載"],
];

export const EMAIL_OK = "公開メール確認済み";
export const EMAIL_REVIEW = "要手動確認";
export const EMAIL_EXCLUDED = "除外";

export interface EmailJudgement {
  purpose: string;
  status: string;
  reason: string;
}

export function sameSite(emailDomain: string, siteHost: string): boolean {
  const d = emailDomain.toLowerCase();
  let site = siteHost.toLowerCase();
  if (site.startsWith("www.")) site = site.slice(4);
  return d === site || d.endsWith("." + site) || site.endsWith("." + d);
}

export function judgeEmail(email: string, siteHost: string, context = ""): EmailJudgement {
  const [local, domain] = email.toLowerCase().split("@");
  for (const [p, reason] of EXCLUDE_LOCAL) if (p.test(local)) return { purpose: "除外候補", status: EMAIL_EXCLUDED, reason };
  const own = sameSite(domain, siteHost);
  if (!own && AGENCY_CONTEXT.test(context)) {
    return { purpose: "除外候補", status: EMAIL_EXCLUDED, reason: "制作会社・運営会社等の連絡先の可能性（「Website by」等の近くに記載）" };
  }
  for (const [p, reason] of CONTEXT_EXCLUDE) {
    if (p.test(context) && !GENERAL_LOCAL.test(local)) return { purpose: "除外候補", status: EMAIL_EXCLUDED, reason };
  }
  for (const [p, label] of DEPARTMENT_LOCAL) {
    if (p.test(local)) {
      const status = own || FREE_MAIL.has(domain) ? EMAIL_OK : EMAIL_REVIEW;
      return { purpose: `部門別（${label}）`, status, reason: status === EMAIL_OK ? "" : "販売店サイトと異なるドメイン" };
    }
  }
  if (own) return { purpose: GENERAL_LOCAL.test(local) ? "一般・営業窓口候補" : "個人・担当者宛ての可能性", status: EMAIL_OK, reason: "" };
  if (FREE_MAIL.has(domain)) return { purpose: "一般・営業窓口候補（無料メール）", status: EMAIL_OK, reason: "無料メールのアドレスを公式サイトで表示" };
  return { purpose: "要確認", status: EMAIL_REVIEW, reason: "販売店サイトと異なるドメインのため、販売店の窓口か要確認" };
}

// ---------------------------------------------------------------- 分類
export interface PageText {
  url: string;
  role: string;
  headline: string; // title + meta + h1 + og:site_name
  text: string;
}

export type Ev = [string, string, string]; // (項目, URL, 抜粋)

export interface Classification {
  categories: string[];
  basis: string;
  scope: string;
  evidence: Ev[];
  otherLabel: string;
}

const count = (re: RegExp, s: string) => (s.match(re) || []).length;

export function classifyDealer(pages: PageText[]): Classification {
  const evidence: Ev[] = [];
  const official: Record<string, [string, string]> = {};
  let otherLabel = "";
  for (const page of pages) {
    if (!["home", "about", "contact", "stock"].includes(page.role)) continue;
    const sources = [page.headline, page.text.slice(0, 6000)];
    for (const [cat, patterns] of Object.entries(SPECIALIST_PATTERNS)) {
      if (official[cat]) continue;
      outer: for (const src of sources) {
        for (const pat of patterns) {
          const m = src.match(pat);
          if (m) {
            official[cat] = [page.url, excerptAround(src, m[0])];
            if (cat === CAT_OTHER) {
              for (const [lp, label] of OTHER_LABELS) {
                if (lp.test(m[0])) {
                  otherLabel = label;
                  break;
                }
              }
            }
            break outer;
          }
        }
      }
    }
  }
  const stockPages = pages.filter((p) => p.role === "stock");
  const stockText = stockPages.map((p) => p.text.slice(0, 60000)).join(" ");
  const prices = count(PRICE_RE, stockText);
  const sports = count(SPORTS_MODELS, stockText);
  const suv = count(SUV_MODELS, stockText);
  const classic = count(CLASSIC_YEAR, stockText);
  const scope = stockPages.length
    ? `在庫・販売車両のページ ${stockPages.length} 件（価格表示 ${prices} 件）を確認。全在庫は未確認`
    : "在庫ページは確認できていません";
  const trend: Record<string, string> = {};
  if (prices >= 5) {
    for (const [cat, n] of [[CAT_SPORTS, sports], [CAT_SUV, suv], [CAT_CLASSIC, classic]] as [string, number][]) {
      if (n >= Math.max(5, Math.floor(prices * 0.6))) trend[cat] = `確認した在庫ページで価格表示 ${prices} 件に対し${cat}に該当する車名・年式の記載 ${n} 件`;
    }
  }
  let cats: string[] = [];
  let basis = BASIS_MANUAL;
  if (Object.keys(official).length) {
    for (const cat of [CAT_CLASSIC, CAT_SPORTS, CAT_SUV, CAT_OTHER]) {
      if (official[cat]) {
        cats.push(cat);
        evidence.push(["分類", official[cat][0], official[cat][1]]);
      }
    }
    basis = BASIS_OFFICIAL;
    for (const [cat, note] of Object.entries(trend)) {
      if (!cats.includes(cat)) {
        cats.push(cat);
        evidence.push(["分類", stockPages[0].url, note]);
      }
    }
  } else if (Object.keys(trend).length) {
    for (const [cat, note] of Object.entries(trend)) {
      cats.push(cat);
      evidence.push(["分類", stockPages[0].url, note]);
    }
    basis = BASIS_TREND;
  } else {
    let hit: [string, string] | null = null;
    for (const page of pages) {
      for (const pat of GENERAL_PATTERNS) {
        const m = page.headline.match(pat) || page.text.slice(0, 6000).match(pat);
        if (m) {
          hit = [page.url, excerptAround(page.headline + " " + page.text.slice(0, 6000), m[0])];
          break;
        }
      }
      if (hit) break;
    }
    if (hit) {
      cats.push(CAT_GENERAL);
      evidence.push(["分類", hit[0], hit[1]]);
      basis = prices >= 5 ? BASIS_TREND : BASIS_MANUAL;
      if (prices >= 5) evidence.push(["分類", stockPages[0].url, `確認した在庫ページで特定分野への偏りなし（価格表示 ${prices} 件）`]);
    } else if (prices >= 5) {
      cats.push(CAT_GENERAL);
      basis = BASIS_TREND;
      evidence.push(["分類", stockPages[0].url, `確認した在庫ページで特定分野への偏りなし（価格表示 ${prices} 件）`]);
    }
  }
  if (!cats.length) {
    cats = [CAT_REVIEW];
    basis = BASIS_MANUAL;
  }
  return { categories: cats, basis, scope, evidence, otherLabel };
}

export function detectUsedSales(pages: PageText[]): [string, [string, string] | null] {
  for (const page of pages) {
    for (const src of [page.headline, page.text.slice(0, 20000)]) {
      for (const pat of USED_PATTERNS) {
        const m = src.match(pat);
        if (m) return ["中古車販売を確認", [page.url, excerptAround(src, m[0])]];
      }
    }
  }
  for (const page of pages) {
    const m = page.text.slice(0, 20000).match(NEW_ONLY_HINT);
    if (m) return ["新車中心の可能性・要確認", [page.url, excerptAround(page.text, m[0])]];
  }
  return ["未確認", null];
}

export function detectUk(postcodes: string[], phones: string[], allText: string, host: string): [string, string] {
  const uk = postcodes.filter((p) => {
    const r = postcodeRegion(p);
    return r && r[2] !== "王室属領（英国外・要確認）";
  });
  if (host.endsWith(".ie") || IRELAND_HINT.test(allText.slice(0, 200000))) {
    if (uk.length) return ["英国の住所あり・英国外の記載もあり要確認", "英国の郵便番号とアイルランド等の記載の両方を検出"];
    return ["英国外（アイルランド等）の可能性", "アイルランドの地名・電話番号・ドメイン等を検出"];
  }
  if (uk.length) return ["英国内の住所を確認", `郵便番号 ${uk[0]}`];
  if (postcodes.length) return ["王室属領の住所（英国外）・要確認", `郵便番号 ${postcodes[0]}`];
  if (phones.some((p) => /^(\+44|0)/.test(p.replace(/[\s()-]/g, "")))) return ["英国の電話番号のみ確認（住所は未確認）", phones[0]];
  return ["未確認", ""];
}

export function detectFeatures(pages: PageText[]): Ev[] {
  const found: Ev[] = [];
  const labels = new Set<string>();
  for (const page of pages) {
    const text = page.headline + " " + page.text.slice(0, 30000);
    for (const [pat, label] of FEATURES) {
      if (labels.has(label)) continue;
      const m = text.match(pat);
      if (m) {
        labels.add(label);
        found.push([label, page.url, excerptAround(text, m[0])]);
      }
    }
    const m = text.match(ESTABLISHED);
    if (m && ![...labels].some((l) => l.endsWith("年創業の記載"))) {
      const year = Number(m[1]);
      if (year >= 1890 && year <= new Date().getUTCFullYear()) {
        const label = `${year}年創業の記載`;
        labels.add(label);
        found.push([label, page.url, excerptAround(text, m[0])]);
      }
    }
  }
  return found;
}

// ---------------------------------------------------------------- 日本語概要（1〜3文）
export function buildSummary(region: string | null, postcode: string | null, usedSales: string, cls: Classification, features: string[]): string {
  const s: string[] = [];
  if (region) {
    const place = postcode ? `${region}（郵便番号 ${postcode}）` : region;
    s.push(usedSales === "中古車販売を確認" ? `${place}の中古車販売店です。` : `${place}の自動車販売事業者で、中古車販売は${usedSales}です。`);
  } else {
    s.push(usedSales === "中古車販売を確認" ? "中古車販売を確認した事業者です（所在地は公式サイトから未確認）。" : `所在地は未確認で、中古車販売は${usedSales}です。`);
  }
  const cats = cls.categories.filter((c) => c !== CAT_REVIEW);
  let label = cats.join("・");
  if (cls.otherLabel && cats.includes(CAT_OTHER)) label = label.replace(CAT_OTHER, `その他（${cls.otherLabel}）`);
  if (cls.basis === BASIS_OFFICIAL && cats.length) s.push(`公式サイトに${label}の専門・販売の記載があります。`);
  else if (cls.basis === BASIS_TREND && cats.length) {
    s.push(cats.length === 1 && cats[0] === CAT_GENERAL
      ? "確認した在庫ページの範囲では、特定分野に偏らない車両を掲載しています（全在庫は未確認）。"
      : `確認した在庫ページの範囲では${label}の掲載が多く見られます（全在庫は未確認）。`);
  } else s.push("取扱分野は公式サイトの記載だけでは判断できず、要確認です。");
  if (features.length) s.push("公式サイトに " + features.slice(0, 5).join("・") + " に関する記載があります。");
  return s.slice(0, 3).join("");
}

const GENERIC_NAMES = /^(?:home|homepage|welcome|index|sales|contact|contact us|about|about us|used cars?|cars|car sales|showroom|stock|our stock|search|menu)$/i;

function usableName(name: string): boolean {
  const n = clean(name);
  return n.length > 2 && n.length <= 60 && !GENERIC_NAMES.test(n) && (n.match(/,/g) || []).length < 2 && !/\b(?:for sale|second hand|cheap)\b/i.test(n);
}

export function pickName(jsonld: Record<string, unknown>[], siteName: string, title: string, host: string): string {
  for (const obj of jsonld) {
    const t = obj["@type"];
    const types = Array.isArray(t) ? t : [t];
    if (types.some((x) => ["AutoDealer", "AutomotiveBusiness", "LocalBusiness", "Organization", "Store"].includes(String(x)))) {
      const name = clean(String(obj.name || ""));
      if (usableName(name)) return name;
    }
  }
  if (siteName && usableName(siteName)) return clean(siteName);
  if (title) {
    const parts = title.split(/\s[|\-–—:•]\s|\s\|\s?/).map(clean).filter(Boolean);
    const generic = /^(?:home|homepage|welcome|used cars?.*|.*for sale.*|.*\bin [A-Z][a-z]+.*)$/i;
    for (const part of parts.length > 1 ? [...parts].reverse() : parts) {
      if (!generic.test(part) && usableName(part)) return part;
    }
  }
  return host;
}
