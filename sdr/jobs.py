"""バックグラウンド処理（画面を固めない）。

- 調査ジョブは DB の job_items を 1 件ずつ取り出して実行する。完了分はその都度保存。
- 停止：ジョブを「停止要求」にすると、実行中の販売店はページ取得の合間で止まり、残りは待機のまま。
- アプリ終了後の再起動では、実行中だったものを「中断」に戻し、利用者が「再開」できる。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import discovery, repo
from .db import connect, transaction
from .fetcher import Fetcher, FetchSettings
from .research import research_site

log = logging.getLogger("sdr.jobs")


class JobManager:
    def __init__(self, db_path: Path, secrets_path: Path, transport=None, resolve_dns: bool = True):
        self.db_path = db_path
        self.secrets_path = secrets_path
        self.transport = transport          # 試験用
        self.resolve_dns = resolve_dns
        self._threads: list[threading.Thread] = []
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.paused = False                 # バックアップ復元中など
        self.min_interval_floor = 2.0       # 取得間隔の下限（試験でのみ変更）

    def conn(self):
        return connect(self.db_path)

    # -- 起動・終了 -------------------------------------------------------
    def start(self) -> None:
        conn = self.conn()
        try:
            n = repo.recover_interrupted(conn)
            if n:
                log.info("中断していた %s 件を待機に戻しました", n)
            workers = int(float(repo.get_settings(conn)["global_concurrency"]))
        finally:
            conn.close()
        for i in range(workers):
            t = threading.Thread(target=self._worker, name=f"research-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._shutdown.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    # -- 操作 -------------------------------------------------------------
    def start_research(self, dealer_ids: list[str], label: str = "") -> str:
        conn = self.conn()
        try:
            jid = repo.create_job(conn, "research", dealer_ids, {"label": label})
        finally:
            conn.close()
        self.wake()
        return jid

    def stop(self, job_id: str) -> None:
        conn = self.conn()
        try:
            job = repo.job_view(conn, job_id)
            if job and job["status"] == "実行中":
                repo.set_job_status(conn, job_id, "停止要求", "停止を受け付けました。実行中の販売店はページ取得の合間で止まります")
                self._maybe_finish(conn, job_id)
        finally:
            conn.close()

    def resume(self, job_id: str) -> None:
        conn = self.conn()
        try:
            with transaction(conn):
                conn.execute("UPDATE job_items SET status='待機', error=NULL WHERE job_id=? AND status='停止'", (job_id,))
                for r in conn.execute("SELECT dealer_id FROM job_items WHERE job_id=? AND status='待機'", (job_id,)).fetchall():
                    repo.set_research_status(conn, r["dealer_id"], "待機中")
                repo.set_job_status(conn, job_id, "実行中", "再開しました")
        finally:
            conn.close()
        self.wake()

    def retry_failed(self, job_id: str) -> str | None:
        conn = self.conn()
        try:
            ids = [r["dealer_id"] for r in conn.execute(
                "SELECT dealer_id FROM job_items WHERE job_id=? AND status IN ('失敗', '停止')", (job_id,))]
        finally:
            conn.close()
        return self.start_research(ids, "失敗・停止分の再実行") if ids else None

    def is_stop_requested(self, job_id: str | None) -> bool:
        if self._shutdown.is_set():
            return True
        if not job_id:
            return False
        conn = self.conn()
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            return not row or row["status"] != "実行中"
        finally:
            conn.close()

    # -- 実行 -------------------------------------------------------------
    def _worker(self) -> None:
        while not self._shutdown.is_set():
            if self.paused:
                time.sleep(0.5)
                continue
            conn = self.conn()
            try:
                item = repo.claim_next_item(conn)
            except Exception:  # DB が一時的にロック中など
                log.exception("ジョブ取り出しに失敗")
                item = None
            finally:
                conn.close()
            if not item:
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            try:
                self.run_item(item)
            except Exception as exc:
                log.exception("調査処理で想定外のエラー")
                conn = self.conn()
                try:
                    repo.finish_item(conn, item["id"], "失敗", f"想定外のエラー: {type(exc).__name__}")
                    repo.set_research_status(conn, item["dealer_id"], "取得失敗")
                finally:
                    conn.close()

    def fetch_settings(self, settings: dict) -> FetchSettings:
        return FetchSettings(
            min_interval_sec=max(self.min_interval_floor, float(settings["min_interval_sec"])),
            timeout_sec=float(settings["timeout_sec"]),
            max_bytes=int(float(settings["max_bytes"])),
            max_retries=int(float(settings["max_retries"])),
            cache_hours=float(settings["cache_hours"]),
        )

    def run_item(self, item) -> None:
        job_id = item["job_id"]
        conn = self.conn()
        try:
            dealer = conn.execute("SELECT * FROM dealers WHERE id=?", (item["dealer_id"],)).fetchone()
            settings = repo.get_settings(conn)
            if not dealer:
                repo.finish_item(conn, item["id"], "失敗", "販売店が見つかりません")
                return
            run_id = repo.start_run(conn, dealer["id"], job_id)
        finally:
            conn.close()
        stop = lambda: self.is_stop_requested(job_id)  # noqa: E731
        fetcher = Fetcher(self.fetch_settings(settings), cache=repo.DBCache(self.conn), transport=self.transport,
                          resolve_dns=self.resolve_dns, stop=stop)
        try:
            result = research_site(dealer["website_url"], fetcher, int(float(settings["max_pages_per_site"])), stop)
        finally:
            fetcher.close()
        conn = self.conn()
        try:
            repo.apply_research(conn, dealer["id"], run_id, result)
            if result.status == "停止":
                repo.finish_item(conn, item["id"], "停止", result.failure_reason)
            elif result.ok:
                repo.finish_item(conn, item["id"], "完了", result.failure_reason or None)
            else:
                repo.finish_item(conn, item["id"], "失敗", result.failure_reason)
            self._maybe_finish(conn, job_id)
        finally:
            conn.close()

    def _maybe_finish(self, conn, job_id: str) -> None:
        job = repo.job_view(conn, job_id)
        if not job:
            return
        counts = job["counts"]
        if counts.get("実行中"):
            return
        if job["status"] == "停止要求":
            with transaction(conn):
                for r in conn.execute("SELECT dealer_id FROM job_items WHERE job_id=? AND status='待機'", (job_id,)).fetchall():
                    conn.execute("UPDATE dealers SET research_status=CASE WHEN last_researched_at IS NULL THEN '未調査' "
                                 "ELSE '停止' END WHERE id=?", (r["dealer_id"],))
                conn.execute("UPDATE job_items SET status='停止' WHERE job_id=? AND status='待機'", (job_id,))
                repo.set_job_status(conn, job_id, "停止", "停止しました。「再開」で残りを実行できます")
        elif job["status"] == "実行中" and not counts.get("待機"):
            repo.set_job_status(conn, job_id, "完了",
                                f"完了 {counts.get('完了', 0)} 件・失敗 {counts.get('失敗', 0)} 件・停止 {counts.get('停止', 0)} 件")

    # -- 候補発見（別スレッドで1回実行） ------------------------------------
    def start_discovery(self, provider: str, region: str, category: str, limit: int) -> str:
        conn = self.conn()
        try:
            jid = repo.create_job(conn, "discover", [], {"provider": provider, "region": region, "category": category,
                                                         "limit": limit})
        finally:
            conn.close()
        threading.Thread(target=self._run_discovery, args=(jid, provider, region, category, limit), daemon=True).start()
        return jid

    def _run_discovery(self, job_id: str, provider: str, region: str, category: str, limit: int) -> None:
        conn = self.conn()
        try:
            if provider == "osm":
                found = discovery.discover_osm(region, limit)
                source, used = "OpenStreetMap", 0.0
            elif provider == "firecrawl":
                settings = repo.get_settings(conn)
                cap = float(settings["firecrawl_monthly_credit_cap"])
                used_now = repo.api_usage_this_month(conn, "firecrawl")
                limit = min(limit, int(float(settings["firecrawl_max_results_per_run"])))
                expected = max(1, -(-limit // 10)) * 2
                if used_now + expected > cap:
                    raise discovery.DiscoveryError(
                        f"今月の Firecrawl 使用上限（{cap:g} クレジット、使用済み {used_now:g}）に達するため実行しません")
                key = discovery.load_secrets(self.secrets_path).get("firecrawl_api_key", "")
                found, used = discovery.discover_firecrawl(key, category, region, limit)
                repo.record_api_usage(conn, "firecrawl", used, f"{category} {region}")
                source = "Firecrawl"
            else:
                raise discovery.DiscoveryError("不明な発見方法です")
            added = dup = excluded = 0
            for c in found:
                cid, note = repo.add_candidate(conn, c.url, source, c.name, region, c.hint, c.detail)
                if cid and note == "候補に追加":
                    added += 1
                elif cid or note == "候補に登録済み":
                    dup += 1
                else:
                    excluded += 1
            msg = f"{source}: {len(found)} 件取得 → 新しい候補 {added} 件・登録済み/重複 {dup} 件・除外 {excluded} 件"
            if used:
                msg += f"（消費 {used:g} クレジット）"
            repo.set_job_status(conn, job_id, "完了", msg)
        except discovery.DiscoveryError as exc:
            repo.set_job_status(conn, job_id, "失敗", str(exc))
        except Exception as exc:
            log.exception("候補発見で想定外のエラー")
            repo.set_job_status(conn, job_id, "失敗", f"想定外のエラー: {type(exc).__name__}")
        finally:
            conn.close()
