"""画面・共有モード認証・安全な表示の試験（架空データ）。"""
import re

import pytest
from fastapi.testclient import TestClient

from sdr import auth, repo
from sdr.db import connect
from sdr.web import create_app
from conftest import FakeWeb, dealer_site
from test_data import run_research

BASE = "https://www.test-motors.example"


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def local_client(paths):
    app = create_app(paths, mode="local", start_jobs=False, load_seed=False)
    with TestClient(app) as c:
        yield c


def seed_dealer(paths, malicious=False):
    conn = connect(paths.db)
    try:
        did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト")
        web = FakeWeb(dealer_site())
        if malicious:
            web.pages[BASE + "/"] = (200, '<html><title>Evil <script>alert(1)</script> Cars</title>'
                                          '<h1>Used cars for sale "><img src=x onerror=alert(1)></h1>'
                                          '<a href="javascript:alert(1)">Contact</a></html>')
        run_research(conn, did, web)
        return did
    finally:
        conn.close()


def test_list_filter_detail_and_csv(paths, local_client):
    did = seed_dealer(paths)
    r = local_client.get("/")
    assert r.status_code == 200 and "Test Motors Ltd" in r.text and "sales@test-motors.example" in r.text
    assert "hello@pixelagency.example" not in r.text  # 制作会社のアドレスは一覧に出さない
    assert "Test Motors Ltd" in local_client.get("/?has_email=1&has_form=1&region=ヨークシャー").text
    assert "Test Motors Ltd" not in local_client.get("/?category=クラシックカー").text
    d = local_client.get(f"/dealers/{did}")
    assert d.status_code == 200 and "出典を開く" in d.text and did in d.text and "制作会社" in d.text
    csv = local_client.get("/export/dealers.csv?has_email=1")
    assert csv.status_code == 200 and csv.content.startswith(b"\xef\xbb\xbf") and did.encode() in csv.content
    assert local_client.get("/export/contacts.csv").status_code == 200
    assert local_client.get("/export/evidence.csv").status_code == 200


def test_post_requires_csrf_and_manual_edit_saved(paths, local_client):
    did = seed_dealer(paths)
    r = local_client.post(f"/dealers/{did}/edit", data={"name": "X"})
    assert r.status_code == 403
    token = csrf_of(local_client.get(f"/dealers/{did}").text)
    r = local_client.post(f"/dealers/{did}/edit", data={"csrf_token": token, "name": "人が確定した名前",
                                                        "categories": ["一般中古車"], "notes": "メモ"},
                          follow_redirects=False)
    assert r.status_code == 303
    page = local_client.get(f"/dealers/{did}").text
    assert "人が確定した名前" in page and "人が確定" in page
    r = local_client.post(f"/dealers/{did}/edit", data={"csrf_token": token, "name": "x"},
                          headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_external_text_is_escaped(paths, local_client):
    did = seed_dealer(paths, malicious=True)
    page = local_client.get(f"/dealers/{did}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page or "alert(1)" not in page
    assert 'href="javascript:' not in page
    assert "<img src=x" not in page
    assert local_client.get("/").headers["content-security-policy"].startswith("default-src 'self'")


def test_host_header_checked(paths):
    app = create_app(paths, mode="local", allowed_hosts={"127.0.0.1:8765"}, start_jobs=False, load_seed=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert c.get("/").status_code == 200
        assert c.get("/", headers={"host": "attacker.example"}).status_code == 400


def test_add_urls_rejects_internal_and_aggregators(paths, local_client):
    token = csrf_of(local_client.get("/add").text)
    r = local_client.post("/add/urls", data={"csrf_token": token,
                                             "urls": "http://127.0.0.1/\nhttps://www.autotrader.co.uk/x\nhttps://new-dealer.example/"})
    assert r.status_code == 200
    assert "内部" in r.text and "掲載サイト" in r.text and "登録しました" in r.text


# --------------------------------------------------------------------------- 共有モード
@pytest.fixture
def shared_client(paths):
    conn = connect(paths.db)
    try:
        auth.create_user(conn, "alice", "correct horse battery")
    finally:
        conn.close()
    app = create_app(paths, mode="shared", secure_cookies=True, start_jobs=False, load_seed=False)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_shared_requires_login(paths, shared_client):
    seed_dealer(paths)
    r = shared_client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert shared_client.get("/export/dealers.csv").status_code == 401
    assert shared_client.get("/api/jobs/JOB-x").status_code == 401
    assert shared_client.post("/research/start", data={"scope": "unresearched"}).status_code == 401
    assert shared_client.get("/healthz").status_code == 200


def test_shared_login_logout_and_cookie_flags(paths, shared_client):
    r = shared_client.post("/login", data={"username": "alice", "password": "wrong password!"}, follow_redirects=False)
    assert "/login" in r.headers["location"]
    r = shared_client.post("/login", data={"username": "alice", "password": "correct horse battery"}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    page = shared_client.get("/")
    assert page.status_code == 200 and "alice でログイン中" in page.text
    token = csrf_of(page.text)
    assert shared_client.post("/shutdown", data={"csrf_token": token}).status_code == 403
    shared_client.post("/logout", data={"csrf_token": token})
    assert shared_client.get("/", follow_redirects=False).status_code == 303


def test_login_lockout(paths, shared_client):
    for _ in range(5):
        shared_client.post("/login", data={"username": "alice", "password": "nope-nope-nope"})
    r = shared_client.post("/login", data={"username": "alice", "password": "correct horse battery"}, follow_redirects=False)
    assert "15" in r.headers["location"] or "%E5%BE%85" in r.headers["location"]


def test_password_storage_and_policy(conn):
    h = auth.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert auth.verify_password("correct horse battery", h) and not auth.verify_password("x", h)
    with pytest.raises(ValueError):
        auth.create_user(conn, "bob", "short")
