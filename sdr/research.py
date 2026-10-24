"""1 販売店の公式サイト調査（候補発見とは別の処理）。

取得 → 抽出 → ルール分類・概要 → ResearchResult を返す。保存は repo.apply_research が行う。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from . import classify as C
from .extract import PageInfo, clean, excerpt_around, parse_page, postcode_region
from .fetcher import Fetcher
from .netsafe import host_key

ROLE_PATTERNS = [
    ("contact", re.compile(r"contact|get-in-touch|getintouch|enquir|inquir|find-us|findus|visit-us|location|directions", re.I), 100),
    ("about", re.compile(r"about|who-we-are|our-story|history|meet-the-team|our-team|why-buy|why-choose", re.I), 80),
    ("stock", re.compile(r"used-cars|usedcars|used-vehicles|our-cars|our-stock|stock|showroom|inventory|vehicles|cars-for-sale|for-sale|search-cars|current-stock|classic-cars|sports-cars|4x4|our-vehicles|cars$|/cars/?", re.I), 60),
]
ANCHOR_HINTS = [
    ("contact", re.compile(r"\b(?:contact|get in touch|enquire|enquiries|find us|visit us)\b", re.I)),
    ("about", re.compile(r"\b(?:about|who we are|our story|meet the team|why buy)\b", re.I)),
    ("stock", re.compile(r"\b(?:used cars|our stock|stock|showroom|our cars|vehicles for sale|cars for sale|view all|browse|current stock|inventory)\b", re.I)),
]
SKIP_LINK = re.compile(
    r"privacy|cookie|terms|legal|careers|jobs|vacanc|blog|news|login|account|basket|cart|wp-admin|wp-login|feed|\.pdf$|\.jpe?g$|\.png$|"
    r"facebook|twitter|instagram|youtube|tel:|mailto:|sitemap|finance-calculator|valuation|sell-your-car|part-exchange-valuation",
    re.I,
)
MAX_PER_ROLE = {"contact": 3, "about": 2, "stock": 3, "other": 0}


@dataclass
class PageRecord:
    url: str
    role: str
    status: str          # 取得 / 失敗 / キャッシュ
    error: str = ""


@dataclass
class ContactFinding:
    kind: str            # email / form / contact_page / phone
    value: str
    purpose: str
    status: str
    source_url: str
    excerpt: str
    reason: str = ""


@dataclass
class ResearchResult:
    ok: bool
    status: str                      # 完了 / 一部取得失敗 / 取得失敗 / 停止
    failure_reason: str = ""
    pages: list[PageRecord] = field(default_factory=list)
    name: str | None = None
    region: str | None = None
    address: str | None = None
    postcode: str | None = None
    categories: list[str] = field(default_factory=list)
    category_basis: str | None = None
    category_scope: str | None = None
    summary: str | None = None
    used_sales: str | None = None
    uk_check: str | None = None
    contacts: list[ContactFinding] = field(default_factory=list)
    evidence: list[tuple[str, str, str]] = field(default_factory=list)  # (topic, url, excerpt)


def same_site(url: str, site: str) -> bool:
    return host_key(url) == host_key(site)


def choose_links(info: PageInfo, site_url: str, limit: int) -> list[tuple[str, str]]:
    scored: dict[str, tuple[int, str]] = {}
    for href, anchor in info.links:
        if not href.startswith(("http://", "https://")) or not same_site(href, site_url):
            continue
        path = urlsplit(href).path.lower()
        if SKIP_LINK.search(href) or path in ("", "/"):
            continue
        role, score = "other", 0
        for r, pat, s in ROLE_PATTERNS:
            if pat.search(path):
                role, score = r, s
                break
        if role == "other":
            for r, pat in ANCHOR_HINTS:
                if pat.search(anchor):
                    role, score = r, {"contact": 95, "about": 75, "stock": 55}[r]
                    break
        if role == "other":
            continue
        # 個別車両の詳細ページより一覧ページを優先（パスが短いほど高評価）
        score -= path.count("/") * 3 + (10 if re.search(r"\d{4,}", path) else 0)
        key = href.rstrip("/")
        if key not in scored or scored[key][0] < score:
            scored[key] = (score, role)
    ordered = sorted(scored.items(), key=lambda kv: -kv[1][0])
    picked: list[tuple[str, str]] = []
    per_role: dict[str, int] = {}
    for href, (_, role) in ordered:
        if per_role.get(role, 0) >= MAX_PER_ROLE.get(role, 0):
            continue
        per_role[role] = per_role.get(role, 0) + 1
        picked.append((href, role))
        if len(picked) >= limit:
            break
    return picked


def research_site(site_url: str, fetcher: Fetcher, max_pages: int = 10,
                  stop: Callable[[], bool] | None = None) -> ResearchResult:
    stop = stop or (lambda: False)
    result = ResearchResult(ok=False, status="取得失敗")
    home = fetcher.fetch(site_url)
    result.pages.append(PageRecord(site_url, "home", "キャッシュ" if home.from_cache else ("取得" if home.ok else "失敗"), home.error))
    if not home.ok:
        result.failure_reason = f"トップページを取得できませんでした: {home.error or 'HTTP ' + str(home.status_code)}"
        return result
    base = home.final_url or site_url
    infos: list[tuple[PageInfo, str]] = [(parse_page(home.text, base), "home")]
    for href, role in choose_links(infos[0][0], base, max(0, max_pages - 1)):
        if stop():
            result.status = "停止"
            result.failure_reason = "利用者の操作で停止しました（取得済みページまで保存）"
            break
        res = fetcher.fetch(href)
        if res.error.startswith("robots.txt で取得が許可されていません"):
            state = "robots.txtで対象外"  # 方針による除外で、取得失敗ではない
        else:
            state = "キャッシュ" if res.from_cache else ("取得" if res.ok else "失敗")
        result.pages.append(PageRecord(href, role, state, res.error))
        if res.ok and same_site(res.final_url or href, base):
            infos.append((parse_page(res.text, res.final_url or href), role))
    analyse(result, infos, base)
    failed = [p for p in result.pages if p.status == "失敗"]
    if result.status != "停止":
        result.status = "一部取得失敗" if failed else "完了"
        if failed:
            result.failure_reason = "一部のページを取得できませんでした: " + "; ".join(
                f"{p.url}（{p.error}）" for p in failed[:3])
    result.ok = True
    return result


UK_COUNTRY = {"gb", "uk", "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland"}


def _foreign_jsonld_address(jsonld: list[dict]) -> str | None:
    """構造化データに英国外の住所（国名・郵便番号の形式）があれば、その住所を返す。"""
    from .extract import UK_POSTCODE_RE
    for obj in jsonld:
        addr = obj.get("address")
        if isinstance(addr, list):
            addr = addr[0] if addr else None
        if not isinstance(addr, dict):
            continue
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name") or country.get("@id")
        country = clean(str(country or "")).lower()
        pc = clean(str(addr.get("postalCode") or "")).upper()
        text = clean(", ".join(str(addr.get(k)) for k in ("streetAddress", "addressLocality", "postalCode", "addressCountry") if addr.get(k)))
        if country and country not in UK_COUNTRY:
            return text
        uk_like = UK_POSTCODE_RE.fullmatch(pc) or re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", pc)  # 前半だけの表記も英国形式
        if pc and not uk_like:
            return text
    return None


def analyse(result: ResearchResult, infos: list[tuple[PageInfo, str]], site_url: str) -> None:
    site_host = host_key(site_url)
    home = infos[0][0]
    pages = [C.PageText(i.url, role, i.title, " ".join([i.title, i.site_name, i.meta_description, " ".join(i.h1)]), i.text)
             for i, role in infos]

    # 名称
    jsonld = [obj for i, _ in infos for obj in i.jsonld]
    result.name = C.pick_name(jsonld, home.site_name, home.title, site_host)
    result.evidence.append(("名称", home.url, (home.site_name or home.title)[:240]))

    # 住所・地域（構造化データ → 本文の郵便番号）
    address, postcode, addr_src = None, None, None
    for i, _ in infos:
        for obj in i.jsonld:
            addr = obj.get("address")
            if isinstance(addr, list):
                addr = addr[0] if addr else None
            if isinstance(addr, dict) and addr.get("postalCode"):
                parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
                address = clean(", ".join(str(p) for p in parts if p))
                postcode = clean(str(addr.get("postalCode"))).upper()
                addr_src = (i.url, f"構造化データ: {address}")
                break
        if address:
            break
    all_codes = []
    for i, role in infos:
        for pc, ctx in i.postcodes:
            all_codes.append((pc, ctx, i.url, role))
    if not address and all_codes:
        # 問い合わせ・会社紹介ページの郵便番号を優先
        all_codes.sort(key=lambda c: {"contact": 0, "about": 1, "home": 2}.get(c[3], 3))
        pc, ctx, url, _ = all_codes[0]
        postcode = pc
        tail = ctx.split(". ")[-1]
        address = clean(tail)[-160:]
        addr_src = (url, ctx[-240:])
    if postcode:
        pr = postcode_region(postcode)
        if pr:
            area, town, region = pr
            result.region = f"{region}・{town}エリア（郵便番号 {area}）"
        result.postcode = postcode
        result.address = address
        if addr_src:
            result.evidence.append(("所在地", addr_src[0], addr_src[1]))

    # 英国内の確認
    phones_all = [p for i, _ in infos for p, _ in i.phones]
    codes_for_uk = [(pc, "", url) for pc, _, url, _ in all_codes] or ([(postcode, "", "")] if postcode else [])
    all_text = " ".join(i.text[:50000] for i, _ in infos)
    result.uk_check, uk_note = C.detect_uk(codes_for_uk, phones_all, all_text, site_host)
    foreign = _foreign_jsonld_address(jsonld)
    if foreign:
        result.uk_check, uk_note = "英国外の住所の記載あり・要確認", f"構造化データの住所: {foreign}"
        if addr_src and addr_src[1].startswith("構造化データ"):
            result.region, result.postcode = None, None
    if uk_note:
        result.evidence.append(("英国所在", addr_src[0] if addr_src else home.url, uk_note))

    # 中古車販売
    result.used_sales, used_ev = C.detect_used_sales(pages)
    if used_ev:
        result.evidence.append(("中古車販売", used_ev[0], used_ev[1]))

    # 分類
    cls = C.classify_dealer(pages)
    result.categories = cls.categories
    result.category_basis = cls.basis
    result.category_scope = cls.scope
    result.evidence.extend(cls.evidence)

    # 特徴
    features = C.detect_features(pages)
    for label, url, ex in features:
        result.evidence.append(("特徴", url, f"{label}: {ex}"))
    region_only = None
    if result.postcode and postcode_region(result.postcode):
        _, _, region_only = postcode_region(result.postcode)
    result.summary = C.build_summary(region_only, result.postcode, result.used_sales, cls, [f[0] for f in features])

    # メール
    seen = set()
    for i, _ in infos:
        for e in i.emails:
            if e.email in seen:
                continue
            seen.add(e.email)
            j = C.judge_email(e.email, site_host, e.context, e.in_footer)
            result.contacts.append(ContactFinding("email", e.email, j.purpose, j.status, i.url,
                                                  f"{e.method}: {e.context[:200]}", j.reason))

    # フォーム・問い合わせページ（1社につき代表1件。問い合わせページ → トップ → 会社紹介 → 在庫の順に優先）
    role_rank = {"contact": 0, "home": 1, "about": 2, "stock": 3}
    form_pages = []
    contact_pages_seen = []
    for i, role in infos:
        contact_forms = [f for f in i.forms if f.kind == "問い合わせ"]
        if contact_forms:
            vehicle_specific = role == "stock" or bool(re.search(r"/(?:cars?|vehicles?|product|stock|used-cars?)/[^/]+|enquiry/\d+", i.url, re.I))
            form_pages.append((role_rank.get(role, 4) + (5 if vehicle_specific else 0), i, contact_forms[0], vehicle_specific))
        elif role == "contact":
            contact_pages_seen.append(i)
    if form_pages:
        form_pages.sort(key=lambda x: x[0])
        _, i, f, vehicle_specific = form_pages[0]
        note = f"{f.reason}（項目: {', '.join(n.split(' ')[0] for n in f.field_names[:6] if n)}）"
        if vehicle_specific:
            note = "車両ごとの問い合わせフォーム。" + note
        result.contacts.append(ContactFinding("form", i.url, "問い合わせフォーム", "フォーム確認済み", i.url, note))
    else:
        for i in contact_pages_seen[:1]:
            if i.embeds:
                note = "外部フォームの埋め込みを検出: " + "・".join(i.embeds) + "（中身は画面で未確認）"
                status = "要手動確認"
            else:
                kinds = "・".join(sorted({f.kind for f in i.forms})) if i.forms else "なし"
                note = f"問い合わせページはあるが送信フォームを確認できません（検出したフォーム: {kinds}）"
                status = "問い合わせページあり・フォーム未確認"
            result.contacts.append(ContactFinding("contact_page", i.url, "問い合わせページ", status, i.url, note))
        if not contact_pages_seen:
            for p in [p for p in result.pages if p.role == "contact" and p.status == "失敗"][:1]:
                result.contacts.append(ContactFinding("contact_page", p.url, "問い合わせページ", "要手動確認", p.url,
                                                      f"問い合わせページを取得できませんでした: {p.error}"))

    # 電話（tel: リンクのみ。最大2件）
    phones_seen = set()
    for i, role in infos:
        for num, anchor in i.phones:
            norm = re.sub(r"[^\d+]", "", num)
            if norm in phones_seen or len(phones_seen) >= 2:
                continue
            phones_seen.add(norm)
            result.contacts.append(ContactFinding("phone", num, "電話", "公開番号確認済み", i.url,
                                                  excerpt_around(i.text, num[-6:], 160) if num else anchor))
