"""完全バックアップ・復元（ID・手動確定値・根拠・履歴を含むデータベース単位）。

バックアップ ZIP の中身
- dealers.sqlite3 … 取得キャッシュとログインセッションを除いた DB
- manifest.json   … アプリ版・DB の版・作成日時・件数
APIキー（secrets.json）は含めない。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__
from .db import connect, current_version, latest_version, utcnow

MANIFEST = "manifest.json"
DBNAME = "dealers.sqlite3"


class BackupError(RuntimeError):
    pass


def create_backup(db_path: Path, dest_dir: Path, label: str = "manual") -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest_dir / f"sdr-backup-{stamp}-{label}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / DBNAME
        src = connect(db_path)
        try:
            dst = sqlite3.connect(str(tmp_db))
            src.backup(dst)
            dst.close()
        finally:
            src.close()
        conn = sqlite3.connect(str(tmp_db), isolation_level=None)
        conn.execute("DELETE FROM fetch_cache")
        conn.execute("DELETE FROM sessions")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("dealers", "contact_methods", "evidence", "change_log", "candidates", "users")}
        version = int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
        conn.execute("VACUUM")
        conn.close()
        manifest = {"app": "SamuraiDealerResearch", "app_version": __version__, "schema_version": version,
                    "created_at": utcnow(), "counts": counts,
                    "note": "取得キャッシュ・ログインセッション・APIキーは含みません"}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, DBNAME)
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    return out


def inspect_backup(zip_path: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if names - {MANIFEST, DBNAME} or DBNAME not in names or MANIFEST not in names:
                raise BackupError("このアプリのバックアップ形式ではありません")
            info = zf.getinfo(DBNAME)
            if info.file_size > 2_000_000_000:
                raise BackupError("バックアップが大きすぎます")
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise BackupError("ZIP ファイルとして読み取れません") from exc
    if manifest.get("app") != "SamuraiDealerResearch":
        raise BackupError("このアプリのバックアップではありません")
    if int(manifest.get("schema_version", 0)) > latest_version():
        raise BackupError("新しい版のアプリで作られたバックアップです。アプリを更新してから復元してください")
    return manifest


def restore_backup(zip_path: Path, db_path: Path, backup_dir: Path) -> tuple[dict, Path]:
    """復元前に現在のデータを自動バックアップしてから置き換える。戻り値 (manifest, 復元前バックアップ)。"""
    manifest = inspect_backup(zip_path)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extract(DBNAME, tmp)
        restored = Path(tmp) / DBNAME
        conn = sqlite3.connect(str(restored))
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise BackupError(f"バックアップのデータベースが壊れています（{ok}）")
            conn.row_factory = sqlite3.Row
            ver = current_version(conn)
            if ver < 1:
                raise BackupError("データベースの版を確認できません")
        finally:
            conn.close()
        pre = create_backup(db_path, backup_dir, "before-restore") if db_path.exists() else None
        tmp_target = db_path.with_suffix(".restoring")
        shutil.copyfile(restored, tmp_target)
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        tmp_target.replace(db_path)
    return manifest, pre
