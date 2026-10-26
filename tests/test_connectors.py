"""実サービス接続処理の試験（通信は偽の応答に差し替え。外部へは送らない）。

Gmail API・Microsoft Graph・SMTP/IMAP・OAuth・Claude API の「成功・送信前の失敗・結果不明・認証切れ・同期位置の失効」を確認する。
実際のアカウントでの動作は利用者の接続情報が必要なため、ここでは確認していない（IMPLEMENTATION_STATUS.md 参照）。
"""
from __future__ import annotations

import base64
import json
import smtplib
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from sdr import ai, runtime
from sdr.mail import AuthError, SendRejected, SendUnknown
from sdr.mail import oauth
from sdr.mail.base import OutgoingMessage, new_message_id, parse_raw
from sdr.mail.gmail import GmailProvider
from sdr.mail.graph import GraphProvider
from sdr.mail.smtp_imap import SmtpImapProvider
from sdr.secretstore import SecretStore


class MemSecrets:
    def __init__(self, **kw):
        self.d = dict(kw)

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def has(self, k):
        return bool(self.d.get(k))


def msg(**kw):
    base = dict(from_addr="me@samurai.example", from_name="Taro", to=["sales@test-motors.example"], cc=[], bcc=[],
                subject="Hello", body_text="Body", message_id=new_message_id("me@samurai.example"))
    base.update(kw)
    return OutgoingMessage(**base)


def token_ok(request):
    return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})


@pytest.fixture(autouse=True)
def clear_tokens():
    oauth.TokenCache._tokens.clear()
    yield
    oauth.TokenCache._tokens.clear()


# ---------------------------------------------------------------- Gmail
def gmail(handler, **cfg):
    acc = {"id": "ACC-G", "provider": "gmail", "from_address": "me@samurai.example", "config": {"client_id": "cid", **cfg}}
    return GmailProvider(acc, MemSecrets(**{"ACC-G:refresh_token": "RT", "ACC-G:client_secret": "cs"}),
                         http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_gmail_send_success_and_raw_format():
    seen = {}

    def h(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            form = parse_qs(req.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["RT"]
            return token_ok(req)
        assert req.headers["authorization"] == "Bearer AT"
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "g1", "threadId": "t1"})

    m = msg(bcc=["hidden@owner.example"])
    r = gmail(h).send(m)
    assert r.provider_message_id == "g1" and r.provider_thread_id == "t1"
    raw = base64.urlsafe_b64decode(seen["body"]["raw"])
    parsed = parse_raw(raw, "x")
    assert parsed.message_id == m.message_id and b"hidden@owner.example" not in raw


def test_gmail_errors_are_classified():
    def make(status):
        return lambda req: token_ok(req) if req.url.host == "oauth2.googleapis.com" else httpx.Response(status, text="err")
    with pytest.raises(SendRejected):
        gmail(make(400)).send(msg())
    with pytest.raises(SendUnknown):
        gmail(make(503)).send(msg())

    def timeout(req):
        if req.url.host == "oauth2.googleapis.com":
            return token_ok(req)
        raise httpx.ReadTimeout("timed out", request=req)
    with pytest.raises(SendUnknown):
        gmail(timeout).send(msg())


def test_gmail_expired_refresh_token_is_auth_error():
    def h(req):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(AuthError):
        gmail(h).send(msg())


def test_gmail_history_404_triggers_resync():
    raw = base64.urlsafe_b64encode(b"From: a@dealer.example\r\nTo: me@samurai.example\r\nSubject: Re: Hello\r\n"
                                   b"Message-ID: <r1@dealer.example>\r\n\r\nYes please").decode()
    calls = []

    def h(req):
        if req.url.host == "oauth2.googleapis.com":
            return token_ok(req)
        calls.append(req.url.path)
        if req.url.path.endswith("/history"):
            return httpx.Response(404, json={"error": {"code": 404}})
        if req.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "me@samurai.example", "historyId": "999"})
        if req.url.path.endswith("/messages"):
            assert "newer_than" in req.url.params["q"]
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if req.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json={"id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "raw": raw})
        return httpx.Response(500)

    msgs, cursor = gmail(h).fetch_new("123")
    assert cursor == "999" and len(msgs) == 1 and msgs[0].message_id == "<r1@dealer.example>"
    assert msgs[0].external_thread_id == "t1"


def test_gmail_find_sent():
    def h(req):
        if req.url.host == "oauth2.googleapis.com":
            return token_ok(req)
        assert "rfc822msgid:" in req.url.params["q"] and "in:sent" in req.url.params["q"]
        return httpx.Response(200, json={"messages": [{"id": "x"}]} if "found" in req.url.params["q"] else {})
    p = gmail(h)
    assert p.find_sent("<found@x>", None) is True
    assert p.find_sent("<none@x>", None) is False


# ---------------------------------------------------------------- Microsoft Graph
def graph(handler):
    acc = {"id": "ACC-M", "provider": "graph", "from_address": "me@samurai.example", "config": {"client_id": "cid", "tenant": "consumers"}}
    return GraphProvider(acc, MemSecrets(**{"ACC-M:refresh_token": "RT"}), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_graph_send_uses_draft_then_send_and_records_ids():
    prepared = []

    def h(req):
        if req.url.host == "login.microsoftonline.com":
            assert "/consumers/" in req.url.path
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600, "refresh_token": "RT2"})
        assert 'IdType="ImmutableId"' in req.headers["prefer"]
        if req.method == "POST" and req.url.path.endswith("/me/messages"):
            body = json.loads(req.content)
            assert body["bccRecipients"] == [{"emailAddress": {"address": "hidden@owner.example"}}]
            return httpx.Response(201, json={"id": "D1", "internetMessageId": "<ms1@outlook>", "conversationId": "C1"})
        if req.url.path.endswith("/messages/D1/send"):
            return httpx.Response(202)
        return httpx.Response(500)

    p = graph(h)
    r = p.send(msg(bcc=["hidden@owner.example"]), on_prepared=lambda pid, mid: prepared.append((pid, mid)))
    assert r.message_id_header == "<ms1@outlook>" and r.provider_message_id == "D1" and r.provider_thread_id == "C1"
    assert prepared == [("D1", "<ms1@outlook>")]
    assert p.secrets.get("ACC-M:refresh_token") == "RT2"  # 入れ替わった更新トークンを保存


