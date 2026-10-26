"""予約送信と受信同期の定期処理（アプリが起動している間だけ動く）。

- 送信予約は DB に保存されており、画面を閉じてもアプリ（サーバー）が起動していれば実行する。
- アプリ・PC の停止中に予定時刻を過ぎた予約は、起動後にまとめて送らず「保留（遅延）」にする。
- 追客（2回目・3回目）は、送信の直前に受信を確認し、返信があれば止める。受信確認が古い・失敗している場合は保留。
- 送信モードが「停止」の間は送らない。実際に送るかどうかの判断は outbound.py の共通処理で行う。
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from . import runtime, sales
from .db import connect, utcnow
from .inbound import SyncService
from .outbound import OutboundService, can_receive
from .tz import iso, parse_iso, utcnow as now_dt

log = logging.getLogger("sdr.scheduler")


class Scheduler:
    def __init__(self, outbound: OutboundService, sync: SyncService, interval: float = 20.0):
        self.outbound = outbound
        self.sync = sync
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_run: str | None = None
        self.last_error: str | None = None

    def conn(self):
        return connect(self.outbound.db_path)

    def start(self) -> None:
        self.outbound.recover_on_start()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # 1 回の失敗で定期処理を止めない
                self.last_error = str(exc)
                log.exception("定期処理でエラー")
            self._stop.wait(self.interval)

    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        """1 回分の処理。試験や画面の「今すぐ確認」からも呼ぶ。"""
        with self._lock:
            summary = {"synced": [], "late": 0, "sent": [], "held": []}
            conn = self.conn()
            try:
                runtime.heartbeat(conn, "scheduler")
                opts = runtime.options(conn)
                accounts = [a for a in sales.list_accounts(conn) if a["enabled"]]
            finally:
                conn.close()
            interval = int(opts["sync_interval_minutes"] or 5)
            for a in accounts:
                if not can_receive(a) or a["status"] == "切断":
                    continue
                last = parse_iso(a.get("last_sync_at"))
                if not last or (now_dt() - last).total_seconds() >= interval * 60:
                    summary["synced"].append(self.sync.sync_account(a["id"]))
            summary["late"] = self.mark_late()
            for sid in self.due_ids():
                summary["sent"].append(self.run_schedule(sid))
            self.last_run = utcnow()
            return summary

    def mark_late(self) -> int:
        conn = self.conn()
        try:
            grace = int(runtime.get_option(conn, "late_grace_minutes") or 10)
            limit = iso(now_dt() - timedelta(minutes=grace))
            n = conn.execute("UPDATE schedules SET status='保留', hold_reason=?, updated_at=? WHERE status='予約済み' AND send_at_utc < ?",
                             (f"予定時刻を {grace} 分以上過ぎていました（アプリ停止・送信モード停止・PC のスリープ等）。"
                              "まとめて送らず保留にしています。内容と状況を確認し、日時を指定して再開してください", utcnow(), limit)).rowcount
            if n:
                runtime.audit(conn, "予約処理", "遅延した予約を保留", None, f"{n} 件")
            return n
        finally:
            conn.close()

    def due_ids(self) -> list[str]:
        conn = self.conn()
        try:
            if runtime.send_mode(conn) == "停止":
                return []
            return [r["id"] for r in conn.execute("SELECT id FROM schedules WHERE status='予約済み' AND send_at_utc <= ? "
                                                  "ORDER BY send_at_utc LIMIT 20", (utcnow(),))]
        finally:
            conn.close()

    def run_schedule(self, schedule_id: str) -> dict:
        conn = self.conn()
        try:
            s = sales.get_schedule(conn, schedule_id)
            account = sales.get_account(conn, s["account_id"]) if s else None
        finally:
            conn.close()
        if not s or s["status"] != "予約済み":
            return {"result": "skipped", "schedule": schedule_id}
        # 予約の送信直前に受信を確認する（返信・配信停止があれば outbound の確認で止まる）
        if account and can_receive(account):
            self.sync.sync_account(account["id"])
        r = self.outbound.send_draft(s["draft_id"], "予約処理", schedule_id=schedule_id)
        r["schedule"] = schedule_id
        return r

    def status(self) -> dict:
        conn = self.conn()
        try:
            hb = runtime.get_state(conn, "heartbeat:scheduler")
            mode = runtime.send_mode(conn)
            ok, why = runtime.sending_environment(conn, self.outbound.data_dir)
        finally:
            conn.close()
        alive = bool(self._thread and self._thread.is_alive())
        return {"alive": alive, "heartbeat": hb, "mode": mode, "env_ok": ok, "env_reason": why, "last_error": self.last_error}
