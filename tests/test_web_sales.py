"""画面を通した営業機能の試験（テスト用接続先のみ使用。外部へは送らない）。

一連の流れ：調査データ → 英文作成（テンプレート） → 承認 → 予約 → 送信記録 → 疑似返信 → 残りの予約が止まる
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sdr import auth
from sdr.db import connect
from sdr.tz import iso, utcnow
from sdr.web import create_app
from test_web import csrf_of, seed_dealer


@pytest.fixture
def client(paths):
    app = create_app(paths, mode="local", start_jobs=False, load_seed=False)
    app.state.port = 8765
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.app_ref = app
        yield c


def post(c, url, data=None, files=None, follow=False):
    token = csrf_of(c.get("/").text)
    return c.post(url, data={"csrf_token": token, **(data or {})}, files=files, follow_redirects=follow)


def q(paths, sql, *p):
    conn = connect(paths.db)
    try:
        r = conn.execute(sql, p).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def test_dashboard_and_screens_render(client):
    for url in ["/", "/dealers", "/deals", "/drafts", "/schedules", "/inbox", "/suppressions", "/reconcile", "/profile",
                "/templates", "/settings/mail", "/accounts/new?provider=test", "/accounts/new?provider=smtp_imap",
                "/accounts/new?provider=gmail", "/accounts/new?provider=graph", "/testbox", "/users", "/audit"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "ダッシュボード" in client.get("/").text and 'id="stat-dealers"' in client.get("/").text
    assert "停止" in client.get("/settings/mail").text


def test_post_requires_csrf(client):
    r = client.post("/settings/send-mode", data={"send_mode": "テスト"}, follow_redirects=False)
    assert r.status_code == 403


def test_full_flow_research_to_reply_stops_followups(client, paths):
    did = seed_dealer(paths)  # 架空サイトを調査した結果（公開メール sales@test-motors.example）
    post(client, "/profile", {"company_name": "Samurai Auto Test Ltd", "sender_name": "Taro Test", "sender_title": "Export Manager",
                              "intro_en": "We are a test exporter.", "vehicles_en": "Japanese used cars",
                              "services_en": "sourcing and export paperwork", "signature_en": "Taro Test"})
    r = post(client, "/accounts/save", {"provider": "test", "label": "テスト", "from_address": "me@samurai.example",
                                        "from_name": "Taro", "daily_limit": "30", "simulate": "正常"})
    assert r.status_code == 303
    acc = q(paths, "SELECT id FROM mail_accounts")
    assert "テスト" in post(client, "/settings/send-mode", {"send_mode": "テスト"}, follow=True).text
    r = post(client, "/deals/new", {"dealer_id": did, "title": "初回営業"})
    deal = r.headers["location"].split("?")[0].rsplit("/", 1)[1]
    tomorrow = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    data = {"account_id": acc, "to_address": "sales@test-motors.example", "method": "template", "stop_on_reply": "1"}
    for i, s in enumerate(["initial", "followup1", "followup2"]):
        data.update({f"use_{s}": "1", f"{s}_date": tomorrow, f"{s}_time": f"1{i}:00", f"{s}_tz": "Europe/London"})
    r = post(client, f"/deals/{deal}/sequence", data, follow=True)
    assert "3 回分" in r.text, r.text[:2000]
    assert q(paths, "SELECT COUNT(*) FROM schedules WHERE status='承認待ち'") == 3
    body = q(paths, "SELECT body FROM drafts WHERE step='initial'")
    assert "Test Motors" in body and "{{" not in body and "unsubscribe" in body
    # 予約だけでは送らない（承認待ち）→ 各文面を承認
    conn = connect(paths.db)
    drafts = [r[0] for r in conn.execute("SELECT id FROM drafts ORDER BY created_at")]
    conn.close()
    for d in drafts:
        _t = post(client, f"/drafts/{d}/approve", follow=True).text
        assert "承認しました" in _t, re.findall(r"flash[^>]*>[^<]*", _t)
    assert q(paths, "SELECT COUNT(*) FROM schedules WHERE status='予約済み'") == 3
    # 初回の時刻が来た状態にして予約処理
    conn = connect(paths.db)
    conn.execute("UPDATE schedules SET send_at_utc=? WHERE step='initial'", (iso(utcnow() - timedelta(minutes=1)),))
    conn.close()
    post(client, "/scheduler/run", {"next": "/schedules"})
    assert q(paths, "SELECT status FROM schedules WHERE step='initial'") == "送信済み"
    assert q(paths, "SELECT result FROM send_attempts") == "受付済み"
    assert q(paths, "SELECT COUNT(*) FROM messages WHERE direction='out'") == 1
    # 疑似返信 → 受信確認 → 追客が止まる
    sent_id = q(paths, "SELECT id FROM test_mailbox WHERE folder='sent'")
    assert "入れました" in post(client, "/testbox/inject", {"sent_id": str(sent_id), "kind": "reply"}, follow=True).text
    post(client, "/sync/now", {"next": "/inbox"})
    assert q(paths, "SELECT kind FROM messages WHERE direction='in'") == "返信"
    assert q(paths, "SELECT COUNT(*) FROM schedules WHERE step IN ('followup1','followup2') AND status='取消'") == 2
    assert q(paths, "SELECT stage FROM deals WHERE id=?", deal) == "返信あり・要対応"
    inbox = client.get("/inbox").text
    assert "返信" in inbox and "Test Motors" in inbox
    # 返信の下書きは作れるが自動では送らない
    mid = q(paths, "SELECT id FROM messages WHERE direction='in'")
    r = post(client, f"/messages/{mid}/reply-draft", {"method": "template"}, follow=True)
    assert "返信の下書き" in r.text
    assert q(paths, "SELECT COUNT(*) FROM send_attempts") == 1


def test_unsubscribe_link_flow(client, paths):
    did = seed_dealer(paths)
    post(client, "/profile", {"company_name": "S", "sender_name": "T", "sender_title": "M", "intro_en": "I", "vehicles_en": "V",
                              "services_en": "S", "signature_en": "T"})
    post(client, "/accounts/save", {"provider": "test", "label": "t", "from_address": "me@samurai.example", "daily_limit": "5"})
    assert "https://" in post(client, "/settings/options", {"public_base_url": "http://insecure.example"}, follow=True).text
    post(client, "/settings/options", {"public_base_url": "https://dealers.samurai.example"})
    deal = post(client, "/deals/new", {"dealer_id": did}).headers["location"].split("?")[0].rsplit("/", 1)[1]
    acc = q(paths, "SELECT id FROM mail_accounts")
    post(client, f"/deals/{deal}/drafts/new", {"method": "template", "step": "initial", "account_id": acc,
                                               "to_address": "sales@test-motors.example"})
    body = q(paths, "SELECT body FROM drafts")
    token = re.search(r"https://dealers\.samurai\.example/u/(\S+)", body).group(1)
    assert client.get(f"/u/{token}").status_code == 200
    r = client.post(f"/u/{token}")
    assert "unsubscribed" in r.text
    assert q(paths, "SELECT scope || status || source FROM suppressions") == "アドレス確定停止リンク"
    assert "not valid" in client.post("/u/forged-token").text


@pytest.fixture
def shared(paths):
    conn = connect(paths.db)
    auth.create_user(conn, "boss", "boss-password-123")            # 最初の利用者は管理者
    auth.create_user(conn, "staff", "staff-password-123", "member")
    conn.close()
    app = create_app(paths, mode="shared", secure_cookies=False, start_jobs=False, load_seed=False)
    with TestClient(app) as c:
        yield c


def login(c, user, pw):
    r = c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303


def test_member_cannot_change_admin_settings(shared, paths):
    login(shared, "staff", "staff-password-123")
    token = csrf_of(shared.get("/").text)
    assert shared.post("/settings/send-mode", data={"csrf_token": token, "send_mode": "テスト"}, follow_redirects=False).status_code == 403
    assert shared.get("/accounts/new?provider=test").status_code == 403
    assert shared.get("/users").status_code == 403
    assert shared.get("/deals").status_code == 200  # 担当者は営業画面を使える


def test_admin_can_manage_users_and_unauthenticated_is_blocked(shared, paths):
    assert shared.get("/deals", follow_redirects=False).status_code == 303   # 未ログインは画面を見られない
    assert shared.post("/sync/now", follow_redirects=False).status_code == 401
    login(shared, "boss", "boss-password-123")
    token = csrf_of(shared.get("/").text)
    r = shared.post("/users/create", data={"csrf_token": token, "username": "friend", "role": "member",
                                           "password": "friend-password-1", "password2": "friend-password-1"}, follow_redirects=True)
    assert "作成" in r.text
    assert q(paths, "SELECT role FROM users WHERE username='friend'") == "member"
    assert q(paths, "SELECT role FROM users WHERE username='boss'") == "admin"


def test_firecrawl_key_goes_to_secret_store_not_db(client, paths):
    post(client, "/settings/firecrawl-key", {"api_key": "fc-test-key"})
    assert client.app_ref.state.secrets.get("firecrawl_api_key") == "fc-test-key"
    conn = connect(paths.db)
    try:
        assert "fc-test-key" not in "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert "設定済み" in client.get("/settings").text
    post(client, "/settings/firecrawl-key", {"clear": "1"})
    assert client.app_ref.state.secrets.get("firecrawl_api_key") is None


def test_ai_draft_via_screen_and_failure_is_recorded(client, paths, monkeypatch):
    """AI の呼び出しは書き込みトランザクションの外で行い、失敗しても利用記録が残る（巻き戻されない）。"""
    import anthropic
    import httpx2

    from sdr import ai, runtime
    from test_connectors import FakeClient

    did = seed_dealer(paths)
    post(client, "/profile", {"company_name": "S", "sender_name": "T", "sender_title": "M", "intro_en": "I", "vehicles_en": "V",
                              "services_en": "S", "signature_en": "T"})
    post(client, "/accounts/save", {"provider": "test", "label": "t", "from_address": "me@samurai.example", "daily_limit": "5"})
    acc = q(paths, "SELECT id FROM mail_accounts")
    deal = post(client, "/deals/new", {"dealer_id": did}).headers["location"].split("?")[0].rsplit("/", 1)[1]
    conn = connect(paths.db)
    try:
        runtime.set_option(conn, "ai_enabled", "1")
    finally:
        conn.close()
    client.app_ref.state.secrets.set(ai.SECRET_KEY, "sk-test")
    fake = FakeClient({"subject": "Japanese cars for Test Motors", "body_en": "Hello Test Motors team,\n\nBody.\n\nTaro",
                       "summary_ja": "日本の中古車の紹介です", "used_facts": ["dealer_name"]})
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    form = {"method": "ai", "step": "initial", "account_id": acc, "to_address": "sales@test-motors.example", "instruction": "丁寧に"}
    r = post(client, f"/deals/{deal}/drafts/new", form, follow=True)
    assert "下書きを作成しました" in r.text
    assert q(paths, "SELECT source || '|' || summary_ja FROM drafts") == "AI|日本の中古車の紹介です"
    assert q(paths, "SELECT COUNT(*) FROM ai_usage WHERE ok=1") == 1
    # 失敗（429）：下書きは増えず、失敗の記録は残る
    resp = httpx2.Response(429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    fake.error = anthropic.RateLimitError("rate limited", response=resp, body=None)
    r = post(client, f"/deals/{deal}/drafts/new", form, follow=True)
    assert "利用制限" in r.text
    assert q(paths, "SELECT COUNT(*) FROM drafts") == 1
    assert q(paths, "SELECT COUNT(*) FROM ai_usage WHERE ok=0") == 1
