"""候補の発見（公式サイト調査とは別の処理）。

- OpenStreetMap（Overpass API）：APIキー不要・無料。shop=car で website が登録された店。
  OSM の分類だけでは中古車販売か・分野は分からないため、調査後に判断する。
- Firecrawl 検索 API：利用者が自分の APIキーを設定した場合のみ。月間の上限を超えたら止める。
- 手動補助：検索語をブラウザーで開くリンクを作るだけ（アプリは検索結果を取得しない）。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx

from .config import USER_AGENT
from .extract import is_aggregator

# OSM Wiki「Overpass API」の公開インスタンス一覧より（英国・アイルランド向けを優先）
OVERPASS_ENDPOINTS = [
    "https://overpass.atownsend.org.uk/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v2/search"

# 地域は範囲（南, 西, 北, 東）で指定する（Overpass の負荷を抑えるため市域程度の大きさ）
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "London（ロンドン中心部）": (51.45, -0.25, 51.56, 0.05),
    "Greater London 西部": (51.40, -0.50, 51.60, -0.20),
    "Greater London 東部": (51.45, 0.0, 51.65, 0.30),
    "Birmingham（バーミンガム）": (52.38, -2.03, 52.60, -1.72),
    "Manchester（マンチェスター）": (53.38, -2.35, 53.55, -2.10),
    "Liverpool（リバプール）": (53.33, -3.05, 53.48, -2.85),
    "Leeds（リーズ）": (53.73, -1.70, 53.88, -1.40),
    "Sheffield（シェフィールド）": (53.32, -1.58, 53.45, -1.35),
    "Bristol（ブリストル）": (51.40, -2.70, 51.52, -2.50),
    "Nottingham（ノッティンガム）": (52.88, -1.25, 53.02, -1.05),
    "Leicester（レスター）": (52.58, -1.20, 52.68, -1.05),
    "Newcastle（ニューカッスル）": (54.93, -1.75, 55.05, -1.50),
    "Southampton（サウサンプトン）": (50.87, -1.50, 50.97, -1.30),
    "Surrey（サリー）": (51.15, -0.75, 51.40, -0.10),
    "Kent（ケント）": (51.05, 0.30, 51.45, 1.40),
    "Essex（エセックス）": (51.50, 0.20, 51.95, 1.10),
    "Glasgow（グラスゴー）": (55.80, -4.40, 55.92, -4.10),
    "Edinburgh（エディンバラ）": (55.88, -3.35, 55.99, -3.08),
    "Cardiff（カーディフ）": (51.43, -3.28, 51.54, -3.10),
    "Belfast（ベルファスト）": (54.55, -6.03, 54.65, -5.85),
}

CATEGORY_QUERIES = {
    "一般中古車": "used car dealer",
    "スポーツカー": "sports car specialist dealer used",
    "ミニバン・SUV": "used 4x4 SUV MPV specialist dealer",
    "クラシックカー": "classic car dealer",
}


@dataclass
class FoundCandidate:
    url: str
    name: str | None
    hint: str | None
    detail: str


class DiscoveryError(RuntimeError):
    pass


def overpass_query(bbox: tuple[float, float, float, float], limit: int) -> str:
    s, w, n, e = bbox
    box = f"({s},{w},{n},{e})"
    return (
        "[out:json][timeout:60];("
        f'nwr["shop"="car"]["website"]{box};'
        f'nwr["shop"="car"]["contact:website"]{box};'
        f");out tags center {int(limit * 3)};"
    )


def parse_overpass(data: dict, limit: int) -> list[FoundCandidate]:
    found: list[FoundCandidate] = []
    seen = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        url = tags.get("website") or tags.get("contact:website")
        if not url or is_aggregator(url):
            continue
        if tags.get("second_hand") == "no":
            continue  # OSM 上で中古車を扱わないと記録された店
        hints = []
        sh = tags.get("second_hand")
        if sh in ("only", "yes"):
            hints.append(f"OSM: second_hand={sh}")
        if tags.get("brand"):
            hints.append(f"OSM: brand={tags['brand']}（正規ディーラーの可能性。新車専売でないか要確認）")
        key = url.lower().split("//")[-1].strip("/").removeprefix("www.")
        if key in seen:
            continue
        seen.add(key)
        name = tags.get("name")
        found.append(FoundCandidate(url, name, "；".join(hints) or None,
                                    f"OSM {el.get('type')}/{el.get('id')}"))
        if len(found) >= limit:
            break
    # second_hand=only/yes を優先
    found.sort(key=lambda c: 0 if c.hint and "second_hand" in c.hint else 1)
    return found


def discover_osm(region: str, limit: int, client: httpx.Client | None = None) -> list[FoundCandidate]:
    if region not in REGIONS:
        raise DiscoveryError("地域を一覧から選んでください")
    query = overpass_query(REGIONS[region], limit)
    own = client is None
    client = client or httpx.Client(timeout=90, headers={"User-Agent": USER_AGENT}, trust_env=True)
    errors = []
    try:
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                resp = client.post(endpoint, data={"data": query})
            except httpx.HTTPError as exc:
                errors.append(f"{endpoint}: {type(exc).__name__}")
                continue
            if resp.status_code != 200:
                errors.append(f"{endpoint}: HTTP {resp.status_code}")
                continue
            try:
                return parse_overpass(resp.json(), limit)
            except ValueError:
                errors.append(f"{endpoint}: 応答を読み取れません")
        raise DiscoveryError("OpenStreetMap から取得できませんでした（" + " / ".join(errors) + "）")
    finally:
        if own:
            client.close()


def discover_firecrawl(api_key: str, category: str, region: str, limit: int,
                       client: httpx.Client | None = None) -> tuple[list[FoundCandidate], float]:
    """戻り値 (候補, 消費クレジット)。"""
    if not api_key:
        raise DiscoveryError("Firecrawl の APIキーが設定されていません（設定画面で入力）")
    base = CATEGORY_QUERIES.get(category, "used car dealer")
    place = region.split("（")[0]
    query = f"{base} {place} UK"
    own = client is None
    client = client or httpx.Client(timeout=60, trust_env=True)
    try:
        resp = client.post(FIRECRAWL_SEARCH, headers={"Authorization": f"Bearer {api_key}"},
                           json={"query": query, "limit": limit, "country": "GB", "location": "United Kingdom",
                                 "sources": [{"type": "web"}]})
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"Firecrawl に接続できません: {type(exc).__name__}") from exc
    finally:
        if own:
            client.close()
    if resp.status_code == 402:
        raise DiscoveryError("Firecrawl の無料枠・残高が不足しています。アプリは有料プランへ切り替えません")
    if resp.status_code == 401:
        raise DiscoveryError("Firecrawl の APIキーが正しくありません")
    if resp.status_code == 429:
        raise DiscoveryError("Firecrawl の回数制限に達しました。時間をおいてください")
    if resp.status_code != 200:
        raise DiscoveryError(f"Firecrawl がエラーを返しました（HTTP {resp.status_code}）")
    data = resp.json()
    web = (data.get("data") or {}).get("web") or []
    credits = float(data.get("creditsUsed") or max(1, round(limit / 10)) * 2)
    found = []
    for item in web:
        url = item.get("url")
        if not url or is_aggregator(url):
            continue
        found.append(FoundCandidate(url, item.get("title"), (item.get("description") or "")[:200] or None,
                                    f"Firecrawl 検索「{query}」"))
    return found, credits


def manual_search_links(category: str, region: str) -> list[tuple[str, str]]:
    base = CATEGORY_QUERIES.get(category, "used car dealer")
    place = region.split("（")[0]
    q = f"{base} {place}"
    return [
        ("Google 検索", "https://www.google.com/search?q=" + quote_plus(q)),
        ("Bing 検索", "https://www.bing.com/search?q=" + quote_plus(q)),
        ("Google マップ", "https://www.google.com/maps/search/" + quote_plus(q)),
        ("OpenStreetMap", "https://www.openstreetmap.org/search?query=" + quote_plus(f"car dealer {place}")),
    ]


# 資格情報保管（sdr/secretstore.py）での名前。旧版の secrets.json と同じ名前なので、そのまま読み取れる
FIRECRAWL_KEY = "firecrawl_api_key"
