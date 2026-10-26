"""営業機能の試験（送信前確認・承認・二重送信防止・送信結果の区別・予約・受信の分類と反映・稼働環境・時刻）。

送信はすべてテスト用接続先（外部へ出さない）か、試験用の偽の接続処理で行う。宛先は .example の架空アドレスだけを使う。
"""
from __future__ import annotations

import shutil
import threading
from datetime import timedelta

import pytest

from sdr import compose, repo, runtime, sales
from sdr.config import Paths
from sdr.db import connect, migrate, utcnow
from sdr.inbound import SyncService, classify
from sdr.mail import InboundMessage, SendResult
from sdr.mail import testbox
from sdr.outbound import OutboundService
from sdr.scheduler import Scheduler
from sdr.secretstore import SecretStore
from sdr.tz import AmbiguousTimeError, TimeInputError, iso, local_to_utc, utcnow as now_dt

BASE = "https://www.test-motors.example"
TO = "sales@test-motors.example"


class Env:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.secrets = SecretStore(paths.secrets, use_keyring=False)
        self.out = OutboundService(paths.data_dir, paths.db, self.secrets)
        self.sync = SyncService(paths.data_dir, paths.db, self.secrets)
        self.sched = Scheduler(self.out, self.sync)

    def conn(self):
        return connect(self.paths.db)


def fill_profile(conn):
    sales.save_profile(conn, {"company_name": "Samurai Auto Test Ltd", "sender_name": "Taro Test", "sender_title": "Export Manager",
                              "intro_en": "We are a test exporter.", "vehicles_en": "Japanese used cars",
                              "services_en": "sourcing and export paperwork", "signature_en": "Taro Test\nSamurai Auto Test Ltd",
                              "company_website": "https://samurai.example"}, "tester")


@pytest.fixture
def env(paths):
    e = Env(paths)
    c = e.conn()
    try:
        did, _, _ = repo.create_dealer(c, BASE + "/", "テスト", name="Test Motors Ltd")
        repo.add_contact_manual(c, did, "email", TO, BASE + "/contact-us", "試験", None, None, "tester")
        c.execute("UPDATE dealers SET postcode_auto='LS1 4AP', categories_auto='[\"一般中古車\"]' WHERE id=?", (did,))
        fill_profile(c)
        compose.ensure_default_templates(c)
        e.dealer_id = did
        e.account_id = sales.save_account(c, None, "test", "テスト", "me@samurai.example", "Taro", {"simulate": "正常"}, "", 30, "tester")
        e.deal_id = sales.create_deal(c, did, "初回営業", "tester", "tester")
        runtime.set_send_mode(c, "テスト", "tester")
    finally:
        c.close()
    return e


def make_draft(e: Env, step="initial", approve=True, to=TO, body="Hello Test Motors team,\n\nThis is a test.\n\nTaro") -> str:
    c = e.conn()
    try:
        d = sales.create_draft(c, e.deal_id, step, {"account_id": e.account_id, "to_address": to, "subject": "Japanese used cars",
                                                    "body": body}, "手動", "tester")
        if approve:
            sales.approve_draft(c, d, "tester")
        return d
    finally:
        c.close()


def set_account(e: Env, **cfg):
    c = e.conn()
    try:
        a = sales.get_account(c, e.account_id)
        sales.save_account(c, e.account_id, "test", a["label"], a["from_address"], "", {**a["config"], **cfg}, "", 30, "tester")
    finally:
        c.close()


def schedule(e: Env, draft_id: str, minutes: int = 5, step="initial", seq=None) -> str:
    c = e.conn()
    try:
        return sales.create_schedule(c, e.deal_id, draft_id, e.account_id, step, iso(now_dt() + timedelta(minutes=minutes)),
                                     "Europe/London", "tester", seq)
    finally:
        c.close()


def make_due(e: Env, sid: str, minutes_ago: int = 1):
    c = e.conn()
    try:
        c.execute("UPDATE schedules SET send_at_utc=? WHERE id=?", (iso(now_dt() - timedelta(minutes=minutes_ago)), sid))
    finally:
        c.close()


def one(e: Env, sql: str, *p):
    c = e.conn()
    try:
        r = c.execute(sql, p).fetchone()
        return r[0] if r else None
    finally:
        c.close()


