"""起動とコマンド。

  SamuraiDealerResearch.exe                    ローカルモード（127.0.0.1 のみ・ブラウザーを開く）
  SamuraiDealerResearch.exe shared ...         共有モード（ログイン必須）
  SamuraiDealerResearch.exe create-user NAME   共有モードの利用者を作成（パスワードは対話入力）
  SamuraiDealerResearch.exe backup / restore ZIP / export-csv FILE / migrate
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx

from . import APP_NAME, __version__
from .config import Paths, default_data_dir

LOCK_NAME = "running.json"


def setup_logging(paths: Paths) -> None:
    handler = logging.handlers.RotatingFileHandler(paths.logs / "app.log", maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    root.addHandler(console)
    # 通信ライブラリの詳細ログ（URL・ヘッダー）は残さない
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def existing_instance(paths: Paths) -> str | None:
    lock = paths.data_dir / LOCK_NAME
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}"
        r = httpx.get(url + "/healthz", timeout=2, trust_env=False)
        if r.status_code == 200 and r.json().get("app") == APP_NAME:
            return url
    except Exception:
        return None
    return None


def run_server(paths: Paths, host: str, port: int, mode: str, open_browser: bool, allowed_hosts: set[str] | None,
               secure_cookies: bool, ssl_cert: str | None = None, ssl_key: str | None = None) -> None:
    import uvicorn

    from .web import create_app

    server_holder: dict = {}

    def shutdown():
        srv = server_holder.get("server")
        if srv:
            threading.Timer(0.5, lambda: setattr(srv, "should_exit", True)).start()

    app = create_app(paths, mode=mode, allowed_hosts=allowed_hosts, secure_cookies=secure_cookies, shutdown_cb=shutdown)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False,
                            ssl_certfile=ssl_cert, ssl_keyfile=ssl_key, proxy_headers=False)
    server = uvicorn.Server(config)
    server_holder["server"] = server
    lock = paths.data_dir / LOCK_NAME
    if mode == "local":
        lock.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    scheme = "https" if ssl_cert else "http"
    url = f"{scheme}://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    print(f"{APP_NAME} {__version__}")
    print(f"データの保存場所: {paths.data_dir}")
    print(f"画面: {url}")
    if mode == "local":
        print("終了するには、画面右上の「アプリを終了」を押すか、このウィンドウで Ctrl+C を押してください。")
    else:
        print("共有モードで起動しました。終了するにはこのウィンドウで Ctrl+C を押してください。")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.run()
    finally:
        app.state.jobs.shutdown()
        if mode == "local":
            try:
                lock.unlink()
            except OSError:
                pass
        print("終了しました。")


def cmd_local(args, paths: Paths) -> None:
    running = existing_instance(paths)
    if running:
        print(f"すでに起動しています。ブラウザーで開きます: {running}")
        webbrowser.open(running + "/")
        return
    port = args.port
    for candidate in range(args.port, args.port + 25):
        if port_free("127.0.0.1", candidate):
            port = candidate
            break
    else:
        sys.exit("使用できるポートが見つかりません")
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    run_server(paths, "127.0.0.1", port, "local", not args.no_browser, allowed, secure_cookies=False)


def cmd_shared(args, paths: Paths) -> None:
    from .db import connect, migrate

    migrate(paths.db, paths.backups)
    conn = connect(paths.db)
    try:
        users = conn.execute("SELECT COUNT(*) FROM users WHERE disabled=0").fetchone()[0]
    finally:
        conn.close()
    if users == 0:
        sys.exit("利用者がいません。先に `create-user 利用者名` で利用者を作成してください（既定のパスワードはありません）。")
    loopback = False
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback and not (args.certfile and args.keyfile):
        sys.exit("127.0.0.1 以外で待ち受けるには HTTPS の証明書（--certfile / --keyfile）が必要です。"
                 "通常は 127.0.0.1 のまま Cloudflare Tunnel 等の HTTPS 経路を使ってください（README「共有モード」）。")
    allowed = set(args.allowed_host or [])
    allowed |= {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    secure = not args.insecure_cookies
    run_server(paths, args.host, args.port, "shared", False, allowed, secure_cookies=secure,
               ssl_cert=args.certfile, ssl_key=args.keyfile)


def cmd_create_user(args, paths: Paths) -> None:
    from . import auth
    from .db import connect, migrate

    migrate(paths.db, paths.backups)
    pw = os.environ.get("SDR_NEW_PASSWORD") if args.password_from_env else None
    if pw is None:
        pw = getpass.getpass("パスワード（10文字以上）: ")
        if pw != getpass.getpass("確認のためもう一度: "):
            sys.exit("パスワードが一致しません")
    conn = connect(paths.db)
    try:
        auth.create_user(conn, args.username, pw)
    except ValueError as exc:
        sys.exit(str(exc))
    finally:
        conn.close()
    print(f"利用者 {args.username} を作成（または更新）しました。")


def cmd_backup(args, paths: Paths) -> None:
    from .backup import create_backup
    from .db import migrate

    migrate(paths.db, paths.backups)
    print(create_backup(paths.db, Path(args.dest) if args.dest else paths.backups))


def cmd_restore(args, paths: Paths) -> None:
    from .backup import BackupError, restore_backup
    from .db import migrate

    if existing_instance(paths):
        sys.exit("アプリが起動中です。終了してから復元してください。")
    try:
        manifest, pre = restore_backup(Path(args.zip), paths.db, paths.backups)
    except BackupError as exc:
        sys.exit(str(exc))
    for line in migrate(paths.db, paths.backups):
        print(line)
    print(f"復元しました（作成 {manifest['created_at']}）。復元前のデータ: {pre}")


def cmd_export(args, paths: Paths) -> None:
    from . import csv_io, repo
    from .db import connect, migrate

    migrate(paths.db, paths.backups)
    conn = connect(paths.db)
    try:
        data = csv_io.dealers_csv(repo.list_dealers(conn, {"include_archived": True}))
    finally:
        conn.close()
    Path(args.file).write_bytes(data)
    print(args.file)


def cmd_migrate(args, paths: Paths) -> None:
    from .db import MigrationError, migrate

    try:
        lines = migrate(paths.db, paths.backups)
    except MigrationError as exc:
        sys.exit(str(exc))
    print("\n".join(lines) or "更新は不要です")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="SamuraiDealerResearch", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--data-dir", help="データの保存先（既定: 利用者フォルダー）")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("local", help="ローカルモードで起動（既定）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("shared", help="共有モード（ログイン必須）で起動")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--allowed-host", action="append", help="外部から使うホスト名（例: dealers.example.com）。複数指定可")
    p.add_argument("--certfile")
    p.add_argument("--keyfile")
    p.add_argument("--insecure-cookies", action="store_true", help="HTTPS を使わない動作確認用（通常は使わない）")
    p = sub.add_parser("create-user", help="共有モードの利用者を作成・パスワード変更")
    p.add_argument("username")
    p.add_argument("--password-from-env", action="store_true", help="環境変数 SDR_NEW_PASSWORD から読む（自動化用）")
    p = sub.add_parser("backup", help="バックアップ ZIP を作成")
    p.add_argument("--dest")
    p = sub.add_parser("restore", help="バックアップ ZIP から復元（アプリ終了中に実行）")
    p.add_argument("zip")
    p = sub.add_parser("export-csv", help="販売店一覧を CSV に出力")
    p.add_argument("file")
    sub.add_parser("migrate", help="データベースの版を更新")
    args = parser.parse_args(argv)

    paths = Paths(Path(args.data_dir) if args.data_dir else default_data_dir())
    paths.ensure()
    setup_logging(paths)
    from .db import MigrationError

    handlers = {"local": cmd_local, "shared": cmd_shared, "create-user": cmd_create_user, "backup": cmd_backup,
                "restore": cmd_restore, "export-csv": cmd_export, "migrate": cmd_migrate}
    if args.cmd is None:
        args.cmd, args.port, args.no_browser = "local", 8765, False
    try:
        handlers[args.cmd](args, paths)
    except MigrationError as exc:
        print(str(exc))
        print("README の「更新に失敗したとき」を参照し、backups フォルダーの更新前バックアップから復元できます。")
        if getattr(sys, "frozen", False):
            input("Enter キーで閉じます")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
