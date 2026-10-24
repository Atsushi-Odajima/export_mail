// 候補の発見（公式サイトの調査とは別の処理）。
// - OpenStreetMap（Overpass API）：無料・APIキー不要
// - Firecrawl 検索：利用者が APIキーを設定した場合のみ。月間上限で止め、有料へ切り替えない
// - 手動補助：検索ページを開くリンクを作るだけ（アプリは検索結果を取得しない）

import { isAggregator } from "./extract";
import { USER_AGENT } from "./fetcher";
import { HttpError } from "./util";

export const OVERPASS_ENDPOINTS = [
  "https://overpass.atownsend.org.uk/api/interpreter", // 英国・アイルランド向け（OSM Wiki の公開インスタンス一覧より）
  "https://overpass-api.de/api/interpreter",
  "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
];
export const FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v2/search";

export const REGIONS: Record<string, [number, number, number, number]> = {
  "London（ロンドン中心部）": [51.45, -0.25, 51.56, 0.05],
  "Greater London 西部": [51.40, -0.50, 51.60, -0.20],
  "Greater London 東部": [51.45, 0.0, 51.65, 0.30],
  "Birmingham（バーミンガム）": [52.38, -2.03, 52.60, -1.72],
  "Manchester（マンチェスター）": [53.38, -2.35, 53.55, -2.10],
  "Liverpool（リバプール）": [53.33, -3.05, 53.48, -2.85],
  "Leeds（リーズ）": [53.73, -1.70, 53.88, -1.40],
  "Sheffield（シェフィールド）": [53.32, -1.58, 53.45, -1.35],
  "Bristol（ブリストル）": [51.40, -2.70, 51.52, -2.50],
  "Nottingham（ノッティンガム）": [52.88, -1.25, 53.02, -1.05],
  "Leicester（レスター）": [52.58, -1.20, 52.68, -1.05],
  "Newcastle（ニューカッスル）": [54.93, -1.75, 55.05, -1.50],
  "Southampton（サウサンプトン）": [50.87, -1.50, 50.97, -1.30],
  "Surrey（サリー）": [51.15, -0.75, 51.40, -0.10],
  "Kent（ケント）": [51.05, 0.30, 51.45, 1.40],
  "Essex（エセックス）": [51.50, 0.20, 51.95, 1.10],
  "Glasgow（グラスゴー）": [55.80, -4.40, 55.92, -4.10],
  "Edinburgh（エディンバラ）": [55.88, -3.35, 55.99, -3.08],
  "Cardiff（カーディフ）": [51.43, -3.28, 51.54, -3.10],
  "Belfast（ベルファスト）": [54.55, -6.03, 54.65, -5.85],
};

export const CATEGORY_QUERIES: Record<string, string> = {
  一般中古車: "used car dealer",
  スポーツカー: "sports car specialist dealer used",
  "ミニバン・SUV": "used 4x4 SUV MPV specialist dealer",
  クラシックカー: "classic car dealer",
};

export interface Found {
  url: string;
  name: string | null;
  hint: string | null;
  detail: string;
}

export function overpassQuery(bbox: [number, number, number, number], limit: number): string {
  const [s, w, n, e] = bbox;
  const box = `(${s},${w},${n},${e})`;
  return `[out:json][timeout:60];(nwr["shop"="car"]["website"]${box};nwr["shop"="car"]["contact:website"]${box};);out tags center ${limit * 3};`;
}