# ---------------------------------------------------------------- 文面・承認
def test_template_placeholders_block_approval_until_profile_filled(env):
    c = env.conn()
    try:
        sales.save_profile(c, {"intro_en": ""}, "tester")
        deal = sales.get_deal(c, env.deal_id)
        values = compose.variables(c, deal, None, "", compose.unsubscribe_text(None, None))
        t = sales.pick_template(c, "initial", ["一般中古車"])
        body = compose.render(t["body"], values)
        assert "Test Motors Ltd" in body and "{{intro}}" in body
        d = sales.create_draft(c, env.deal_id, "initial", {"account_id": env.account_id, "to_address": TO,
                                                           "subject": compose.render(t["subject"], values), "body": body}, "定型文", "t")
        with pytest.raises(sales.SalesError, match="差し込み"):
            sales.approve_draft(c, d, "tester")
        fill_profile(c)
        values = compose.variables(c, deal, None, "", compose.unsubscribe_text(None, None))
        sales.update_draft(c, d, {"body": compose.render(t["body"], values)}, "tester")
        sales.approve_draft(c, d, "tester")
        assert "reply with \"unsubscribe\"" in sales.get_draft(c, d)["body"]
    finally:
        c.close()


def test_edit_after_approval_revokes_and_blocks_send(env):
    d = make_draft(env)
    c = env.conn()
    try:
        sales.update_draft(c, d, {"to_address": "other@test-motors.example"}, "tester")
        assert sales.current_approval(c, sales.get_draft(c, d)) is None
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("承認" in p for p in r["problems"])
    assert one(env, "SELECT COUNT(*) FROM test_mailbox") == 0


def test_send_mode_stop_blocks(env):
    d = make_draft(env)
    c = env.conn()
    try:
        runtime.set_send_mode(c, "停止", "tester")
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("停止" in p for p in r["problems"])


def test_send_accepted_records_everything(env):
    d = make_draft(env)
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "accepted", r
    assert "配達・開封の確認ではありません" in r["message"]
    assert one(env, "SELECT result FROM send_attempts") == "受付済み"
    assert one(env, "SELECT COUNT(*) FROM test_mailbox WHERE folder='sent'") == 1
    assert one(env, "SELECT stage FROM deals WHERE id=?", env.deal_id) == "送信済み・返信待ち"
    assert one(env, "SELECT status FROM drafts WHERE id=?", d) == "送信済み"
    # 2 回目は送らない
    r2 = env.out.send_draft(d, "tester")
    assert r2["result"] == "blocked"
    assert one(env, "SELECT COUNT(*) FROM test_mailbox WHERE folder='sent'") == 1


def test_concurrent_schedule_execution_sends_once(env):
    d = make_draft(env)
    sid = schedule(env, d)
    make_due(env, sid)
    env.sync.sync_account(env.account_id)  # 予約処理は送信直前に受信を確認する
    results = []

    def run():
        results.append(env.out.send_draft(d, "予約処理", schedule_id=sid))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r["result"] == "accepted") == 1
    assert one(env, "SELECT COUNT(*) FROM test_mailbox WHERE folder='sent'") == 1
    assert one(env, "SELECT COUNT(*) FROM send_attempts") == 1


def test_suppressions_block(env):
    d = make_draft(env)
    c = env.conn()
    try:
        sales.add_suppression(c, "会社", "tester", status="確認待ち", dealer_id=env.dealer_id, reason="確認中")
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("会社単位" in p for p in r["problems"])
    c = env.conn()
    try:
        sid = c.execute("SELECT id FROM suppressions").fetchone()[0]
        sales.resolve_suppression(c, sid, "解除", "admin")
        sales.add_suppression(c, "無効な宛先", "tester", address=TO, source="不達通知")
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("無効な宛先" in p for p in r["problems"])


