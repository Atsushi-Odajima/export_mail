"""送信の共通処理（画面の「今すぐ送信」と予約実行の両方がここを通る）。

送信前に必ず確認すること（preflight）：
  送信モード／送信担当の稼働環境（コピー・復元・巻き戻しでないか）／送信元アカウントの状態／
  承認（その版の内容ハッシュと一致するか）／差し込み未完了／宛先の形式と連絡拒否（会社・個人・アドレス・ドメイン・無効な宛先）／
  案件の状態と「送信可」の判断／販売店の対象外・非表示／返信・未対応の受信／受信確認の新しさ（追客）／1日の送信上限

二重送信の防止：
  予約は「予約済み → 送信中」の条件付き更新で 1 つの処理だけが取得する。
  送信試行は idempotency_key（予約または下書き＋版＋再試行番号）が一意で、同じ内容の 2 回目の試行を DB が拒否する。
  結果不明の試行がある間は、同じ下書きを再送しない（照合または人の判断が必要）。

結果の区別：受付済み（サービスが受け付けた。配達・開封ではない）／送信前に失敗／結果不明。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import runtime, sales
from .db import connect, transaction, utcnow
from .ids import PREFIX_MESSAGE, PREFIX_THREAD, new_id
from .mail import AuthError, OutgoingMessage, SendRejected, SendUnknown, get_provider
from .mail.base import new_message_id
from .tz import parse_iso, utcnow as tz_now

log = logging.getLogger("sdr.outbound")
DONE_RESULTS = ("送信中", "受付済み", "結果不明", "照合で受付確認", "人が送信済みと判断")
FAILED_RESULTS = ("送信前に失敗", "照合で未送信確認", "人が未送信と判断")
RECEIVING = {"test", "gmail", "graph"}


def can_receive(account: dict) -> bool:
    return account["provider"] in RECEIVING or (account["provider"] == "smtp_imap" and bool(account["config"].get("imap_host")))


class OutboundService:
    def __init__(self, data_dir: Path, db_path: Path, secret_store, http=None):
        self.data_dir = data_dir
        self.db_path = db_path
        self.secrets = secret_store
        self.http = http
        self.provider_override = None  # 試験用

    def conn(self):
        return connect(self.db_path)

    def provider(self, account: dict):
        if self.provider_override:
            return self.provider_override(account)
        return get_provider(account, self.secrets, self.conn, self.http)

    # ------------------------------------------------------------------ 送信前確認
    def preflight(self, conn, draft: dict, schedule: dict | None = None) -> list[str]:
        problems: list[str] = []
        mode = runtime.send_mode(conn)
        if mode == "停止":
            problems.append("送信モードが「停止」です（設定 → 送信モード で「テスト」か「実送信」にしてください）")
        ok, why = runtime.sending_environment(conn, self.data_dir)
        if not ok:
            problems.append(why)
        if draft["status"] == "送信済み":
            problems.append("この文面は送信済みです")
        account = sales.get_account(conn, draft["account_id"]) if draft.get("account_id") else None
        if not account:
            problems.append("送信元アカウントが設定されていません")
        else:
            if not account["enabled"]:
                problems.append(f"送信元アカウント「{account['label']}」は無効になっています")
            if account["status"] in ("認証エラー", "切断"):
                problems.append(f"送信元アカウント「{account['label']}」が{account['status']}です。再認証してください")
            if mode == "テスト" and account["provider"] != "test":
                tr = runtime.get_option(conn, "test_recipient")
                if not tr or not sales.EMAIL_RE.match(tr):
                    problems.append("テストモードで実際のメールアカウントを使うには、検証用の宛先（設定 → 送信モード）を指定してください")
            limit = int(account["daily_limit"] or 0)
            sent = conn.execute("SELECT COUNT(*) FROM send_attempts WHERE account_id=? AND result IN "
                                "('送信中','受付済み','結果不明','照合で受付確認','人が送信済みと判断') "
                                "AND started_at >= datetime('now','-1 day')", (account["id"],)).fetchone()[0]
            if limit and sent >= limit:
                problems.append(f"送信元アカウントの1日の送信上限（{limit} 通）に達しています")
        if not sales.current_approval(conn, draft):
            problems.append("承認されていないか、承認後に宛先・件名・本文・添付・送信元が変わりました。内容を確認して承認し直してください")
        ph = sales.find_placeholders(draft["subject"], draft["body"])
        if ph:
            problems.append("差し込みが完了していない箇所があります：" + "、".join(ph[:6]))
        try:
            addrs = sales.validate_addresses(draft["to_address"], required=True) + sales.validate_addresses(draft["cc"]) \
                + sales.validate_addresses(draft["bcc"])
        except sales.SalesError as exc:
            problems.append(str(exc))
            addrs = []
        deal = sales.get_deal(conn, draft["deal_id"])
        problems += sales.blocking_suppressions(conn, draft["dealer_id"], addrs, deal.get("person_id") if deal else None)
        if deal:
            if deal["stage"] in sales.CLOSED_STAGES:
                problems.append(f"案件の状態が「{deal['stage']}」です")
            if mode == "実送信" and not deal["send_allowed"]:
                problems.append("この案件は、営業メールを送ってよいと人が判断（「送信可」と根拠の記録）していません")
            if deal["archived"]:
                problems.append("販売店が非表示（重複等）になっています")
            if deal.get("eligibility_manual") == "対象外":
                problems.append("販売店が「対象外」と判断されています")
        pending = conn.execute("SELECT kind FROM messages WHERE dealer_id=? AND direction='in' AND needs_action=1 "
                               "AND handled_at IS NULL LIMIT 1", (draft["dealer_id"],)).fetchone()
        if pending and draft["step"] != "reply":
            problems.append(f"この販売店から未対応の受信（{pending['kind']}）があります。受信・対応の画面で確認してください")
        if draft["step"] in sales.FOLLOWUP_STEPS or (schedule and schedule.get("sequence_id")):
            since = None
            if schedule and schedule.get("sequence_id"):
                seq = conn.execute("SELECT created_at, stop_on_reply FROM sequences WHERE id=?", (schedule["sequence_id"],)).fetchone()
                since = seq["created_at"] if seq and seq["stop_on_reply"] else None
            elif draft["step"] in sales.FOLLOWUP_STEPS:
                first = conn.execute("SELECT MIN(sent_at) FROM messages WHERE deal_id=? AND direction='out'", (draft["deal_id"],)).fetchone()[0]
                since = first
            if since:
                replied = conn.execute("SELECT kind FROM messages WHERE dealer_id=? AND direction='in' AND kind IN "
                                       "('返信','配信停止要求','拒否の疑い') AND COALESCE(received_at, created_at) >= ? LIMIT 1",
                                       (draft["dealer_id"], since)).fetchone()
                if replied:
                    problems.append(f"最初の営業の後に受信（{replied['kind']}）があったため、追客は送りません")
            if account and not can_receive(account):
                problems.append("この送信元アカウントは受信を確認できないため、追客（2回目・3回目）は自動送信しません")
        # 予約送信と追客は、受信の確認が新しいときだけ送る（同期の失敗・停止中に返信を見落として送らないため）
        if (schedule is not None or draft["step"] in sales.FOLLOWUP_STEPS) and account and can_receive(account):
            fresh = int(runtime.get_option(conn, "sync_fresh_minutes") or 30)
            last_ok = parse_iso(account.get("last_sync_ok_at"))
            if not last_ok or (tz_now() - last_ok).total_seconds() > fresh * 60:
                why = f"（同期エラー：{account['sync_error'][:80]}）" if account.get("sync_error") else ""
                problems.append(f"返信の確認が {fresh} 分以上前のため、予約・追客の送信を保留します{why}。受信の同期を確認してください")
        return problems

    # ------------------------------------------------------------------ 送信
    def send_draft(self, draft_id: str, actor: str | None, schedule_id: str | None = None) -> dict:
        conn = self.conn()
        try:
            with transaction(conn):
                draft = sales.get_draft(conn, draft_id)
                if not draft:
                    return {"result": "error", "message": "文面が見つかりません"}
                schedule = None
                if schedule_id:
                    n = conn.execute("UPDATE schedules SET status='送信中', claimed_by=?, claimed_at=?, updated_at=? "
                                     "WHERE id=? AND status='予約済み'",
                                     (runtime.local_instance(self.data_dir)["instance_id"], utcnow(), utcnow(), schedule_id)).rowcount
                    if n != 1:
                        return {"result": "skipped", "message": "この予約は他の処理が実行中か、状態が変わっています"}
                    schedule = dict(conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())
                problems = self.preflight(conn, draft, schedule)
                busy = conn.execute(f"SELECT result FROM send_attempts WHERE draft_id=? AND draft_version=? AND result IN "
                                    f"({','.join('?' * len(DONE_RESULTS))}) LIMIT 1",
                                    (draft_id, draft["version"], *DONE_RESULTS)).fetchone()
                if busy:
                    problems.append(f"この文面には既に送信試行があります（{busy['result']}）。二重送信を防ぐため送りません")
                if problems:
                    if schedule_id:
                        conn.execute("UPDATE schedules SET status='保留', hold_reason=?, claimed_by=NULL, updated_at=? WHERE id=?",
                                     ("送信前の確認で止めました：" + " / ".join(problems), utcnow(), schedule_id))
                    runtime.audit(conn, actor, "送信を止めました", schedule_id or draft_id, " / ".join(problems)[:900])
                    return {"result": "blocked", "problems": problems}
                failures = conn.execute(f"SELECT COUNT(*) FROM send_attempts WHERE draft_id=? AND draft_version=? AND result IN "
                                        f"({','.join('?' * len(FAILED_RESULTS))})",
                                        (draft_id, draft["version"], *FAILED_RESULTS)).fetchone()[0]
                key = f"{'sch:' + schedule_id if schedule_id else 'drf:' + draft_id}:v{draft['version']}:r{failures}"
                account = sales.get_account(conn, draft["account_id"])
                mode = runtime.send_mode(conn)
                redirect = mode == "テスト" and account["provider"] != "test"
                to = sales.split_addresses(draft["to_address"])
                cc, bcc = sales.split_addresses(draft["cc"]), sales.split_addresses(draft["bcc"])
                subject, body = draft["subject"], draft["body"]
                if redirect:
                    tr = runtime.get_option(conn, "test_recipient")
                    body = (f"[テスト送信] 本来の宛先: To {', '.join(to)}"
                            + (f" / Cc {', '.join(cc)}" if cc else "") + (f" / Bcc {', '.join(bcc)}" if bcc else "")
                            + "\n----------------------------------------\n\n" + body)
                    subject, to, cc, bcc = "[TEST] " + subject, [tr], [], []
                message_id = new_message_id(account["from_address"])
                try:
                    cur = conn.execute(
                        "INSERT INTO send_attempts(idempotency_key, schedule_id, draft_id, draft_version, account_id, instance_id, "
                        "test_mode, actual_recipients, message_id_header, result, started_at) VALUES(?,?,?,?,?,?,?,?,?,'送信中',?)",
                        (key, schedule_id, draft_id, draft["version"], account["id"],
                         runtime.local_instance(self.data_dir)["instance_id"], 1 if mode == "テスト" else 0,
                         ", ".join([*to, *cc, *bcc]), message_id, utcnow()))
                except sqlite3.IntegrityError:
                    return {"result": "duplicate", "message": "同じ送信が既に実行されています（二重送信を防止しました）"}
                attempt_id = cur.lastrowid
                runtime.bump_send_seq(conn, self.data_dir)
                out = self._build(conn, draft, account, to, cc, bcc, subject, body, message_id)
        finally:
            conn.close()
        return self._execute(attempt_id, draft, account, schedule_id, out, actor, redirect)

    def _build(self, conn, draft, account, to, cc, bcc, subject, body, message_id) -> OutgoingMessage:
        in_reply_to = references = thread_ext = reply_ext = None
        ref = None
        if draft.get("reply_to_message_id"):
            ref = sales.get_message(conn, draft["reply_to_message_id"])
        elif draft["step"] in ("followup1", "followup2"):
            ref = conn.execute("SELECT * FROM messages WHERE deal_id=? AND direction='out' ORDER BY sent_at DESC LIMIT 1",
                               (draft["deal_id"],)).fetchone()
            ref = dict(ref) if ref else None
        if ref and ref.get("message_id_header"):
            in_reply_to = ref["message_id_header"]
            references = ((ref.get("references_hdr") or "") + " " + ref["message_id_header"]).strip()
            if ref.get("account_id") == account["id"]:
                thread_ext = ref.get("external_thread_id")
                if account["provider"] == "graph" and ref["direction"] == "in" and (ref.get("external_id") or "").startswith("graph:"):
                    reply_ext = ref["external_id"].split(":", 1)[1]
        attachments = []
        for a in sales.draft_attachments(conn, draft["id"]):
            path = self.data_dir / "attachments" / a["stored_name"]
            attachments.append((a["filename"], a["content_type"], path.read_bytes()))
        list_unsub = f"<mailto:{account['from_address']}?subject=unsubscribe>"
        return OutgoingMessage(from_addr=account["from_address"], from_name=account.get("from_name") or "", to=to, cc=cc,
                               bcc=bcc, subject=subject, body_text=body, message_id=message_id, in_reply_to=in_reply_to,
                               references=references, attachments=attachments, thread_external_id=thread_ext,
                               reply_to_external_id=reply_ext, list_unsubscribe=list_unsub)

    def _execute(self, attempt_id: int, draft: dict, account: dict, schedule_id: str | None, out: OutgoingMessage,
                 actor: str | None, redirect: bool) -> dict:
        def on_prepared(provider_id, mid):
            c = self.conn()
            try:
                c.execute("UPDATE send_attempts SET provider_message_id=COALESCE(?, provider_message_id), message_id_header=? WHERE id=?",
                          (provider_id, mid, attempt_id))
            finally:
                c.close()

        provider = None
        try:
            provider = self.provider(account)
            result = provider.send(out, on_prepared=on_prepared)
        except AuthError as exc:
            return self._finish_failure(attempt_id, account, schedule_id, "送信前に失敗", f"認証エラー：{exc}", actor, auth=True)
        except SendRejected as exc:
            return self._finish_failure(attempt_id, account, schedule_id, "送信前に失敗", str(exc), actor)
        except SendUnknown as exc:
            return self._finish_failure(attempt_id, account, schedule_id, "結果不明", str(exc), actor)
        except Exception as exc:  # 想定外：送ったかどうか分からないものとして扱う（自動で再送しない）
            log.exception("送信中の想定外のエラー")
            state = "結果不明" if provider is not None else "送信前に失敗"
            return self._finish_failure(attempt_id, account, schedule_id, state, f"想定外のエラー：{exc}", actor)
        conn = self.conn()
        try:
            with transaction(conn):
                conn.execute("UPDATE send_attempts SET result='受付済み', provider_message_id=?, provider_thread_id=?, "
                             "message_id_header=?, finished_at=? WHERE id=?",
                             (result.provider_message_id, result.provider_thread_id, result.message_id_header, utcnow(), attempt_id))
                self._record_sent(conn, draft, account, schedule_id, out, result.message_id_header, result.provider_message_id,
                                  result.provider_thread_id, redirect)
                runtime.audit(conn, actor, "送信（サービスが受付）", schedule_id or draft["id"],
                              f"試行 {attempt_id}{'・テスト（検証用宛先）' if redirect else ''}")
        finally:
            conn.close()
        return {"result": "accepted", "message": "送信サービスが受け付けました（相手への配達・開封の確認ではありません）",
                "attempt_id": attempt_id}

    def _record_sent(self, conn, draft, account, schedule_id, out: OutgoingMessage, mid, provider_id, thread_id, redirect) -> str:
        thread = None
        if out.in_reply_to:
            r = conn.execute("SELECT thread_id FROM messages WHERE message_id_header=? AND thread_id IS NOT NULL LIMIT 1",
                             (out.in_reply_to,)).fetchone()
            thread = r["thread_id"] if r else None
        if not thread:
            thread = new_id(PREFIX_THREAD)
            conn.execute("INSERT INTO threads(id, account_id, dealer_id, deal_id, subject, external_thread_id, last_message_at, created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (thread, account["id"], draft["dealer_id"], draft["deal_id"], out.subject,
                                                     thread_id, utcnow(), utcnow()))
        else:
            conn.execute("UPDATE threads SET last_message_at=?, external_thread_id=COALESCE(external_thread_id, ?) WHERE id=?",
                         (utcnow(), thread_id, thread))
        msg_id = new_id(PREFIX_MESSAGE)
        conn.execute(
            "INSERT INTO messages(id, thread_id, account_id, direction, message_id_header, in_reply_to, references_hdr, external_id, "
            "external_thread_id, from_addr, to_addrs, cc_addrs, subject, body_text, sent_at, dealer_id, deal_id, draft_id, schedule_id, "
            "kind, link_method, needs_action, note, created_at) VALUES(?,?,?, 'out', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, '送信', '送信', 0, ?, ?)",
            (msg_id, thread, account["id"], mid, out.in_reply_to, out.references, f"sent:{provider_id or mid}", thread_id,
             account["from_address"], ", ".join(out.to), ", ".join(out.cc), out.subject, out.body_text, utcnow(),
             draft["dealer_id"], draft["deal_id"], draft["id"], schedule_id,
             "テスト送信（検証用宛先へ）" if redirect else None, utcnow()))
        conn.execute("UPDATE drafts SET status='送信済み', updated_at=? WHERE id=?", (utcnow(), draft["id"]))
        if schedule_id:
            conn.execute("UPDATE schedules SET status='送信済み', sent_message_id=?, hold_reason=NULL, updated_at=? WHERE id=?",
                         (msg_id, utcnow(), schedule_id))
        if not redirect:
            sales.set_stage_if(conn, draft["deal_id"], "送信済み・返信待ち", only_from=("未着手", "初回送信予定"))
        return msg_id

    def _finish_failure(self, attempt_id, account, schedule_id, result, error, actor, auth: bool = False) -> dict:
        conn = self.conn()
        try:
            with transaction(conn):
                conn.execute("UPDATE send_attempts SET result=?, error=?, finished_at=? WHERE id=?",
                             (result, error[:500], utcnow(), attempt_id))
                if schedule_id:
                    st = "結果確認待ち" if result == "結果不明" else "送信前に失敗"
                    conn.execute("UPDATE schedules SET status=?, hold_reason=?, updated_at=? WHERE id=?",
                                 (st, error[:500], utcnow(), schedule_id))
                if auth:
                    sales.set_account_status(conn, account["id"], "認証エラー", error)
                runtime.audit(conn, actor, f"送信：{result}", schedule_id or str(attempt_id), error[:300])
        finally:
            conn.close()
        if result == "結果不明":
            return {"result": "unknown", "message": "送信結果が分かりません。自動では再送しません。「照合」で外部の送信履歴を確認してください：" + error,
                    "attempt_id": attempt_id}
        return {"result": "rejected", "message": "送信前に失敗しました（送信されていません）：" + error, "attempt_id": attempt_id}

    # ------------------------------------------------------------------ 照合・人の判断
    def reconcile(self, attempt_id: int, actor: str | None) -> dict:
        conn = self.conn()
        try:
            a = conn.execute("SELECT * FROM send_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not a:
                return {"result": "error", "message": "送信試行が見つかりません"}
            a = dict(a)
            account = sales.get_account(conn, a["account_id"])
        finally:
            conn.close()
        if a["result"] not in ("結果不明", "送信中"):
            return {"result": "noop", "message": f"この試行は「{a['result']}」のため照合は不要です"}
        try:
            found = self.provider(account).find_sent(a["message_id_header"], a["provider_message_id"])
        except Exception as exc:
            return {"result": "error", "message": f"照合できませんでした：{exc}"}
        if found is None:
            return {"result": "unknown", "message": "外部の送信履歴から判断できませんでした。メールソフト等で確認し、人が判断してください"}
        return self.decide(attempt_id, "送信済み" if found else "未送信", actor, by_reconcile=True)

    def decide(self, attempt_id: int, decision: str, actor: str | None, by_reconcile: bool = False) -> dict:
        if decision not in ("送信済み", "未送信"):
            return {"result": "error", "message": "判断が不正です"}
        conn = self.conn()
        try:
            with transaction(conn):
                a = dict(conn.execute("SELECT * FROM send_attempts WHERE id=?", (attempt_id,)).fetchone())
                if a["result"] not in ("結果不明", "送信中"):
                    return {"result": "noop", "message": f"この試行は既に「{a['result']}」です"}
                label = ("照合で受付確認" if decision == "送信済み" else "照合で未送信確認") if by_reconcile else \
                    ("人が送信済みと判断" if decision == "送信済み" else "人が未送信と判断")
                conn.execute("UPDATE send_attempts SET result=?, finished_at=? WHERE id=?", (label, utcnow(), attempt_id))
                draft = sales.get_draft(conn, a["draft_id"])
                account = sales.get_account(conn, a["account_id"])
                if decision == "送信済み":
                    exists = conn.execute("SELECT 1 FROM messages WHERE message_id_header=? AND direction='out'",
                                          (a["message_id_header"],)).fetchone()
                    if not exists:
                        out = OutgoingMessage(account["from_address"], "", sales.split_addresses(a["actual_recipients"]), [], [],
                                              draft["subject"], draft["body"], a["message_id_header"])
                        self._record_sent(conn, draft, account, a["schedule_id"], out, a["message_id_header"],
                                          a["provider_message_id"], a["provider_thread_id"], bool(a["test_mode"]) and account["provider"] != "test")
                elif a["schedule_id"]:
                    conn.execute("UPDATE schedules SET status='送信前に失敗', hold_reason=?, updated_at=? WHERE id=?",
                                 (f"{label}。必要なら日時を指定して再開してください", utcnow(), a["schedule_id"]))
                runtime.audit(conn, actor, f"送信結果を確定（{label}）", str(attempt_id))
        finally:
            conn.close()
        return {"result": "ok", "message": f"「{label}」として記録しました"}

    def recover_on_start(self) -> int:
        """前回の終了時に送信中だったものは「結果不明」にする（自動で再送しない）。"""
        conn = self.conn()
        try:
            with transaction(conn):
                n = conn.execute("UPDATE send_attempts SET result='結果不明', error='アプリが送信中に終了しました', finished_at=? "
                                 "WHERE result='送信中'", (utcnow(),)).rowcount
                conn.execute("UPDATE schedules SET status='結果確認待ち', hold_reason='アプリが送信中に終了したため結果不明', updated_at=? "
                             "WHERE status='送信中'", (utcnow(),))
            return n
        finally:
            conn.close()