export function parseOverpass(data: any, limit: number): Found[] {
  const found: Found[] = [];
  const seen = new Set<string>();
  for (const el of data?.elements || []) {
    const tags = el.tags || {};
    const url: string | undefined = tags.website || tags["contact:website"];
    if (!url || isAggregator(url)) continue;
    if (tags.second_hand === "no") continue; // OSM 上で中古車を扱わないと記録された店
    const hints: string[] = [];
    if (tags.second_hand === "only" || tags.second_hand === "yes") hints.push(`OSM: second_hand=${tags.second_hand}`);
    if (tags.brand) hints.push(`OSM: brand=${tags.brand}（正規ディーラーの可能性。新車専売でないか要確認）`);
    const key = url.toLowerCase().split("//").pop()!.replace(/\/+$/, "").replace(/^www\./, "");
    if (seen.has(key)) continue;
    seen.add(key);
    found.push({ url, name: tags.name || null, hint: hints.join("；") || null, detail: `OSM ${el.type}/${el.id}` });
    if (found.length >= limit) break;
  }
  found.sort((a, b) => (a.hint?.includes("second_hand") ? 0 : 1) - (b.hint?.includes("second_hand") ? 0 : 1));
  return found;
}

export async function discoverOsm(region: string, limit: number, fetchImpl: typeof fetch = fetch): Promise<Found[]> {
  const bbox = REGIONS[region];
  if (!bbox) throw new HttpError(400, "地域を一覧から選んでください");
  const body = new URLSearchParams({ data: overpassQuery(bbox, limit) });
  const errors: string[] = [];
  for (const ep of OVERPASS_ENDPOINTS) {
    try {
      const r = await fetchImpl(ep, { method: "POST", body, headers: { "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded" } });
      if (r.status !== 200) {
        errors.push(`${new URL(ep).host}: HTTP ${r.status}`);
        continue;
      }
      return parseOverpass(await r.json(), limit);
    } catch (e) {
      errors.push(`${new URL(ep).host}: ${(e as Error).name}`);
    }
  }
  throw new HttpError(502, "OpenStreetMap から取得できませんでした（" + errors.join(" / ") + "）。時間をおいて再試行してください");
}

export async function discoverFirecrawl(apiKey: string, category: string, region: string, limit: number, fetchImpl: typeof fetch = fetch): Promise<{ found: Found[]; credits: number }> {
  if (!apiKey) throw new HttpError(400, "Firecrawl の APIキーが設定されていません（設定画面で入力）");
  const query = `${CATEGORY_QUERIES[category] || "used car dealer"} ${region.split("（")[0]} UK`;
  let r: Response;
  try {
    r = await fetchImpl(FIRECRAWL_SEARCH, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ query, limit, country: "GB", location: "United Kingdom", sources: [{ type: "web" }] }),
    });
  } catch (e) {
    throw new HttpError(502, `Firecrawl に接続できません: ${(e as Error).name}`);
  }
  if (r.status === 402) throw new HttpError(402, "Firecrawl の無料枠・残高が不足しています。アプリは有料プランへ切り替えません");
  if (r.status === 401) throw new HttpError(400, "Firecrawl の APIキーが正しくありません");
  if (r.status === 429) throw new HttpError(429, "Firecrawl の回数制限に達しました。時間をおいてください");
  if (r.status !== 200) throw new HttpError(502, `Firecrawl がエラーを返しました（HTTP ${r.status}）`);
  const data: any = await r.json();
  const web: any[] = data?.data?.web || [];
  const credits = Number(data?.creditsUsed || Math.max(1, Math.ceil(limit / 10)) * 2);
  const found: Found[] = [];
  for (const item of web) {
    if (!item?.url || isAggregator(item.url)) continue;
    found.push({ url: item.url, name: item.title || null, hint: (item.description || "").slice(0, 200) || null, detail: `Firecrawl 検索「${query}」` });
  }
  return { found, credits };
}

export function manualSearchLinks(category: string, region: string): { label: string; url: string }[] {
  const q = `${CATEGORY_QUERIES[category] || "used car dealer"} ${region.split("（")[0]}`;
  const e = encodeURIComponent;
  return [
    { label: "Google 検索", url: `https://www.google.com/search?q=${e(q)}` },
    { label: "Bing 検索", url: `https://www.bing.com/search?q=${e(q)}` },
    { label: "Google マップ", url: `https://www.google.com/maps/search/${e(q)}` },
    { label: "OpenStreetMap", url: `https://www.openstreetmap.org/search?query=${e("car dealer " + region.split("（")[0])}` },
  ];
}