def test_rejected_and_unknown_are_distinguished(env):
    set_account(env, simulate="送信前エラー")
    d = make_draft(env)
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "rejected"
    assert one(env, "SELECT result FROM send_attempts") == "送信前に失敗"
    # 送信前の失敗は、原因を直せば同じ文面を送り直せる
    set_account(env, simulate="送信結果不明")
    c = env.conn()
    try:
        sales.approve_draft(c, d, "tester")
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "unknown"
    assert one(env, "SELECT COUNT(*) FROM send_attempts WHERE result='結果不明'") == 1
    # 結果不明の間は自動でも手動でも再送しない
    set_account(env, simulate="正常")
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("既に送信試行" in p for p in r["problems"])
    # 照合：テスト用送信箱に残っているので「受付確認」
    aid = one(env, "SELECT id FROM send_attempts WHERE result='結果不明'")
    rr = env.out.reconcile(aid, "tester")
    assert rr["result"] == "ok"
    assert one(env, "SELECT result FROM send_attempts WHERE id=?", aid) == "照合で受付確認"
    assert one(env, "SELECT COUNT(*) FROM messages WHERE direction='out'") == 1


def test_crash_while_sending_becomes_unknown(env):
    d = make_draft(env)
    sid = schedule(env, d)
    c = env.conn()
    try:
        c.execute("UPDATE schedules SET status='送信中' WHERE id=?", (sid,))
        c.execute("INSERT INTO send_attempts(idempotency_key, schedule_id, draft_id, draft_version, account_id, instance_id, result, started_at) "
                  "VALUES('x', ?, ?, 1, ?, 'ENV', '送信中', ?)", (sid, d, env.account_id, utcnow()))
    finally:
        c.close()
    assert env.out.recover_on_start() == 1
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "結果確認待ち"


def test_live_mode_requires_human_send_permission(env):
    d = make_draft(env)
    c = env.conn()
    try:
        runtime.set_send_mode(c, "実送信", "tester")
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("送信可" in p for p in r["problems"])
    c = env.conn()
    try:
        with pytest.raises(sales.SalesError, match="根拠"):
            sales.update_deal(c, env.deal_id, {"send_allowed": "1"}, "tester")
        sales.update_deal(c, env.deal_id, {"send_allowed": "1", "send_basis": "法人の公開営業窓口（試験）"}, "tester")
    finally:
        c.close()
    assert env.out.send_draft(d, "tester")["result"] == "accepted"


class FakeRealProvider:
    """実際のメールアカウントの代わり（外部へは出さない）。送った内容を記録する。"""
    sent: list = []

    def __init__(self, account):
        self.account = account

    def send(self, msg, on_prepared=None):
        FakeRealProvider.sent.append(msg)
        return SendResult(message_id_header=msg.message_id, provider_message_id="fake-1")


def test_test_mode_with_real_account_redirects_to_verification_address(env):
    c = env.conn()
    try:
        aid = sales.save_account(c, None, "smtp_imap", "実アカウント", "me@samurai.example", "", {"smtp_host": "smtp.example"}, "", 30, "t")
    finally:
        c.close()
    env.account_id = aid
    d = make_draft(env, to=TO)
    c = env.conn()
    try:
        sales.update_draft(c, d, {"cc": "boss@test-motors.example"}, "tester")
        sales.approve_draft(c, d, "tester")
    finally:
        c.close()
    env.out.provider_override = lambda acc: FakeRealProvider(acc)
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("検証用の宛先" in p for p in r["problems"])
    c = env.conn()
    try:
        runtime.set_option(c, "test_recipient", "verify@owner.example")
    finally:
        c.close()
    FakeRealProvider.sent = []
    assert env.out.send_draft(d, "tester")["result"] == "accepted"
    msg = FakeRealProvider.sent[0]
    assert msg.to == ["verify@owner.example"] and msg.cc == [] and msg.bcc == []
    assert msg.subject.startswith("[TEST] ") and TO in msg.body_text
    assert one(env, "SELECT stage FROM deals WHERE id=?", env.deal_id) == "未着手"  # テスト送信では案件を進めない


def test_bcc_not_visible_in_headers(env):
    d = make_draft(env)
    c = env.conn()
    try:
        sales.update_draft(c, d, {"bcc": "hidden@owner.example"}, "tester")
        sales.approve_draft(c, d, "tester")
    finally:
        c.close()
    assert env.out.send_draft(d, "tester")["result"] == "accepted"
    raw = one(env, "SELECT raw FROM test_mailbox WHERE folder='sent'")
    assert "hidden@owner.example" not in raw


# ---------------------------------------------------------------- 予約・返信・停止
def sent_raw(e: Env) -> str:
    return one(e, "SELECT raw FROM test_mailbox WHERE folder='sent' ORDER BY id DESC LIMIT 1")


