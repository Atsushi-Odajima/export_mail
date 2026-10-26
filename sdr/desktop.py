"""専用ウィンドウでの起動（Windows の通常の使い方）。

画面のサーバーは今までどおり 127.0.0.1 だけで動かし、その画面を Windows 標準の
Microsoft Edge WebView2 を使ったアプリ専用のウィンドウに表示する。
- ウィンドウを閉じるとアプリが終了する（実行中の調査は中断され、次回「再開」できる）。
- 調査の実行中、または予約済みの送信があるときだけ、閉じる前に確認を出す（終了すると予約は送られない）。
- 2 回目に起動されたときは新しく起動せず、既存のウィンドウを前面に出す。
- 販売店サイトなど外部へのリンクは、このウィンドウではなく既定のブラウザーで開く。
- CSV・バックアップの保存は「名前を付けて保存」で行う。
- WebView2 が使えない PC では、従来どおり既定のブラウザーで開く。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser

import httpx

from . import APP_NAME, __version__
from .config import Paths

log = logging.getLogger("sdr.desktop")

QUIT_CONFIRMATION = ("調査の実行中、または予約済みの送信があります。終了すると、調査は中断され（次回「再開」できます）、"
                     "予約はアプリを起動するまで送られません（予定時刻を過ぎた予約は保留になります）。終了しますか？")
LOCALIZATION = {
    "global.quitConfirmation": QUIT_CONFIRMATION,
    "global.ok": "OK",
    "global.quit": "終了",
    "global.cancel": "キャンセル",
    "global.saveFile": "ファイルを保存",
    "windows.fileFilter.allFiles": "すべてのファイル",
    "windows.fileFilter.otherFiles": "その他のファイル",
}


def focus_existing(paths: Paths) -> bool:
    """起動中のアプリがあれば、そのウィンドウを前面に出して True を返す。"""
    from .main import LOCK_NAME, existing_instance

    url = existing_instance(paths)
    if not url:
        return False
    try:
        info = json.loads((paths.data_dir / LOCK_NAME).read_text(encoding="utf-8"))
        token = info.get("focus_token") or ""
        if token:
            r = httpx.post(url + "/desktop/focus", headers={"x-sdr-focus-token": token}, timeout=5, trust_env=False)
            if r.status_code == 200:
                return True
    except Exception:
        log.warning("起動中のウィンドウを前面に出せませんでした", exc_info=True)
    # ブラウザー版（cli）で起動中の場合などは、ブラウザーで開く
    webbrowser.open(url + "/")
    return True


def research_running(paths: Paths) -> bool:
    from .db import connect

    try:
        conn = connect(paths.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('実行中', '停止要求')").fetchone()[0]
        finally:
            conn.close()
        return n > 0
    except Exception:
        return False


def scheduled_pending(paths: Paths) -> bool:
    """予約済み（承認済みで送信待ち）の予約があるか。アプリを閉じると予約処理も止まるため確認に使う。"""
    from .db import connect

    try:
        conn = connect(paths.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM schedules WHERE status='予約済み'").fetchone()[0]
        finally:
            conn.close()
        return n > 0
    except Exception:
        return False


def load_webview():
    """pywebview と Edge WebView2 が使えれば webview モジュールを返す。使えなければ None。"""
    if sys.platform != "win32":
        return None
    try:
        import webview
        from webview.platforms import winforms

        if getattr(winforms, "renderer", "") != "edgechromium":
            log.warning("Microsoft Edge WebView2 が見つからないため、ブラウザーで開きます")
            return None
        return webview
    except Exception:
        log.warning("専用ウィンドウを使えないため、ブラウザーで開きます", exc_info=True)
        return None


def wait_ready(url: str, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url + "healthz", timeout=2, trust_env=False).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def run_desktop(paths: Paths, port_start: int = 8765) -> None:
    from .main import build_server, free_port, serve, show_error, single_instance_marker

    if focus_existing(paths):
        return
    mutex = single_instance_marker()
    port = free_port(port_start)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    focus_token = secrets.token_urlsafe(24)
    holder: dict = {}

    def on_shutdown():  # 画面の「アプリを終了」
        window = holder.get("window")
        if window is not None:
            window.confirm_close = False
            threading.Timer(0.8, window.destroy).start()

    server, app, url = build_server(paths, "127.0.0.1", port, "local", allowed, secure_cookies=False,
                                    on_shutdown=on_shutdown)
    app.state.port = port
    thread = threading.Thread(target=serve, args=(paths, server, app, "local", port, {"focus_token": focus_token}),
                              name="server", daemon=True)
    thread.start()
    if not wait_ready(url):
        server.should_exit = True
        show_error("画面のサーバーを起動できませんでした。もう一度起動してください。")
        return

    webview = load_webview()
    if webview is None:
        if not os.environ.get("SDR_DESKTOP_SMOKE"):
            webbrowser.open(url)
        thread.join()  # 画面の「アプリを終了」で終わる
        return

    webview.settings["ALLOW_DOWNLOADS"] = True                 # CSV・バックアップは「名前を付けて保存」
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # 販売店サイトは既定のブラウザーで開く
    window = webview.create_window(f"{APP_NAME} {__version__}", url, width=1280, height=880, min_size=(900, 600),
                                   text_select=True, zoomable=True, localization=LOCALIZATION)
    holder["window"] = window
    stats = {"focus_count": 0}

    def focus():
        stats["focus_count"] += 1
        try:
            window.restore()
            window.show()
            window.on_top = True
            window.on_top = False
        except Exception:
            log.warning("ウィンドウを前面に出せませんでした", exc_info=True)

    app.state.focus_token = focus_token
    app.state.focus_cb = focus

    stop = threading.Event()

    def watch_busy():  # 調査の実行中・予約済みの送信があるときだけ、閉じるときに確認を出す
        while not stop.is_set():
            window.confirm_close = research_running(paths) or scheduled_pending(paths)
            stop.wait(2)

    threading.Thread(target=watch_busy, name="close-guard", daemon=True).start()

    smoke = os.environ.get("SDR_DESKTOP_SMOKE")
    if smoke:  # 配布物の起動確認（CI）：表示を確かめて結果を書き、指定秒数後に閉じる
        def check():
            time.sleep(1.5)
            result = {"renderer": "edgechromium", "url": url}
            try:
                result["title"] = window.evaluate_js("document.title")
                result["has_list"] = bool(window.evaluate_js("document.body.innerText.indexOf('ダッシュボード') >= 0"))
                result["rows"] = window.evaluate_js("parseInt((document.getElementById('stat-dealers') || {}).textContent || '0', 10)")
            except Exception as exc:
                result["error"] = repr(exc)
            time.sleep(float(os.environ.get("SDR_DESKTOP_SMOKE_HOLD", "0")))
            result["focus_count"] = stats["focus_count"]
            with open(smoke, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            window.confirm_close = False
            window.destroy()

        def on_loaded():
            if not stats.get("checked"):  # 最初の表示のときだけ
                stats["checked"] = True
                threading.Thread(target=check, daemon=True).start()

        window.events.loaded += on_loaded

    try:
        webview.start(gui="edgechromium", private_mode=True)
    finally:
        stop.set()
        server.should_exit = True
        thread.join(15)
        del mutex
