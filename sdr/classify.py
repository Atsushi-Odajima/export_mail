"""ルールによる分類・メール判定・日本語概要（外部AIなし）。

原則
- 「公式に専門店と明記」は、公式サイトの文言に専門・販売の明記がある場合だけ。
- 在庫の傾向は確認したページの範囲だけで判断し、範囲を必ず記録する。専門店とは書かない。
- 判断できないものは「要確認」「不明」とし、もっともらしい説明で埋めない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extract import clean, excerpt_around

CAT_GENERAL = "一般中古車"
CAT_SPORTS = "スポーツカー"
CAT_SUV = "ミニバン・SUV"
CAT_CLASSIC = "クラシックカー"
CAT_OTHER = "その他"
CAT_REVIEW = "要確認"
CATEGORIES = [CAT_GENERAL, CAT_SPORTS, CAT_SUV, CAT_CLASSIC, CAT_OTHER, CAT_REVIEW]

BASIS_OFFICIAL = "公式に専門店と明記"
BASIS_TREND = "確認した在庫からの傾向"
BASIS_MANUAL = "要手動確認"
BASES = [BASIS_OFFICIAL, BASIS_TREND, BASIS_MANUAL]

_SPEC = r"(?:specialist|specialists|specialising|specializing|specialise|specialize|dealer|dealers|dealership|sales|centre|center|supermarket|for sale)"

_GAP = r"[^.|!?]{0,40}?"  # 同じ文の中だけで対応させる
SPORTS_MARQUES = r"(?:porsche|ferrari|lamborghini|mclaren|lotus|aston martin|tvr|caterham|maserati|morgan|noble|ariel)"

SPECIALIST_PATTERNS: dict[str, list[re.Pattern]] = {
    CAT_CLASSIC: [
        re.compile(r"\b(?:classic|vintage|historic|heritage)(?: (?:and|&) (?:classic|vintage|sports|prestige|historic|modern classic))?(?: sports)? (?:car|cars|vehicle|vehicles|motor car|motor cars|motors)\b" + _GAP + r"\b" + _SPEC + r"\b", re.I),
        re.compile(r"\b(?:specialist|specialists|dealers?|specialising|specializing)\b (?:in|of) (?:the )?(?:sale of )?(?:classic|vintage|historic)\b", re.I),
        re.compile(r"\bclassic (?:car|cars) (?:dealer|dealers|dealership|specialists?)\b", re.I),
    ],
    CAT_SPORTS: [
        re.compile(r"\b(?:sports|performance|high[- ]performance|sportscar|sportscars)(?: (?:and|&) (?:prestige|performance|sports|classic))? (?:car|cars|vehicles)\b" + _GAP + r"\b(?:specialist|specialists|dealer|dealers|dealership|sales)\b", re.I),
        re.compile(r"\bsupercars? (?:specialist|specialists|dealer|dealers|dealership|sales)\b", re.I),
        re.compile(r"\b(?:specialist|specialists|dealers?|specialising|specializing)\b (?:in|of) (?:the )?(?:sale of )?(?:prestige (?:and|&) )?(?:sports|performance|supercars?)\b", re.I),
        # スポーツカーのメーカー専門（例：Independent Porsche Specialists）
        re.compile(r"\b" + SPORTS_MARQUES + r"(?: (?:and|&) [a-z-]+)? (?:specialist|specialists|dealer|dealers)\b", re.I),
        re.compile(r"\b(?:specialist|specialists|specialising|specializing|specialise|specialize)\b (?:in|on) (?:the )?(?:sale of )?(?:used |pre-owned |the )?" + SPORTS_MARQUES + r"\b", re.I),
    ],
    CAT_SUV: [
        re.compile(r"\b(?:4[x×]4|4 x 4|4[x×]4s|suv|suvs|mpv|mpvs|people carriers?|7[- ]seaters?|seven[- ]seaters?|land rover|range rover)\b" + _GAP + r"\b(?:specialist|specialists|dealer|dealers|dealership|centre|center)\b", re.I),
        re.compile(r"\b(?:specialist|specialists|specialising|specializing)\b (?:in|of) (?:the )?(?:sale of )?(?:4[x×]4s?|suvs?|mpvs?|people carriers|land rovers?|range rovers?)\b", re.I),
    ],
    CAT_OTHER: [
        re.compile(r"\b(?:wheelchair accessible vehicles?|wavs?|motorhomes?|campervans?|camper vans?|commercial vehicles?)\b" + _GAP + r"\b(?:specialist|specialists|dealer|dealers|sales|centre)\b", re.I),
        re.compile(r"\bvan (?:specialist|specialists|centre|center)\b", re.I),
        re.compile(r"\b(?:japanese|jdm) (?:car |vehicle )?imports?\b" + _GAP + r"\b(?:specialist|specialists|dealer|dealers|sales)\b", re.I),
        re.compile(r"\b(?:specialist|specialists)\b (?:in|of) (?:japanese|jdm) imports?\b", re.I),
    ],
}

OTHER_LABELS = [
    (re.compile(r"japanese|jdm", re.I), "日本からの輸入車"),
    (re.compile(r"wheelchair|\bwavs?\b", re.I), "福祉車両（車椅子対応車）"),
    (re.compile(r"motorhome|campervan|camper van", re.I), "キャンピングカー"),
    (re.compile(r"commercial|\bvans?\b", re.I), "商用車・バン"),
]

GENERAL_PATTERNS = [
    re.compile(r"\bused (?:car|cars|vehicle|vehicles) (?:dealer|dealers|dealership|sales|supermarket|centre|center|specialists?)\b", re.I),
    re.compile(r"\bquality used (?:cars|vehicles)\b", re.I),
    re.compile(r"\bused cars? for sale\b", re.I),
]

USED_PATTERNS = [
    re.compile(r"\bused (?:car|cars|vehicle|vehicles|stock)\b", re.I),
    re.compile(r"\bpre[- ]?owned\b", re.I),
    re.compile(r"\bsecond[- ]hand (?:car|cars|vehicles?)\b", re.I),
    re.compile(r"\bapproved used\b", re.I),
    re.compile(r"\b(?:classic|sports|prestige|performance) cars? for sale\b", re.I),
    re.compile(r"\b(?:cars|vehicles) for sale\b", re.I),
    re.compile(r"\bnearly new\b", re.I),
    re.compile(r"\b(?:our|current|view|browse) (?:stock|showroom|cars|vehicles)\b", re.I),
]
NEW_ONLY_HINT = re.compile(r"\bnew (?:car|cars) (?:offers|deals|range)\b", re.I)
PRICE_RE = re.compile(r"£\s?\d{1,3}(?:,\d{3})+|£\s?\d{4,6}\b")

SPORTS_MODELS = re.compile(
    r"\b(?:porsche (?:911|boxster|cayman|718)|ferrari|lamborghini|mclaren|lotus (?:elise|exige|evora|emira)|aston martin|"
    r"mazda mx-?5|nissan (?:gt-?r|370z|350z)|bmw (?:m2|m3|m4|m5|m8|z4)|audi (?:r8|rs\s?\d|tt rs)|toyota (?:gr86|gt86|supra)|"
    r"jaguar f-?type|mercedes(?:-benz)?(?:-amg)? (?:amg gt|sl\b|slk)|corvette|caterham|ariel atom|ford mustang|honda (?:s2000|nsx|civic type r))",
    re.I,
)
SUV_MODELS = re.compile(
    r"\b(?:range rover|land rover (?:discovery|defender|freelander)|discovery sport|volvo xc(?:40|60|90)|kia (?:sorento|sportage)|"
    r"toyota (?:rav4|land cruiser|alphard|vellfire|estima)|nissan (?:qashqai|x-?trail|juke)|hyundai (?:tucson|santa fe)|"
    r"ford (?:galaxy|s-?max|kuga)|vw (?:touran|sharan|tiguan)|volkswagen (?:touran|sharan|tiguan|touareg)|citroen (?:berlingo|c4 picasso|grand c4)|"
    r"bmw x[1-7]|audi q[2-8]|mercedes(?:-benz)? (?:glc|gle|gls|v-class)|skoda kodiaq|seat alhambra|mitsubishi (?:outlander|shogun|l200)|"
    r"jeep|honda cr-?v|mazda cx-?\d|peugeot (?:3008|5008)|dacia duster)\b",
    re.I,
)
CLASSIC_YEAR = re.compile(r"\b19[2-8]\d\b\s+(?:[A-Z][A-Za-z-]+)")

FEATURES = [
    (re.compile(r"\bpart[- ]exchange", re.I), "下取り（Part Exchange）"),
    (re.compile(r"\bfinance\b", re.I), "ファイナンス"),
    (re.compile(r"\bwarrant(?:y|ies)\b", re.I), "保証"),
    (re.compile(r"\b(?:nationwide delivery|deliver(?:y|ies)? nationwide|uk[- ]wide delivery|delivery across the uk)\b", re.I), "全国配送"),
    (re.compile(r"\b(?:worldwide shipping|export(?:ing)? worldwide|international shipping|we export)\b", re.I), "海外発送・輸出"),
    (re.compile(r"\bfamily[- ](?:run|owned)\b", re.I), "家族経営"),
    (re.compile(r"\bmotor ombudsman\b", re.I), "The Motor Ombudsman の記載"),
    (re.compile(r"\brac approved\b", re.I), "RAC Approved の記載"),
    (re.compile(r"\baa (?:dealer promise|approved|cars)\b", re.I), "AA の認定等の記載"),
]
ESTABLISHED = re.compile(
    r"\b(?:established|est\.|founded|trading since|in business since|family business since|family[- ](?:run|owned) (?:business )?since|dealing (?:in [a-z ]+ )?since|"
    r"serving [a-z ]{0,40}since|business since)\s+(?:in\s+)?((?:19|20)\d{2})\b", re.I)

IRELAND_HINT = re.compile(r"(?<!northern )\b(?:republic of ireland|co\. (?:dublin|cork|galway|kerry|limerick|meath|kildare|wicklow|louth|wexford|mayo|donegal)|eircode)\b|\+353", re.I)


# ---------------------------------------------------------------------------
# メールの判定
# ---------------------------------------------------------------------------
FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.co.uk", "live.com",
    "yahoo.com", "yahoo.co.uk", "btinternet.com", "btconnect.com", "sky.com", "aol.com", "icloud.com",
    "me.com", "virginmedia.com", "talktalk.net", "ntlworld.com", "msn.com", "mail.com", "protonmail.com",
}
EXCLUDE_LOCAL = [
    (re.compile(r"^(?:jobs?|careers?|recruit(?:ment|ing)?|vacanc(?:y|ies)|hr|cv|apply)\b", re.I), "求人・採用用のアドレス"),
    (re.compile(r"^(?:privacy|dpo|gdpr|data[._-]?protection|dataprotection|data[._-]?controller)\b", re.I), "個人情報・プライバシー窓口"),
    (re.compile(r"^(?:no[._-]?reply|do[._-]?not[._-]?reply|mailer-daemon|postmaster|abuse|webmaster|hostmaster|bounce)", re.I), "自動送信・技術窓口"),
]
DEPARTMENT_LOCAL = [
    (re.compile(r"^(?:accounts?|invoices?|billing|payroll|finance)\b", re.I), "経理・ファイナンス"),
    (re.compile(r"^(?:service|servicing|parts|workshop|bodyshop|mot|repairs?|aftersales)\b", re.I), "整備・部品"),
    (re.compile(r"^(?:complaints?)\b", re.I), "苦情窓口"),
    (re.compile(r"^(?:press|media|marketing)\b", re.I), "広報"),
]
GENERAL_LOCAL = re.compile(
    r"^(?:info|sales|enquir(?:y|ies)|inquir(?:y|ies)|hello|contact|office|admin|mail|team|reception|cars|showroom|used|usedcars|general|help|customer\w*)\b",
    re.I,
)
AGENCY_CONTEXT = re.compile(
    r"\b(?:website|site|web site|web design|designed|developed|built|created|powered|hosted|marketing)\s+(?:by|and hosted by)\b|\bdigital agency\b|\bweb design\b",
    re.I,
)
CONTEXT_EXCLUDE = [
    (re.compile(r"\b(?:careers?|vacanc(?:y|ies)|recruitment|job opportunities|apply now|send your cv)\b", re.I), "求人の案内の近くに記載"),
    (re.compile(r"\b(?:privacy policy|data protection officer|subject access request|gdpr)\b", re.I), "個人情報の案内の近くに記載"),
]

EMAIL_STATUS_OK = "公開メール確認済み"
EMAIL_STATUS_REVIEW = "要手動確認"
EMAIL_STATUS_EXCLUDED = "除外"
EMAIL_STATUS_FAILED = "取得失敗"


@dataclass
class EmailJudgement:
    purpose: str
    status: str
    reason: str = ""


def same_site(email_domain: str, site_host: str) -> bool:
    email_domain = email_domain.lower()
    site = site_host.lower()
    site = site[4:] if site.startswith("www.") else site
    return email_domain == site or email_domain.endswith("." + site) or site.endswith("." + email_domain)


def judge_email(email: str, site_host: str, context: str = "", in_footer: bool = False) -> EmailJudgement:
    local, _, domain = email.lower().partition("@")
    for pattern, reason in EXCLUDE_LOCAL:
        if pattern.search(local):
            return EmailJudgement("除外候補", EMAIL_STATUS_EXCLUDED, reason)
    own = same_site(domain, site_host)
    ctx = context or ""
    if not own and AGENCY_CONTEXT.search(ctx):
        return EmailJudgement("除外候補", EMAIL_STATUS_EXCLUDED, "制作会社・運営会社等の連絡先の可能性（「Website by」等の近くに記載）")
    for pattern, reason in CONTEXT_EXCLUDE:
        m = pattern.search(ctx)
        if m and not GENERAL_LOCAL.search(local):
            return EmailJudgement("除外候補", EMAIL_STATUS_EXCLUDED, reason)
    for pattern, label in DEPARTMENT_LOCAL:
        if pattern.search(local):
            status = EMAIL_STATUS_OK if (own or domain in FREE_MAIL) else EMAIL_STATUS_REVIEW
            return EmailJudgement(f"部門別（{label}）", status, "" if status == EMAIL_STATUS_OK else "販売店サイトと異なるドメイン")
    if own:
        purpose = "一般・営業窓口候補" if GENERAL_LOCAL.search(local) else "個人・担当者宛ての可能性"
        return EmailJudgement(purpose, EMAIL_STATUS_OK)
    if domain in FREE_MAIL:
        return EmailJudgement("一般・営業窓口候補（無料メール）", EMAIL_STATUS_OK, "無料メールのアドレスを公式サイトで表示")
    return EmailJudgement("要確認", EMAIL_STATUS_REVIEW, "販売店サイトと異なるドメインのため、販売店の窓口か要確認")


# ---------------------------------------------------------------------------
# 分類
# ---------------------------------------------------------------------------
@dataclass
class PageText:
    url: str
    role: str        # home / about / contact / stock / other
    title: str
    headline: str    # title + meta + h1 + og:site_name
    text: str


@dataclass
class Classification:
    categories: list[str]
    basis: str
    scope: str
    evidence: list[tuple[str, str, str]] = field(default_factory=list)  # (topic, url, excerpt)
    other_label: str = ""


def classify_dealer(pages: list[PageText]) -> Classification:
    evidence: list[tuple[str, str, str]] = []
    official: dict[str, tuple[str, str]] = {}
    other_label = ""
    for page in pages:
        if page.role not in ("home", "about", "contact", "stock"):
            continue
        sources = [page.headline, page.text[:6000]]
        for cat, patterns in SPECIALIST_PATTERNS.items():
            if cat in official:
                continue
            for src in sources:
                for pat in patterns:
                    m = pat.search(src)
                    if m:
                        official[cat] = (page.url, excerpt_around(src, m.group(0)))
                        if cat == CAT_OTHER:
                            for lp, label in OTHER_LABELS:
                                if lp.search(m.group(0)):
                                    other_label = label
                                    break
                        break
                if cat in official:
                    break

    stock_pages = [p for p in pages if p.role == "stock"]
    stock_text = " ".join(p.text[:60000] for p in stock_pages)
    prices = len(PRICE_RE.findall(stock_text))
    sports = len(SPORTS_MODELS.findall(stock_text))
    suv = len(SUV_MODELS.findall(stock_text))
    classic = len(CLASSIC_YEAR.findall(stock_text))
    scope_parts = []
    if stock_pages:
        scope_parts.append(f"在庫・販売車両のページ {len(stock_pages)} 件（価格表示 {prices} 件）を確認。全在庫は未確認")
    else:
        scope_parts.append("在庫ページは確認できていません")

    trend: dict[str, str] = {}
    if prices >= 5:
        for cat, count in ((CAT_SPORTS, sports), (CAT_SUV, suv), (CAT_CLASSIC, classic)):
            if count >= max(5, int(prices * 0.6)):
                trend[cat] = f"確認した在庫ページで価格表示 {prices} 件に対し{cat}に該当する車名・年式の記載 {count} 件"

    cats: list[str] = []
    basis = BASIS_MANUAL
    if official:
        for cat in (CAT_CLASSIC, CAT_SPORTS, CAT_SUV, CAT_OTHER):
            if cat in official:
                cats.append(cat)
                url, ex = official[cat]
                evidence.append(("分類", url, ex))
        basis = BASIS_OFFICIAL
        for cat, note in trend.items():
            if cat not in cats:
                cats.append(cat)
                evidence.append(("分類", stock_pages[0].url, note))
    elif trend:
        for cat, note in trend.items():
            cats.append(cat)
            evidence.append(("分類", stock_pages[0].url, note))
        basis = BASIS_TREND
    else:
        general_hit = None
        for page in pages:
            for pat in GENERAL_PATTERNS:
                m = pat.search(page.headline) or pat.search(page.text[:6000])
                if m:
                    general_hit = (page.url, excerpt_around(page.headline + " " + page.text[:6000], m.group(0)))
                    break
            if general_hit:
                break
        if general_hit:
            cats.append(CAT_GENERAL)
            evidence.append(("分類", general_hit[0], general_hit[1]))
            basis = BASIS_TREND if prices >= 5 else BASIS_MANUAL
            if prices >= 5:
                evidence.append(("分類", stock_pages[0].url, f"確認した在庫ページで特定分野への偏りなし（価格表示 {prices} 件）"))
        elif prices >= 5:
            cats.append(CAT_GENERAL)
            basis = BASIS_TREND
            evidence.append(("分類", stock_pages[0].url, f"確認した在庫ページで特定分野への偏りなし（価格表示 {prices} 件）"))
    if not cats:
        cats = [CAT_REVIEW]
        basis = BASIS_MANUAL
    return Classification(cats, basis, "／".join(scope_parts), evidence, other_label)


def detect_used_sales(pages: list[PageText]) -> tuple[str, tuple[str, str] | None]:
    for page in pages:
        for src in (page.headline, page.text[:20000]):
            for pat in USED_PATTERNS:
                m = pat.search(src)
                if m:
                    return "中古車販売を確認", (page.url, excerpt_around(src, m.group(0)))
    for page in pages:
        m = NEW_ONLY_HINT.search(page.text[:20000])
        if m:
            return "新車中心の可能性・要確認", (page.url, excerpt_around(page.text, m.group(0)))
    return "未確認", None


def detect_uk(postcodes: list[tuple[str, str, str]], phones: list[str], all_text: str, host: str) -> tuple[str, str]:
    """戻り値 (状態, 根拠の短い説明)。postcodes は (郵便番号, 地方, ページURL)。"""
    from .extract import postcode_region

    uk_codes = [p for p in postcodes if (postcode_region(p[0]) or ("", "", ""))[2] not in ("", "王室属領（英国外・要確認）")]
    if host.endswith(".ie") or IRELAND_HINT.search(all_text[:200000]):
        if uk_codes:
            return "英国の住所あり・英国外の記載もあり要確認", "英国の郵便番号とアイルランド等の記載の両方を検出"
        return "英国外（アイルランド等）の可能性", "アイルランドの地名・電話番号・ドメイン等を検出"
    if uk_codes:
        return "英国内の住所を確認", f"郵便番号 {uk_codes[0][0]}"
    if postcodes:
        return "王室属領の住所（英国外）・要確認", f"郵便番号 {postcodes[0][0]}"
    if any(re.sub(r"[\s()-]", "", p).startswith(("+44", "0")) for p in phones):
        return "英国の電話番号のみ確認（住所は未確認）", phones[0]
    return "未確認", ""


def detect_features(pages: list[PageText]) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    labels = set()
    for page in pages:
        text = page.headline + " " + page.text[:30000]
        for pat, label in FEATURES:
            if label in labels:
                continue
            m = pat.search(text)
            if m:
                labels.add(label)
                found.append((label, page.url, excerpt_around(text, m.group(0))))
        m = ESTABLISHED.search(text)
        if m and not any(l.endswith("年創業の記載") for l in labels):
            year = int(m.group(1))
            if 1890 <= year <= 2026:
                label = f"{year}年創業の記載"
                labels.add(label)
                found.append((label, page.url, excerpt_around(text, m.group(0))))
    return found


# ---------------------------------------------------------------------------
# 日本語概要（1〜3文。確認できた事実だけで組み立てる）
# ---------------------------------------------------------------------------
def build_summary(region: str | None, postcode: str | None, used_sales: str, cls: Classification,
                  features: list[str]) -> str:
    sentences = []
    if region:
        place = f"{region}（郵便番号 {postcode}）" if postcode else region
        if used_sales == "中古車販売を確認":
            sentences.append(f"{place}の中古車販売店です。")
        else:
            sentences.append(f"{place}の自動車販売事業者で、中古車販売は{used_sales}です。")
    else:
        if used_sales == "中古車販売を確認":
            sentences.append("中古車販売を確認した事業者です（所在地は公式サイトから未確認）。")
        else:
            sentences.append(f"所在地は未確認で、中古車販売は{used_sales}です。")

    cats = [c for c in cls.categories if c != CAT_REVIEW]
    label = "・".join(cats)
    if cls.other_label and CAT_OTHER in cats:
        label = label.replace(CAT_OTHER, f"その他（{cls.other_label}）")
    if cls.basis == BASIS_OFFICIAL and cats:
        sentences.append(f"公式サイトに{label}の専門・販売の記載があります。")
    elif cls.basis == BASIS_TREND and cats:
        if cats == [CAT_GENERAL]:
            sentences.append("確認した在庫ページの範囲では、特定分野に偏らない車両を掲載しています（全在庫は未確認）。")
        else:
            sentences.append(f"確認した在庫ページの範囲では{label}の掲載が多く見られます（全在庫は未確認）。")
    else:
        sentences.append("取扱分野は公式サイトの記載だけでは判断できず、要確認です。")

    if features:
        sentences.append("公式サイトに " + "・".join(features[:5]) + " に関する記載があります。")
    return "".join(sentences[:3])


GENERIC_NAMES = re.compile(r"^(?:home|homepage|welcome|index|sales|contact|contact us|about|about us|used cars?|cars|"
                           r"car sales|showroom|stock|our stock|search|menu)$", re.I)


def _usable_name(name: str) -> bool:
    name = clean(name)
    return (2 < len(name) <= 60 and not GENERIC_NAMES.match(name) and name.count(",") < 2
            and not re.search(r"\b(?:for sale|second hand|cheap)\b", name, re.I))


def pick_name(jsonld: list[dict], site_name: str, title: str, host: str) -> str:
    """店名の自動候補（人が確認して確定する前提）。見つからなければドメイン名。"""
    for obj in jsonld:
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x in ("AutoDealer", "AutomotiveBusiness", "LocalBusiness", "Organization", "Store") for x in types):
            name = clean(str(obj.get("name", "")))
            if _usable_name(name):
                return name
    if site_name and _usable_name(site_name):
        return clean(site_name)
    if title:
        parts = [clean(p) for p in re.split(r"\s[|\-–—:•]\s|\s\|\s?", title) if clean(p)]
        generic = re.compile(r"^(?:home|homepage|welcome|used cars?.*|.*for sale.*|.*\bin [A-Z][a-z]+.*)$", re.I)
        for part in (parts[::-1] if len(parts) > 1 else parts):
            if not generic.match(part) and _usable_name(part):
                return part
    return host