def inject(e: Env, kind: str):
    c = e.conn()
    try:
        testbox.inject(c, sales.get_account(c, e.account_id), kind, sent_raw(e), TO)
    finally:
        c.close()


def make_sequence(e: Env) -> tuple[str, list[str]]:
    c = e.conn()
    try:
        seq = sales.create_sequence(c, e.deal_id, "一連", True, "tester")
    finally:
        c.close()
    sids = []
    for i, step in enumerate(["initial", "followup1", "followup2"]):
        d = make_draft(e, step=step)
        sids.append(schedule(e, d, minutes=10 + i * 60, step=step, seq=seq))
    return seq, sids


def test_reply_stops_followups(env):
    _, sids = make_sequence(env)
    make_due(env, sids[0])
    out = env.sched.run_once()
    assert any(r.get("result") == "accepted" for r in out["sent"]), out
    inject(env, "reply")
    env.sync.sync_account(env.account_id)
    assert one(env, "SELECT kind FROM messages WHERE direction='in'") == "返信"
    assert one(env, "SELECT link_method FROM messages WHERE direction='in'") == "返信参照"
    assert [one(env, "SELECT status FROM schedules WHERE id=?", s) for s in sids[1:]] == ["取消", "取消"]
    assert one(env, "SELECT stage FROM deals WHERE id=?", env.deal_id) == "返信あり・要対応"


def test_reply_after_schedule_blocks_even_if_not_cancelled(env):
    _, sids = make_sequence(env)
    make_due(env, sids[0])
    env.sched.run_once()
    inject(env, "reply")
    env.sync.sync_account(env.account_id)
    # 取り消された予約を人が無理に再開しても、送信前確認で止まる
    c = env.conn()
    try:
        c.execute("UPDATE schedules SET status='予約済み' WHERE id=?", (sids[1],))
        c.execute("UPDATE messages SET handled_at=? WHERE direction='in'", (utcnow(),))
    finally:
        c.close()
    make_due(env, sids[1])
    r = env.sched.run_schedule(sids[1])
    assert r["result"] == "blocked" and any("追客は送りません" in p for p in r["problems"])


def test_unsubscribe_reply(env):
    _, sids = make_sequence(env)
    make_due(env, sids[0])
    env.sched.run_once()
    inject(env, "unsubscribe")
    env.sync.sync_account(env.account_id)
    assert one(env, "SELECT kind FROM messages WHERE direction='in'") == "配信停止要求"
    assert one(env, "SELECT status FROM suppressions WHERE scope='アドレス'") == "確定"
    assert one(env, "SELECT status FROM suppressions WHERE scope='会社'") == "確認待ち"
    assert all(one(env, "SELECT status FROM schedules WHERE id=?", s) in ("取消", "保留") for s in sids[1:])


def test_ambiguous_refusal_is_held_for_review(env):
    _, sids = make_sequence(env)
    make_due(env, sids[0])
    env.sched.run_once()
    inject(env, "refusal")
    env.sync.sync_account(env.account_id)
    assert one(env, "SELECT kind FROM messages WHERE direction='in'") == "拒否の疑い"
    assert one(env, "SELECT COUNT(*) FROM suppressions WHERE scope='会社'") == 0  # 曖昧な拒否は自動で停止確定しない
    assert one(env, "SELECT needs_action FROM messages WHERE direction='in'") == 1


def test_bounce_marks_invalid_address_not_company(env):
    _, sids = make_sequence(env)
    make_due(env, sids[0])
    env.sched.run_once()
    inject(env, "bounce")
    env.sync.sync_account(env.account_id)
    assert one(env, "SELECT kind FROM messages WHERE direction='in'") == "不達通知"
    assert one(env, "SELECT scope FROM suppressions") == "無効な宛先"
    assert one(env, "SELECT COUNT(*) FROM suppressions WHERE scope='会社'") == 0


def test_auto_reply_does_not_stop_followups(env):
    _, sids = make_sequence(env)
    make_due(env, sids[0])
    env.sched.run_once()
    inject(env, "auto")
    env.sync.sync_account(env.account_id)
    assert one(env, "SELECT kind FROM messages WHERE direction='in'") == "自動応答"
    assert one(env, "SELECT status FROM schedules WHERE id=?", sids[1]) == "予約済み"


