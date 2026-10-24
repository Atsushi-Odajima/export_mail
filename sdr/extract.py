"""HTML から事実を抽出する（実行はせず、文字列として読むだけ）。

外部ページ内の文章はデータとして扱い、アプリへの命令として解釈しない。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

EXCERPT_LEN = 240

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})(?![\w-])")
UK_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b")
PHONE_RE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)(?:\d[\s-]?){9,10}\b")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico")
PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com", "example.co.uk", "domain.com", "email.com", "yourdomain.com", "yourdomain.co.uk",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "test.com", "mysite.com", "company.com",
}

# 掲載サイト（車両検索・ポータル）や SNS。販売店本体としては登録しない。
AGGREGATOR_DOMAINS = {
    "autotrader.co.uk", "carwow.co.uk", "cazoo.co.uk", "motors.co.uk", "gumtree.com", "ebay.co.uk", "ebay.com",
    "pistonheads.com", "carandclassic.com", "cargurus.co.uk", "heycar.com", "heycar.co.uk", "cinch.co.uk",
    "carsite.co.uk", "exchangeandmart.co.uk", "autoscout24.com", "donedeal.ie", "carzone.ie", "yell.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "tiktok.com",
    "google.com", "google.co.uk", "maps.google.com", "bing.com", "yelp.com", "yelp.co.uk", "tripadvisor.co.uk",
    "wikipedia.org", "find-and-update.company-information.service.gov.uk", "checkatrade.com", "trustpilot.com",
    "classicdriver.com", "carandclassic.co.uk", "collectingcars.com", "bringatrailer.com", "classiccarsforsale.co.uk",
    "carvana.co.uk", "motorpoint.co.uk", "arnoldclark.com", "carshop.co.uk", "theaa.com", "rac.co.uk",
    "bidtodrive.co.uk", "autotrader.com", "webuyanycar.com", "motorsnippet.com", "car-dealers.co.uk",
    "scoot.co.uk", "freeindex.co.uk", "thomsonlocal.com", "cylex-uk.co.uk", "192.com", "carsnip.com",
}
# 注：motorpoint・arnoldclark・carshop は販売事業者だが大規模チェーン。候補発見では除外し、
# 必要なら利用者が個別に追加する（README 参照）。


def registrable_suffix_match(host: str, domains: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def is_aggregator(url: str) -> bool:
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return registrable_suffix_match(host, AGGREGATOR_DOMAINS)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def excerpt_around(text: str, needle: str, width: int = EXCERPT_LEN) -> str:
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return text[:width]
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(needle) + width // 2)
    snippet = text[start:end]
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


@dataclass
class FoundEmail:
    email: str
    method: str          # mailto / 本文 / 保護表示の復号 / 構造化データ
    context: str
    in_footer: bool = False


@dataclass
class FoundForm:
    kind: str            # 問い合わせ / 検索 / ログイン / ニュースレター / 在庫検索 / 不明
    reason: str
    field_names: list[str] = field(default_factory=list)


@dataclass
class PageInfo:
    url: str
    title: str = ""
    site_name: str = ""
    meta_description: str = ""
    h1: list[str] = field(default_factory=list)
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    emails: list[FoundEmail] = field(default_factory=list)
    phones: list[tuple[str, str]] = field(default_factory=list)
    forms: list[FoundForm] = field(default_factory=list)
    embeds: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    postcodes: list[tuple[str, str]] = field(default_factory=list)
    footer_text: str = ""


EMBED_PROVIDERS = {
    "hsforms": "HubSpot フォーム", "hubspot": "HubSpot フォーム", "jotform": "Jotform", "typeform": "Typeform",
    "docs.google.com/forms": "Google フォーム", "forms.gle": "Google フォーム", "formstack": "Formstack",
    "cognitoforms": "Cognito Forms", "wufoo": "Wufoo", "123formbuilder": "123FormBuilder",
    "gravityforms": "Gravity Forms", "wpforms": "WPForms", "contact-form-7": "Contact Form 7",
    "ninja-forms": "Ninja Forms", "formspree": "Formspree", "zohopublic": "Zoho Forms", "forms.office.com": "Microsoft Forms",
}


def decode_cfemail(hexstr: str) -> str:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    key = data[0]
    return "".join(chr(b ^ key) for b in data[1:])


def valid_email(email: str) -> bool:
    email = email.strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email):
        return False
    if email.endswith(IMAGE_EXT):
        return False
    domain = email.split("@", 1)[1]
    if domain in PLACEHOLDER_EMAIL_DOMAINS or domain.endswith(".wixpress.com"):
        return False
    local = email.split("@", 1)[0]
    if re.fullmatch(r"[0-9a-f]{20,}", local):  # 追跡用の自動生成アドレス
        return False
    return True


def parse_page(html: str, url: str) -> PageInfo:
    soup = BeautifulSoup(html, "html.parser")
    info = PageInfo(url=url)

    # 構造化データ（JSON-LD）
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for obj in _iter_jsonld(data):
            if isinstance(obj, dict):
                info.jsonld.append(obj)

    # 埋め込みフォーム（スクリプト・iframe の参照先から判定）
    for tag in soup.find_all(["script", "iframe"]):
        src = (tag.get("src") or "") + " " + (tag.string or "" if tag.name == "script" else "")
        low = src.lower()
        for key, label in EMBED_PROVIDERS.items():
            if key in low and label not in info.embeds:
                info.embeds.append(label)
    for tag in soup.find_all(class_=re.compile(r"wpcf7|gform_wrapper|wpforms|nf-form|frm_forms", re.I)):
        label = "WordPress フォームプラグイン"
        if label not in info.embeds:
            info.embeds.append(label)

    # Cloudflare のメール保護表示（公開表示されているアドレスの復号）
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        email = decode_cfemail(tag.get("data-cfemail", ""))
        if valid_email(email):
            info.emails.append(FoundEmail(email.lower(), "保護表示の復号", clean(tag.parent.get_text(" "))[:EXCERPT_LEN]))
    for a in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection#", re.I)):
        email = decode_cfemail(a["href"].split("#", 1)[1])
        if valid_email(email):
            info.emails.append(FoundEmail(email.lower(), "保護表示の復号", clean(a.parent.get_text(" "))[:EXCERPT_LEN]))

    for bad in soup(["script", "style", "noscript", "template", "svg"]):
        bad.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    info.title = clean(soup.title.get_text()) if soup.title else ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    info.site_name = clean(og.get("content", "")) if og else ""
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    info.meta_description = clean(md.get("content", "")) if md else ""
    info.h1 = [clean(h.get_text(" ")) for h in soup.find_all("h1")][:5]

    footer = soup.find("footer")
    info.footer_text = clean(footer.get_text(" ")) if footer else ""

    # フォーム
    for form in soup.find_all("form"):
        info.forms.append(classify_form(form))

    # リンク・mailto・tel
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        anchor = clean(a.get_text(" "))[:120]
        low = href.lower()
        if low.startswith("mailto:"):
            addr = unquote(href[7:].split("?", 1)[0]).strip()
            for part in addr.split(","):
                part = part.strip().lower()
                if valid_email(part):
                    in_footer = bool(footer and a in footer.descendants)
                    ctx = clean(a.parent.get_text(" ")) if a.parent else anchor
                    info.emails.append(FoundEmail(part, "mailto", ctx[:EXCERPT_LEN], in_footer))
        elif low.startswith("tel:"):
            num = clean(unquote(href[4:]))
            if len(re.sub(r"\D", "", num)) >= 9:
                info.phones.append((num, anchor))
        elif low.startswith(("http://", "https://", "/")) or not re.match(r"^[a-z][a-z0-9+.-]*:", low):
            if low.startswith("#"):
                continue
            info.links.append((urljoin(url, href).split("#", 1)[0], anchor))

    text = clean(soup.get_text(" "))
    info.text = text[:200_000]

    # 本文中のメール（[at] 表記も表示どおりに読む）
    deob = re.sub(r"\s*[\[\(]\s*at\s*[\]\)]\s*", "@", text, flags=re.I)
    deob = re.sub(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", ".", deob, flags=re.I)
    known = {e.email for e in info.emails}
    for m in EMAIL_RE.finditer(deob):
        email = m.group(1).lower().strip(".")
        if email in known or not valid_email(email):
            continue
        known.add(email)
        ctx = deob[max(0, m.start() - 100): m.end() + 100]
        in_footer = bool(info.footer_text and email in info.footer_text.lower())
        info.emails.append(FoundEmail(email, "本文", ctx, in_footer))

    # 構造化データ内のメール
    for obj in info.jsonld:
        email = str(obj.get("email", "")).replace("mailto:", "").strip().lower()
        if email and valid_email(email) and email not in known:
            known.add(email)
            info.emails.append(FoundEmail(email, "構造化データ", f"{obj.get('@type', '')} {obj.get('name', '')}"))

    # 郵便番号
    seen = set()
    for m in UK_POSTCODE_RE.finditer(text):
        area = re.match(r"[A-Z]+", m.group(1)).group(0)
        if area not in POSTCODE_AREAS:
            continue
        pc = f"{m.group(1)} {m.group(2)}"
        if pc in seen:
            continue
        seen.add(pc)
        start = max(0, m.start() - 140)
        info.postcodes.append((pc, text[start: m.end()]))
    # 電話（本文）
    if not info.phones:
        for m in PHONE_RE.finditer(text[:50_000]):
            info.phones.append((clean(m.group(0)), ""))
            if len(info.phones) >= 3:
                break
    return info


def _iter_jsonld(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_jsonld(data["@graph"])
        yield data


# ---------------------------------------------------------------------------
# フォームの判定（問い合わせ / 検索 / ログイン / ニュースレター / 在庫検索）
# ---------------------------------------------------------------------------
SEARCH_NAMES = {"q", "s", "search", "query", "keyword", "keywords", "term", "searchterm"}
VEHICLE_FILTER_HINTS = ("make", "model", "price", "mileage", "fuel", "transmission", "body", "year", "minprice", "maxprice")
NEWSLETTER_HINTS = ("newsletter", "subscribe", "sign up", "signup", "mailing list", "mailchimp", "klaviyo")
MESSAGE_HINTS = ("message", "enquiry", "inquiry", "comment", "question", "details", "your-message", "msg")


def classify_form(form) -> FoundForm:
    inputs = form.find_all(["input", "textarea", "select"])
    names = []
    types = []
    for el in inputs:
        t = (el.get("type") or el.name).lower()
        if t in ("hidden", "submit", "button", "image", "reset"):
            continue
        types.append(t)
        names.append(" ".join(str(el.get(a, "")) for a in ("name", "id", "placeholder", "aria-label")).lower().strip())
    form_text = clean(form.get_text(" ")).lower()
    attrs = " ".join(str(form.get(a, "")) for a in ("action", "id", "class", "name", "role")).lower()
    joined = " ".join(names)
    method = (form.get("method") or "get").lower()

    if "password" in types:
        return FoundForm("ログイン", "パスワード欄があるためログイン用と判断", names)
    if form.get("role") == "search" or "search" in types or (
        len(types) <= 2 and any(n.split(" ")[0] in SEARCH_NAMES for n in names)
    ):
        return FoundForm("検索", "検索欄の項目名・役割を検出", names)
    has_textarea = "textarea" in types
    has_email = "email" in types or "email" in joined or "e-mail" in joined
    has_phone = "tel" in types or "phone" in joined or "telephone" in joined
    has_message = has_textarea or any(h in joined for h in MESSAGE_HINTS)
    if any(h in attrs or h in form_text for h in NEWSLETTER_HINTS) and not has_textarea and len(types) <= 3:
        return FoundForm("ニュースレター", "メール登録（ニュースレター）用の文言を検出", names)
    vehicle_hits = sum(1 for h in VEHICLE_FILTER_HINTS if h in joined)
    if vehicle_hits >= 2 and not has_textarea:
        return FoundForm("在庫検索", "メーカー・車種・価格などの絞り込み項目を検出", names)
    if has_message and (has_email or has_phone) and len(types) >= 2:
        return FoundForm("問い合わせ", "本文欄と連絡先欄（メール・電話）を持つ送信フォーム", names)
    if "search" in attrs and len(types) <= 3:
        return FoundForm("検索", "送信先・属性に search を含む", names)
    return FoundForm("不明", "問い合わせフォームと判断できる項目がそろっていない", names)


# ---------------------------------------------------------------------------
# 郵便番号エリア → (ポストタウン, 地方)
# ---------------------------------------------------------------------------
_LON, _SE, _SW, _E, _EM, _WM, _Y, _NW, _NE = (
    "ロンドン", "イングランド南東部", "イングランド南西部", "イングランド東部", "イースト・ミッドランズ",
    "ウェスト・ミッドランズ", "ヨークシャー", "イングランド北西部", "イングランド北東部",
)
_SC, _WA, _NI, _CD = "スコットランド", "ウェールズ", "北アイルランド", "王室属領（英国外・要確認）"

POSTCODE_AREAS: dict[str, tuple[str, str]] = {
    "AB": ("Aberdeen", _SC), "AL": ("St Albans", _E), "B": ("Birmingham", _WM), "BA": ("Bath", _SW),
    "BB": ("Blackburn", _NW), "BD": ("Bradford", _Y), "BH": ("Bournemouth", _SW), "BL": ("Bolton", _NW),
    "BN": ("Brighton", _SE), "BR": ("Bromley", _LON), "BS": ("Bristol", _SW), "BT": ("Belfast", _NI),
    "CA": ("Carlisle", _NW), "CB": ("Cambridge", _E), "CF": ("Cardiff", _WA), "CH": ("Chester", _NW),
    "CM": ("Chelmsford", _E), "CO": ("Colchester", _E), "CR": ("Croydon", _LON), "CT": ("Canterbury", _SE),
    "CV": ("Coventry", _WM), "CW": ("Crewe", _NW), "DA": ("Dartford", _SE), "DD": ("Dundee", _SC),
    "DE": ("Derby", _EM), "DG": ("Dumfries", _SC), "DH": ("Durham", _NE), "DL": ("Darlington", _NE),
    "DN": ("Doncaster", _Y), "DT": ("Dorchester", _SW), "DY": ("Dudley", _WM), "E": ("London E", _LON),
    "EC": ("London EC", _LON), "EH": ("Edinburgh", _SC), "EN": ("Enfield", _LON), "EX": ("Exeter", _SW),
    "FK": ("Falkirk", _SC), "FY": ("Blackpool", _NW), "G": ("Glasgow", _SC), "GL": ("Gloucester", _SW),
    "GU": ("Guildford", _SE), "GY": ("Guernsey", _CD), "HA": ("Harrow", _LON), "HD": ("Huddersfield", _Y),
    "HG": ("Harrogate", _Y), "HP": ("Hemel Hempstead", _SE), "HR": ("Hereford", _WM), "HS": ("Outer Hebrides", _SC),
    "HU": ("Hull", _Y), "HX": ("Halifax", _Y), "IG": ("Ilford", _LON), "IM": ("Isle of Man", _CD),
    "IP": ("Ipswich", _E), "IV": ("Inverness", _SC), "JE": ("Jersey", _CD), "KA": ("Kilmarnock", _SC),
    "KT": ("Kingston upon Thames", _SE), "KW": ("Kirkwall", _SC), "KY": ("Kirkcaldy", _SC), "L": ("Liverpool", _NW),
    "LA": ("Lancaster", _NW), "LD": ("Llandrindod Wells", _WA), "LE": ("Leicester", _EM), "LL": ("Llandudno", _WA),
    "LN": ("Lincoln", _EM), "LS": ("Leeds", _Y), "LU": ("Luton", _E), "M": ("Manchester", _NW),
    "ME": ("Rochester", _SE), "MK": ("Milton Keynes", _SE), "ML": ("Motherwell", _SC), "N": ("London N", _LON),
    "NE": ("Newcastle upon Tyne", _NE), "NG": ("Nottingham", _EM), "NN": ("Northampton", _EM), "NP": ("Newport", _WA),
    "NR": ("Norwich", _E), "NW": ("London NW", _LON), "OL": ("Oldham", _NW), "OX": ("Oxford", _SE),
    "PA": ("Paisley", _SC), "PE": ("Peterborough", _E), "PH": ("Perth", _SC), "PL": ("Plymouth", _SW),
    "PO": ("Portsmouth", _SE), "PR": ("Preston", _NW), "RG": ("Reading", _SE), "RH": ("Redhill", _SE),
    "RM": ("Romford", _LON), "S": ("Sheffield", _Y), "SA": ("Swansea", _WA), "SE": ("London SE", _LON),
    "SG": ("Stevenage", _E), "SK": ("Stockport", _NW), "SL": ("Slough", _SE), "SM": ("Sutton", _LON),
    "SN": ("Swindon", _SW), "SO": ("Southampton", _SE), "SP": ("Salisbury", _SW), "SR": ("Sunderland", _NE),
    "SS": ("Southend-on-Sea", _E), "ST": ("Stoke-on-Trent", _WM), "SW": ("London SW", _LON), "SY": ("Shrewsbury", _WM),
    "TA": ("Taunton", _SW), "TD": ("Galashiels", _SC), "TF": ("Telford", _WM), "TN": ("Tonbridge", _SE),
    "TQ": ("Torquay", _SW), "TR": ("Truro", _SW), "TS": ("Middlesbrough", _NE), "TW": ("Twickenham", _LON),
    "UB": ("Southall", _LON), "W": ("London W", _LON), "WA": ("Warrington", _NW), "WC": ("London WC", _LON),
    "WD": ("Watford", _E), "WF": ("Wakefield", _Y), "WN": ("Wigan", _NW), "WR": ("Worcester", _WM),
    "WS": ("Walsall", _WM), "WV": ("Wolverhampton", _WM), "YO": ("York", _Y), "ZE": ("Shetland", _SC),
}


def postcode_region(postcode: str) -> tuple[str, str, str] | None:
    """郵便番号 → (エリア, ポストタウン, 地方)。"""
    m = re.match(r"([A-Z]{1,2})\d", postcode.upper())
    if not m or m.group(1) not in POSTCODE_AREAS:
        return None
    town, region = POSTCODE_AREAS[m.group(1)]
    return m.group(1), town, region
