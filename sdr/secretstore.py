"""認証情報（パスワード・OAuth の更新トークン・API キー）の保管。

- Windows では OS の資格情報マネージャー（keyring の Windows バックエンド）に保存する。
- 資格情報保管が使えない環境（試験用の Linux など）では、データフォルダーの secrets.json
  （DB・バックアップ・CSV・ログに含めないファイル。所有者のみ読み書き可）に保存する。
- 値そのものは画面・ログ・DB に出さない。画面には「保存済み／未保存」だけを表示する。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("sdr.secrets")
SERVICE = "SamuraiDealerResearch"


class SecretStore:
    def __init__(self, fallback_path: Path, use_keyring: bool | None = None):
        self.fallback_path = fallback_path
        self._lock = threading.Lock()
        self._keyring = None
        if use_keyring is not False:
            try:
                import keyring
                from keyring.backends import fail

                kr = keyring.get_keyring()
                # 使える保管先がない場合は fail.Keyring（優先度 0）になる
                if not isinstance(kr, fail.Keyring) and float(getattr(kr, "priority", 0) or 0) > 0:
                    self._keyring = keyring
            except Exception:
                self._keyring = None

    @property
    def backend_name(self) -> str:
        return "Windows 資格情報マネージャー（OS の資格情報保管）" if self._keyring else f"保護されたファイル（{self.fallback_path.name}）"

    # -- ファイル保管 ------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.fallback_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, values: dict) -> None:
        tmp = self.fallback_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(values), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.fallback_path)

    # -- 公開操作 ----------------------------------------------------------
    def get(self, name: str) -> str | None:
        if self._keyring:
            try:
                value = self._keyring.get_password(SERVICE, name)
                if value is not None:
                    return value
            except Exception:
                log.warning("資格情報マネージャーから読み取れませんでした（%s）", name)
        with self._lock:
            return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        if self._keyring:
            try:
                self._keyring.set_password(SERVICE, name, value)
                with self._lock:  # 以前ファイルに置いていた値は消す
                    data = self._load()
                    if name in data:
                        data.pop(name)
                        self._save(data)
                return
            except Exception:
                log.warning("資格情報マネージャーに保存できなかったため、保護されたファイルに保存します（%s）", name)
        with self._lock:
            data = self._load()
            data[name] = value
            self._save(data)

    def delete(self, name: str) -> None:
        if self._keyring:
            try:
                self._keyring.delete_password(SERVICE, name)
            except Exception:
                pass
        with self._lock:
            data = self._load()
            if name in data:
                data.pop(name)
                self._save(data)

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def delete_prefix(self, prefix: str) -> None:
        """アカウント削除時など、その接頭辞の値をまとめて消す（資格情報マネージャー側は既知の名前のみ）。"""
        for suffix in ("password", "refresh_token", "client_secret"):
            self.delete(f"{prefix}:{suffix}")
        with self._lock:
            data = self._load()
            keys = [k for k in data if k.startswith(prefix + ":")]
            for k in keys:
                data.pop(k)
            if keys:
                self._save(data)