def test_duplicate_and_out_of_order_and_other_sender(env):
    d = make_draft(env)
    env.out.send_draft(d, "tester")
    inject(env, "reply")
    inject(env, "duplicate")
    inject(env, "old")
    inject(env, "reply_other")
    inject(env, "new_same_domain")
    env.sync.sync_account(env.account_id)
    env.sync.sync_account(env.account_id)  # 何度取得しても重複登録しない
    rows = []
    c = env.conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT from_addr, link_method, received_at FROM messages WHERE direction='in' ORDER BY received_at")]
    finally:
        c.close()
    assert len(rows) == 4
    assert rows[0]["received_at"] < rows[-1]["received_at"]
    methods = {r["from_addr"]: r["link_method"] for r in rows}
    assert "送信元ドメイン一致（要確認）" in methods.values()
    assert any(r["from_addr"].startswith("colleague@") and r["link_method"] == "返信参照" for r in rows)


def test_unrelated_mail_is_not_stored(env):
    c = env.conn()
    try:
        acc = sales.get_account(c, env.account_id)
        c.execute("INSERT INTO test_mailbox(account_id, folder, raw, message_id_header, created_at) VALUES(?, 'inbox', ?, '<p@x>', ?)",
                  (env.account_id, "From: friend@private.example\nTo: me@samurai.example\nSubject: Japanese used cars\nMessage-ID: <p@x>\n\nhi",
                   utcnow()))
    finally:
        c.close()
    env.sync.sync_account(env.account_id)
    assert one(env, "SELECT COUNT(*) FROM messages WHERE direction='in'") == 0  # 件名が似ていても関連付けない


def test_late_schedule_is_held_not_sent(env):
    d = make_draft(env)
    sid = schedule(env, d)
    make_due(env, sid, minutes_ago=180)  # PC 停止中に期限を過ぎた
    out = env.sched.run_once()
    assert out["late"] == 1
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "保留"
    assert one(env, "SELECT COUNT(*) FROM test_mailbox WHERE folder='sent'") == 0


def test_followup_held_when_sync_fails(env):
    d1 = make_draft(env)
    env.out.send_draft(d1, "tester")
    d2 = make_draft(env, step="followup1")
    sid = schedule(env, d2, step="followup1")
    set_account(env, simulate="同期エラー")
    make_due(env, sid)
    env.sched.run_once()
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "保留"
    assert "返信の確認" in one(env, "SELECT hold_reason FROM schedules WHERE id=?", sid)


def test_initial_schedule_held_when_sync_failing(env):
    d = make_draft(env)
    sid = schedule(env, d)
    set_account(env, simulate="同期エラー")
    make_due(env, sid)
    env.sched.run_once()
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "保留"
    assert "返信の確認" in one(env, "SELECT hold_reason FROM schedules WHERE id=?", sid)
    assert one(env, "SELECT COUNT(*) FROM test_mailbox WHERE folder='sent'") == 0
    # 手動の「今すぐ送信」（初回）は人が確認して押すため、同期の古さでは止めない
    assert env.out.send_draft(d, "tester")["result"] == "accepted"


def test_approval_needed_before_schedule_runs(env):
    d = make_draft(env, approve=False)
    sid = schedule(env, d)
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "承認待ち"
    make_due(env, sid)
    env.sched.run_once()
    assert one(env, "SELECT COUNT(*) FROM test_mailbox WHERE folder='sent'") == 0


def test_regenerated_body_returns_schedule_to_approval(env):
    d = make_draft(env)
    sid = schedule(env, d)
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "予約済み"
    c = env.conn()
    try:
        sales.update_draft(c, d, {"body": "Different body"}, "tester", source="AI")
    finally:
        c.close()
    assert one(env, "SELECT status FROM schedules WHERE id=?", sid) == "承認待ち"


def test_pause_resume_cancel_company_stop(env):
    d = make_draft(env)
    sid = schedule(env, d)
    c = env.conn()
    try:
        sales.schedule_action(c, sid, "pause", "t")
        assert sales.get_schedule(c, sid)["status"] == "一時停止"
        sales.schedule_action(c, sid, "resume", "t")
        assert sales.get_schedule(c, sid)["status"] == "予約済み"
        sales.add_suppression(c, "会社", "t", dealer_id=env.dealer_id)
        assert sales.get_schedule(c, sid)["status"] == "保留"
        sales.schedule_action(c, sid, "cancel", "t")
        assert sales.get_schedule(c, sid)["status"] == "取消"
    finally:
        c.close()


