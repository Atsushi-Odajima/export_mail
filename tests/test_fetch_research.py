"""取得の安全性・調査処理の試験（架空データ）。"""
import pytest

from sdr.fetcher import FetchSettings, Fetcher, HostThrottle
from sdr.netsafe import UnsafeURLError, assert_safe_url, check_url_syntax
from sdr.research import research_site
from conftest import FakeWeb, dealer_site, make_fetcher

BASE = "https://www.test-motors.example"


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/",
    "http://example.com:22/", "http://router.local/", "http://192.168.1.1/", "http://metadata.google.internal/",
    "http://100.64.0.1/", "http://intranet/",
])
def test_internal_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url, resolve=False)


def test_public_url_allowed():
    assert check_url_syntax("https://www.test-motors.example/contact") == "www.test-motors.example"


def test_dns_resolution_to_private_address_rejected(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 0))])
    with pytest.raises(UnsafeURLError):
        assert_safe_url("https://rebind.example/", resolve=True)


def test_redirect_to_internal_address_rejected():
    web = FakeWeb({
        f"{BASE}/robots.txt": (404, ""),
        f"{BASE}/": (302, "", {"location": "http://127.0.0.1:8765/settings"}),
    })
    res = make_fetcher(web).fetch(BASE + "/")
    assert "転送先を拒否" in res.error
    assert not any("127.0.0.1" in r for r in web.requests)


def test_robots_disallow_respected_and_captcha_not_bypassed():
    web = FakeWeb(dealer_site())
    f = make_fetcher(web)
    res = f.fetch(BASE + "/private/secret")
    assert "robots.txt" in res.error
    assert BASE + "/private/secret" not in web.requests
    web.pages[BASE + "/blocked"] = (403, "<html>Just a moment...</html>", {"cf-mitigated": "challenge"})
    assert "CAPTCHA" in f.fetch(BASE + "/blocked").error


def test_size_limit_truncates():
    web = FakeWeb({f"{BASE}/robots.txt": (404, ""), f"{BASE}/": (200, "<html>" + "a" * 50000 + "</html>")})
    res = make_fetcher(web, max_bytes=1000).fetch(BASE + "/")
    assert res.truncated and len(res.text) <= 1000


def test_interval_between_requests_to_same_host():
    import time
    web = FakeWeb(dealer_site())
    settings = FetchSettings(min_interval_sec=0.3, timeout_sec=5, max_retries=0, cache_hours=0)
    f = Fetcher(settings, transport=web.transport(), throttle=HostThrottle(), resolve_dns=False)
    t0 = time.monotonic()
    f.fetch(BASE + "/")
    f.fetch(BASE + "/about-us")
    assert time.monotonic() - t0 >= 0.6  # robots.txt + 2 ページ = 3 回、間隔 0.3 秒 × 2 以上


def test_research_full_site():
    web = FakeWeb(dealer_site())
    r = research_site(BASE + "/", make_fetcher(web), max_pages=10)
    assert r.ok and r.status == "完了"
    assert r.name == "Test Motors Ltd"
    assert r.postcode == "LS1 4AP" and "ヨークシャー" in r.region
    assert r.uk_check == "英国内の住所を確認"
    assert r.used_sales == "中古車販売を確認"
    emails = {c.value: c for c in r.contacts if c.kind == "email"}
    assert emails["sales@test-motors.example"].status == "公開メール確認済み"
    assert emails["info@test-motors.example"].status == "公開メール確認済み"
    assert emails["hello@pixelagency.example"].status == "除外"
    assert emails["jobs@test-motors.example"].status == "除外"
    assert emails["privacy@test-motors.example"].status == "除外"
    forms = [c for c in r.contacts if c.kind == "form"]
    assert len(forms) == 1 and forms[0].value.endswith("/contact-us") and forms[0].status == "フォーム確認済み"
    # ホームの検索欄・ニュースレターは問い合わせフォームに数えない
    assert not any(c.kind == "form" and c.value.rstrip("/") == BASE for c in r.contacts)
    assert BASE + "/private/secret" not in web.requests
    assert any("1998年創業" in e[2] for e in r.evidence)
    assert "ヨークシャー（郵便番号 LS1 4AP）の中古車販売店です。" in r.summary


def test_research_failure_reason_recorded():
    web = FakeWeb({f"{BASE}/robots.txt": (404, ""), f"{BASE}/": (500, "error")})
    r = research_site(BASE + "/", make_fetcher(web))
    assert not r.ok and r.status == "取得失敗" and "HTTP 500" in r.failure_reason


def test_contact_page_without_form_is_not_confirmed():
    web = FakeWeb({
        f"{BASE}/robots.txt": (404, ""),
        f"{BASE}/": (200, '<html><title>X Cars</title><a href="/contact">Contact</a><p>Used cars for sale</p></html>'),
        f"{BASE}/contact": (200, '<html><h1>Contact</h1><p>Call us</p><div id="app"></div></html>'),
    })
    r = research_site(BASE + "/", make_fetcher(web))
    pages = [c for c in r.contacts if c.kind == "contact_page"]
    assert pages and pages[0].status == "問い合わせページあり・フォーム未確認"
    assert not [c for c in r.contacts if c.kind == "form"]


def test_stop_during_research_keeps_partial():
    web = FakeWeb(dealer_site())
    r = research_site(BASE + "/", make_fetcher(web), stop=lambda: True)
    assert r.status == "停止" and r.ok