def test_graph_send_failures():
    def make(status):
        def h(req):
            if req.url.host == "login.microsoftonline.com":
                return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
            if req.url.path.endswith("/me/messages"):
                return httpx.Response(201, json={"id": "D1", "internetMessageId": "<m@x>"})
            return httpx.Response(status, text="x")
        return h
    with pytest.raises(SendUnknown):
        graph(make(503)).send(msg())
    with pytest.raises(SendRejected):
        graph(make(400)).send(msg())


def test_graph_delta_410_resyncs_and_find_sent():
    state = {"n": 0}

    def h(req):
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if "deltatoken=old" in str(req.url):
            return httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})
        if req.url.path.endswith("/messages/delta"):
            return httpx.Response(200, json={"value": [{"id": "I1", "conversationId": "C1", "internetMessageId": "<in@d>",
                                                        "subject": "Re: Hello", "from": {"emailAddress": {"address": "A@Dealer.example"}},
                                                        "toRecipients": [], "ccRecipients": [], "receivedDateTime": "2026-10-26T00:00:00Z",
                                                        "body": {"contentType": "text", "content": "Yes"}, "isDraft": False}],
                                             "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=new"})
        if req.url.path.endswith("/messages/I1"):
            return httpx.Response(200, json={"internetMessageHeaders": [{"name": "In-Reply-To", "value": "<orig@me>"}]})
        if req.url.path.endswith("/messages/D1"):
            return httpx.Response(200, json={"isDraft": False, "parentFolderId": "SENT"})
        if req.url.path.endswith("/mailFolders/sentitems"):
            return httpx.Response(200, json={"id": "SENT"})
        return httpx.Response(404)

    p = graph(h)
    msgs, cursor = p.fetch_new("https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=old")
    assert cursor.endswith("deltatoken=new")
    assert msgs[0].in_reply_to == "<orig@me>" and msgs[0].from_addr == "a@dealer.example"
    assert p.find_sent(None, "D1") is True


# ---------------------------------------------------------------- SMTP / IMAP
class FakeSMTP:
    def __init__(self, fail_rcpt=False, drop_on_data=False, auth_fail=False):
        self.fail_rcpt, self.drop_on_data, self.auth_fail = fail_rcpt, drop_on_data, auth_fail
        self.rcpts, self.sent_data = [], None

    def login(self, u, p):
        if self.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def mail(self, f):
        return 250, b"ok"

    def rcpt(self, r):
        self.rcpts.append(r)
        return (550, b"no such user") if self.fail_rcpt else (250, b"ok")

    def rset(self):
        pass

    def data(self, d):
        if self.drop_on_data:
            raise smtplib.SMTPServerDisconnected("connection lost")
        self.sent_data = d
        return 250, b"queued"

    def quit(self):
        pass


