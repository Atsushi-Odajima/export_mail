"""送信モードと「送信担当の稼働環境」。

誤送信・二重送信を防ぐため、予約送信を実行できる環境を 1 つに限定する。
- データフォルダーの instance.json（DB の外）に、この稼働環境の ID と、DB ごとの送信番号を記録する。
- DB の runtime_state に、送信担当の環境 ID と送信番号（send_seq）を記録する。
- 次の場合は送信を止め、照合（外部の送信履歴との突き合わせ）を求める。
  * DB を別の PC・別のフォルダーにコピーした（担当環境 ID が一致しない）
  * DB が過去の状態に戻った（DB の送信番号が instance.json の記録より小さい＝復元・巻き戻し）
  * 画面からバックアップを復元した
- 判定できない場合も送信しない（保留）。
"""
from __future__ import annotations

import json
import platform
import secrets
import threading
from pathlib import Path

from .db import utcnow

SEND_MODES = ("停止", "テスト", "実送信")
_lock = threading.Lock()


def _machine_id() -> str:
    """OS が持つ、再起動しても変わらない PC の識別子（Windows の MachineGuid 等）。取れなければ空。"""
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except OSError:
            return ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text().strip()
            if value:
                return value
        except OSError:
            continue
    return ""


def _machine_fingerprint() -> str:
    # uuid.getnode() は MAC アドレスを読めない環境で起動ごとに乱数になるため使わない
    return f"{platform.node()}|{_machine_id()}"


def local_instance(data_dir: Path) -> dict:
    """この稼働環境の情報。別の PC・別のフォルダーへコピーされた instance.json は新しい環境として作り直す。"""
    path = data_dir / "instance.json"
    with _lock:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        here = str(data_dir.resolve())
        if info.get("machine") != _machine_fingerprint() or info.get("data_dir") != here or not info.get("instance_id"):
            info = {"instance_id": "ENV-" + secrets.token_hex(8), "machine": _machine_fingerprint(), "data_dir": here,
                    "created_at": utcnow(), "send_seq": {}}
            _write(path, info)
        return info


def _write(path: Path, info: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def get_state(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO runtime_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def db_uid(conn) -> str:
    uid = get_state(conn, "db_uid")
    if not uid:
        uid = "DB-" + secrets.token_hex(8)
        set_state(conn, "db_uid", uid)
    return uid


def send_mode(conn) -> str:
    mode = get_state(conn, "send_mode", "停止")
    return mode if mode in SEND_MODES else "停止"


def set_send_mode(conn, mode: str, actor: str | None) -> None:
    if mode not in SEND_MODES:
        raise ValueError("送信モードが不正です")
    set_state(conn, "send_mode", mode)
    audit(conn, actor, "送信モード変更", "system", mode)


def audit(conn, actor: str | None, action: str, target: str | None = None, detail: str | None = None) -> None:
    conn.execute("INSERT INTO audit_log(at, actor, action, target, detail) VALUES(?,?,?,?,?)",
                 (utcnow(), actor, action, target, (detail or "")[:1000]))


def sending_environment(conn, data_dir: Path) -> tuple[bool, str]:
    """この環境が送信してよいか。(可否, 理由)"""
    inst = local_instance(data_dir)
    uid = db_uid(conn)
    owner = get_state(conn, "send_owner_instance")
    if not owner:
        # 初めて使う DB：この環境を送信担当にする
        set_state(conn, "send_owner_instance", inst["instance_id"])
        set_state(conn, "send_seq", get_state(conn, "send_seq", "0") or "0")
        owner = inst["instance_id"]
    if get_state(conn, "reconcile_required") == "1":
        return False, ("復元・コピー・巻き戻しの後のため送信を止めています（理由："
                       f"{get_state(conn, 'reconcile_reason', '不明')}）。「照合」画面で外部の送信履歴を確認してから再開してください")
    if owner != inst["instance_id"]:
        return False, ("このデータベースは別の稼働環境（別の PC やコピー元）が送信担当です。"
                       "誤送信を防ぐため、この環境では送信しません。「照合」画面で送信担当を切り替えられます")
    db_seq = int(get_state(conn, "send_seq", "0") or 0)
    seen = int(inst.get("send_seq", {}).get(uid, 0))
    if db_seq < seen:
        return False, (f"データベースが以前の状態に戻っています（送信番号 {db_seq} < 記録 {seen}）。"
                       "復元や巻き戻しの可能性があるため送信を止めています。「照合」画面で確認してください")
    return True, ""


def bump_send_seq(conn, data_dir: Path) -> int:
    """送信試行のたびに送信番号を進め、DB と DB の外（instance.json）の両方に記録する。"""
    seq = int(get_state(conn, "send_seq", "0") or 0) + 1
    set_state(conn, "send_seq", str(seq))
    path = data_dir / "instance.json"
    inst = local_instance(data_dir)
    with _lock:
        inst.setdefault("send_seq", {})[db_uid(conn)] = seq
        _write(path, inst)
    return seq


def mark_reconcile_required(conn, reason: str, actor: str | None = None) -> None:
    set_state(conn, "reconcile_required", "1")
    set_state(conn, "reconcile_reason", reason)
    set_state(conn, "send_mode", "停止")
    audit(conn, actor, "送信停止（照合待ち）", "system", reason)


def take_ownership(conn, data_dir: Path, actor: str | None) -> None:
    """照合を終えた管理者が、この環境を送信担当にして再開できる状態にする（送信モードは停止のまま）。"""
    inst = local_instance(data_dir)
    set_state(conn, "send_owner_instance", inst["instance_id"])
    set_state(conn, "reconcile_required", "0")
    set_state(conn, "reconcile_reason", "")
    seq = int(get_state(conn, "send_seq", "0") or 0)
    with _lock:
        inst.setdefault("send_seq", {})[db_uid(conn)] = seq
        _write(data_dir / "instance.json", inst)
    audit(conn, actor, "送信担当をこの環境に設定（照合済み）", "system", inst["instance_id"])


def heartbeat(conn, name: str) -> None:
    set_state(conn, f"heartbeat:{name}", utcnow())


# ---------------------------------------------------------------- アプリの設定（営業・メール・AI）
OPTION_DEFAULTS = {
    "display_tz": "Asia/Tokyo",          # 画面の表示（日本時間 / 英国時間）
    "test_recipient": "",                # テストモードで実アカウントを使うときの検証用宛先（利用者が指定）
    "late_grace_minutes": "10",          # 予定時刻からこれ以上遅れた予約は送らず保留
    "sync_interval_minutes": "5",        # 受信の定期確認の間隔
    "sync_fresh_minutes": "30",          # 追客の送信前に必要な「受信確認の新しさ」
    "public_base_url": "",               # 配信停止リンク用の HTTPS の公開アドレス（無ければ返信で停止を案内）
    "ai_enabled": "0",
    "ai_model": "claude-opus-5-5",
    "ai_effort": "medium",
    "ai_fallbacks": "1",                 # 安全上の理由で断られたときに別モデルで再実行（Claude API の fallbacks）
    "ai_monthly_request_cap": "200",
    "ai_monthly_token_cap": "2000000",
}


def get_option(conn, key: str) -> str:
    return get_state(conn, f"opt:{key}", OPTION_DEFAULTS.get(key, "")) or ""


def set_option(conn, key: str, value: str) -> None:
    if key not in OPTION_DEFAULTS:
        raise ValueError(f"不明な設定です: {key}")
    set_state(conn, f"opt:{key}", value.strip())


def options(conn) -> dict[str, str]:
    return {k: get_option(conn, k) for k in OPTION_DEFAULTS}