# ---------------------------------------------------------------- 稼働環境（コピー・復元・巻き戻し）
def test_db_copied_to_other_folder_does_not_send(env, tmp_path):
    d = make_draft(env)
    c = env.conn()
    try:
        runtime.sending_environment(c, env.paths.data_dir)  # 元の環境を送信担当にする
    finally:
        c.close()
    other = Paths(tmp_path / "copy")
    other.ensure()
    shutil.copyfile(env.paths.db, other.db)
    e2 = Env(other)
    r = e2.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("別の稼働環境" in p for p in r["problems"])


def test_db_rollback_detected(env):
    d1 = make_draft(env)
    assert env.out.send_draft(d1, "tester")["result"] == "accepted"
    c = env.conn()
    try:
        runtime.set_state(c, "send_seq", "0")  # 古いバックアップに戻ったのと同じ状態
    finally:
        c.close()
    d2 = make_draft(env)
    r = env.out.send_draft(d2, "tester")
    assert r["result"] == "blocked" and any("以前の状態" in p for p in r["problems"])


def test_restore_requires_reconcile_then_ownership(env):
    d = make_draft(env)
    c = env.conn()
    try:
        runtime.mark_reconcile_required(c, "復元（試験）", "admin")
        assert runtime.send_mode(c) == "停止"
        runtime.set_send_mode(c, "テスト", "admin")
    finally:
        c.close()
    r = env.out.send_draft(d, "tester")
    assert r["result"] == "blocked" and any("照合" in p for p in r["problems"])
    c = env.conn()
    try:
        runtime.take_ownership(c, env.paths.data_dir, "admin")
    finally:
        c.close()
    assert env.out.send_draft(d, "tester")["result"] == "accepted"


# ---------------------------------------------------------------- 時刻
def test_timezones_and_dst():
    assert local_to_utc("2026-07-01", "10:00", "Europe/London").isoformat() == "2026-07-01T09:00:00+00:00"
    assert local_to_utc("2026-12-01", "10:00", "Europe/London").isoformat() == "2026-12-01T10:00:00+00:00"
    assert local_to_utc("2026-07-01", "10:00", "Asia/Tokyo").isoformat() == "2026-07-01T01:00:00+00:00"
    with pytest.raises(TimeInputError, match="存在しない"):
        local_to_utc("2026-03-29", "01:30", "Europe/London")
    with pytest.raises(AmbiguousTimeError):
        local_to_utc("2026-10-25", "01:30", "Europe/London")
    a = local_to_utc("2026-10-25", "01:30", "Europe/London", fold=0)
    b = local_to_utc("2026-10-25", "01:30", "Europe/London", fold=1)
    assert (b - a) == timedelta(hours=1)


# ---------------------------------------------------------------- 分類
def _ib(body="", subject="Re: hello", frm="a@dealer.example", headers=None, dsn=False):
    return InboundMessage("x", None, None, None, frm, "", "", subject, body, None, headers or {}, is_dsn=dsn)


def test_classify():
    assert classify(_ib("Please unsubscribe me")) == "配信停止要求"
    assert classify(_ib("Thanks, we are not interested at the moment.")) == "拒否の疑い"
    assert classify(_ib("Yes please send details")) == "返信"
    assert classify(_ib("Yes please send details\n\nOn Mon, Taro wrote:\n> reply unsubscribe")) == "返信"  # 引用部分は見ない
    assert classify(_ib("away", headers={"auto-submitted": "auto-replied"})) == "自動応答"
    assert classify(_ib("x", subject="Out of Office: hello")) == "自動応答"
    assert classify(_ib("x", frm="mailer-daemon@dealer.example")) == "不達通知"


def test_instance_is_stable_across_restarts(paths):
    a = runtime.local_instance(paths.data_dir)["instance_id"]
    assert runtime.local_instance(paths.data_dir)["instance_id"] == a
    assert runtime._machine_fingerprint() == runtime._machine_fingerprint()