def smtp_provider(fake, **cfg):
    acc = {"id": "ACC-S", "provider": "smtp_imap", "from_address": "me@samurai.example",
           "config": {"smtp_host": "smtp.test", "save_to_sent": False, **cfg}}
    return SmtpImapProvider(acc, MemSecrets(**{"ACC-S:password": "pw"}), smtp_factory=lambda *a: fake)


def test_smtp_success_rejected_unknown_auth():
    fake = FakeSMTP()
    m = msg(cc=["c@test-motors.example"], bcc=["hidden@owner.example"])
    smtp_provider(fake).send(m)
    assert fake.rcpts == ["sales@test-motors.example", "c@test-motors.example", "hidden@owner.example"]
    assert b"hidden@owner.example" not in fake.sent_data
    with pytest.raises(SendRejected):
        smtp_provider(FakeSMTP(fail_rcpt=True)).send(msg())
    with pytest.raises(SendUnknown):
        smtp_provider(FakeSMTP(drop_on_data=True)).send(msg())
    with pytest.raises(AuthError):
        smtp_provider(FakeSMTP(auth_fail=True)).send(msg())


def test_smtp_missing_password_is_auth_error():
    acc = {"id": "ACC-S", "provider": "smtp_imap", "from_address": "me@samurai.example", "config": {"smtp_host": "smtp.test"}}
    with pytest.raises(AuthError):
        SmtpImapProvider(acc, MemSecrets(), smtp_factory=lambda *a: FakeSMTP()).send(msg())


class FakeIMAP:
    def __init__(self, uidvalidity="7", uids=(5, 6)):
        self.uidvalidity, self.uids = uidvalidity, list(uids)

    def login(self, u, p):
        pass

    def select(self, folder, readonly=False):
        return "OK", [b"2"]

    def response(self, code):
        return code, [self.uidvalidity.encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [" ".join(str(u) for u in self.uids).encode()]
        uid = args[0]
        raw = (f"From: a@dealer.example\r\nTo: me@samurai.example\r\nSubject: Re: Hi\r\nMessage-ID: <u{uid}@d>\r\n\r\nbody").encode()
        return "OK", [(b"1 (BODY[] {10}", raw), b")"]

    def logout(self):
        pass


def test_imap_cursor_and_uidvalidity():
    acc = {"id": "ACC-S", "provider": "smtp_imap", "from_address": "me@samurai.example", "config": {"imap_host": "imap.test"}}
    p = SmtpImapProvider(acc, MemSecrets(**{"ACC-S:password": "pw"}), imap_factory=lambda h, port: FakeIMAP())
    msgs, cursor = p.fetch_new(None)
    assert [m.message_id for m in msgs] == ["<u5@d>", "<u6@d>"] and cursor == "7:6"
    msgs, cursor = p.fetch_new("7:6")  # 続きから（新着なし：UID 6 以下は返さない）
    assert msgs == [] and cursor == "7:6"
    p.imap_factory = lambda h, port: FakeIMAP(uidvalidity="8", uids=(1,))
    msgs, cursor = p.fetch_new("7:6")  # UIDVALIDITY が変わったら期間を区切って取り直す
    assert len(msgs) == 1 and cursor == "8:1"


# ---------------------------------------------------------------- OAuth
def test_oauth_pkce_url_and_exchange():
    acc = {"id": "ACC-G", "provider": "gmail", "from_address": "me@samurai.example", "config": {"client_id": "cid"}}
    pending = oauth.PendingAuth()
    state, challenge = pending.add("ACC-G")
    url = oauth.authorization_url(acc, 8765, state, challenge)
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://127.0.0.1:8765/oauth/callback"]
    assert "gmail.send" in q["scope"][0] and q["access_type"] == ["offline"]
    aid, verifier = pending.pop(state)
    assert aid == "ACC-G" and pending.pop(state) is None  # 1 回だけ使える
    secrets = MemSecrets(**{"ACC-G:client_secret": "cs"})

    def h(req):
        form = parse_qs(req.content.decode())
        assert form["code_verifier"] == [verifier] and form["code"] == ["CODE"]
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
    oauth.exchange_code(acc, secrets, "CODE", verifier, 8765, http=httpx.Client(transport=httpx.MockTransport(h)))
    assert secrets.get("ACC-G:refresh_token") == "RT"
    graph_acc = {"id": "ACC-M", "provider": "graph", "from_address": "me@x.example", "config": {"client_id": "c"}}
    assert oauth.redirect_uri(graph_acc, 8765) == "http://localhost:8765/oauth/callback"


# ---------------------------------------------------------------- Claude API（偽のクライアント）
class FakeClient:
    def __init__(self, payload=None, error=None, stop_reason="end_turn"):
        self.payload, self.error, self.stop_reason, self.calls = payload, error, stop_reason, []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, model=kw["model"],
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
                               usage=SimpleNamespace(input_tokens=120, output_tokens=80))


def ai_ready(conn, secrets, cap="10"):
    runtime.set_option(conn, "ai_enabled", "1")
    runtime.set_option(conn, "ai_monthly_request_cap", cap)
    secrets.set(ai.SECRET_KEY, "sk-test")


def test_ai_generate_records_usage_and_respects_caps(paths, conn):
    from sdr import repo, sales
    secrets = MemSecrets()
    did, _, _ = repo.create_dealer(conn, "https://www.test-motors.example/", "テスト", name="Test Motors Ltd")
    deal = sales.get_deal(conn, sales.create_deal(conn, did, "t", "u", "u"))
    with pytest.raises(ai.AIError, match="無効"):
        ai.generate_draft(conn, secrets, deal, "initial", "", None, client=FakeClient({}))
    ai_ready(conn, secrets, cap="1")
    fc = FakeClient({"subject": "Hi", "body_en": "Hello [[price]]", "summary_ja": "挨拶です", "used_facts": ["name"]})
    r = ai.generate_draft(conn, secrets, deal, "initial", "丁寧に", None, client=fc)
    assert r["subject"] == "Hi" and r["summary_ja"] == "挨拶です"
    call = fc.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "untrusted data" in call["system"] and "<website_excerpts>" in call["messages"][0]["content"]
    row = conn.execute("SELECT input_tokens, output_tokens, ok FROM ai_usage").fetchone()
    assert tuple(row) == (120, 80, 1)
    with pytest.raises(ai.AIError, match="上限"):
        ai.generate_draft(conn, secrets, deal, "initial", "", None, client=FakeClient({}))
    assert conn.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 1  # 上限では呼び出さない


def test_ai_errors_are_recorded(paths, conn):
    import anthropic
    import httpx2
    secrets = MemSecrets()
    ai_ready(conn, secrets)
    resp = httpx2.Response(429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.RateLimitError("rate limited", response=resp, body=None)
    with pytest.raises(ai.AIError, match="利用制限"):
        ai.summarize_ja(conn, secrets, "hello", client=FakeClient(error=err))
    with pytest.raises(ai.AIError, match="断りました"):
        ai.summarize_ja(conn, secrets, "hello", client=FakeClient({"summary_ja": ""}, stop_reason="refusal"))
    rows = [tuple(r) for r in conn.execute("SELECT ok, error FROM ai_usage ORDER BY id")]
    assert rows[0][0] == 0 and "429" in rows[0][1] and rows[1][0] == 0


# ---------------------------------------------------------------- 秘密情報
def test_secret_store_file_fallback_not_in_db(paths):
    s = SecretStore(paths.secrets, use_keyring=False)
    s.set("ACC-1:password", "very-secret-pw")
    assert s.get("ACC-1:password") == "very-secret-pw"
    assert b"very-secret-pw" not in paths.db.read_bytes()
    s.delete_prefix("ACC-1")
    assert s.get("ACC-1:password") is None


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows の資格情報マネージャーは Windows でのみ確認できる")
def test_secret_store_uses_windows_credential_manager(paths):
    s = SecretStore(paths.secrets)
    assert "Windows" in s.backend_name
    s.set("ACC-WIN:password", "win-secret-value")
    try:
        assert s.get("ACC-WIN:password") == "win-secret-value"
        assert not paths.secrets.exists() or "win-secret-value" not in paths.secrets.read_text(encoding="utf-8")
    finally:
        s.delete("ACC-WIN:password")
    assert s.get("ACC-WIN:password") is None
