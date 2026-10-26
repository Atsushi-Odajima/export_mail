"""汎用 SMTP 送信＋IMAP 受信。

- 認証はユーザー名＋パスワード（Gmail はアプリパスワード）。パスワード認証を許可しないサービス
  （Outlook.com 等。2024-09-16 に廃止）は Microsoft Graph の接続を使う。
- 送信は MAIL / RCPT / DATA を分けて実行し、
  DATA の前に失敗 → 送信前に失敗（送っていない）
  DATA 中の切断・タイムアウト → 結果不明（自動で再送しない）
  として区別する。SMTP は二重送信を完全には防げないため、結果不明のときは送信済みフォルダーと照合する。
- 受信は IMAP の UID と UIDVALIDITY を取得位置として保存し、再起動後も続きから取得する（既読にしない）。
"""
from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone

from .base import (AuthError, InboundMessage, MailError, OutgoingMessage, SendRejected, SendResult, SendUnknown,
                   SyncError, build_mime, parse_raw)

DEFAULTS = {
    "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
    "imap_host": "", "imap_port": 993, "username": "",
    "inbox_folder": "INBOX", "sent_folder": "Sent", "save_to_sent": True, "sync_days": 14,
}
PRESETS = {
    "gmail": {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_security": "starttls", "imap_host": "imap.gmail.com",
              "imap_port": 993, "sent_folder": "[Gmail]/Sent Mail", "save_to_sent": False},
}


class SmtpImapProvider:
    def __init__(self, account: dict, secrets, smtp_factory=None, imap_factory=None, timeout: float = 30.0):
        self.account = account
        self.cfg = {**DEFAULTS, **(account.get("config") or {})}
        self.secrets = secrets
        self.timeout = timeout
        self.smtp_factory = smtp_factory
        self.imap_factory = imap_factory

    # -- 共通 -------------------------------------------------------------
    def _password(self) -> str:
        pw = self.secrets.get(f"{self.account['id']}:password") if self.secrets else None
        if not pw:
            raise AuthError("パスワード（アプリパスワード）が保存されていません。メール接続の設定で入力してください")
        return pw

    def _username(self) -> str:
        return self.cfg.get("username") or self.account["from_address"]

    def _smtp(self):
        host, port = self.cfg["smtp_host"], int(self.cfg["smtp_port"])
        if not host:
            raise SendRejected("SMTP サーバーが設定されていません")
        if self.smtp_factory:
            return self.smtp_factory(host, port, self.cfg["smtp_security"], self.timeout)
        ctx = ssl.create_default_context()
        if self.cfg["smtp_security"] == "ssl":
            return smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=ctx)
        s = smtplib.SMTP(host, port, timeout=self.timeout)
        s.ehlo()
        if self.cfg["smtp_security"] == "starttls":
            s.starttls(context=ctx)
            s.ehlo()
        return s

    def _imap(self):
        host, port = self.cfg["imap_host"], int(self.cfg["imap_port"])
        if not host:
            raise SyncError("IMAP サーバーが設定されていません")
        if self.imap_factory:
            im = self.imap_factory(host, port)
        else:
            im = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=self.timeout)
        try:
            im.login(self._username(), self._password())
        except imaplib.IMAP4.error as exc:
            raise AuthError(f"IMAP の認証に失敗しました：{exc}") from exc
        return im

    # -- 接続確認 ---------------------------------------------------------
    def check(self) -> str:
        notes = []
        try:
            s = self._smtp()
            try:
                s.login(self._username(), self._password())
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
            notes.append("SMTP：認証できました")
        except smtplib.SMTPAuthenticationError as exc:
            raise AuthError(f"SMTP の認証に失敗しました（{exc.smtp_code}）") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(f"SMTP に接続できません：{exc}") from exc
        if self.cfg.get("imap_host"):
            try:
                im = self._imap()
                im.logout()
                notes.append("IMAP：認証できました")
            except AuthError:
                raise
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailError(f"IMAP に接続できません：{exc}") from exc
        else:
            notes.append("IMAP 未設定のため返信の取得はできません（送信前の返信確認ができないため、追客の予約は保留になります）")
        return " / ".join(notes)

    # -- 送信 -------------------------------------------------------------
    def send(self, msg: OutgoingMessage, on_prepared=None) -> SendResult:
        mime = build_mime(msg)
        data = mime.as_bytes()
        try:
            s = self._smtp()
        except (OSError, smtplib.SMTPException) as exc:
            raise SendRejected(f"SMTP サーバーに接続できませんでした：{exc}") from exc
        stage = "接続"
        try:
            try:
                stage = "認証"
                s.login(self._username(), self._password())
            except smtplib.SMTPAuthenticationError as exc:
                raise AuthError(f"SMTP の認証に失敗しました（{exc.smtp_code}）") from exc
            stage = "送信元"
            code, resp = s.mail(msg.from_addr)
            if code != 250:
                raise SendRejected(f"送信元が拒否されました（{code}）")
            stage = "宛先"
            for rcpt in msg.envelope_recipients:
                code, resp = s.rcpt(rcpt)
                if code not in (250, 251):
                    try:
                        s.rset()
                    except Exception:
                        pass
                    raise SendRejected(f"宛先 {rcpt} が拒否されました（{code}）")
            if on_prepared:
                on_prepared(None, msg.message_id)
            stage = "本文"
            try:
                code, resp = s.data(data)
            except smtplib.SMTPDataError as exc:
                raise SendRejected(f"本文の受け付けを拒否されました（{exc.smtp_code}）") from exc
            except (smtplib.SMTPServerDisconnected, socket.timeout, OSError) as exc:
                raise SendUnknown(f"本文の送信中に接続が切れました。届いたかどうか分かりません：{exc}") from exc
            if code != 250:
                raise SendRejected(f"本文の受け付けを拒否されました（{code}）")
        except (SendRejected, SendUnknown, AuthError):
            raise
        except smtplib.SMTPException as exc:
            if stage == "本文":
                raise SendUnknown(f"本文の送信中にエラーが起きました：{exc}") from exc
            raise SendRejected(f"{stage}の段階で失敗しました：{exc}") from exc
        except (socket.timeout, OSError) as exc:
            if stage == "本文":
                raise SendUnknown(f"本文の送信中に通信が切れました：{exc}") from exc
            raise SendRejected(f"{stage}の段階で通信が切れました：{exc}") from exc
        finally:
            try:
                s.quit()
            except Exception:
                pass
        if self.cfg.get("save_to_sent") and self.cfg.get("imap_host"):
            try:  # 送信済みフォルダーに保存（照合のため。失敗しても送信結果は変えない）
                im = self._imap()
                try:
                    im.append(self._quote(self.cfg["sent_folder"]), "\\Seen", imaplib.Time2Internaldate(time.time()), data)
                finally:
                    im.logout()
            except Exception:
                pass
        return SendResult(message_id_header=msg.message_id)

    @staticmethod
    def _quote(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    # -- 受信 -------------------------------------------------------------
    def fetch_new(self, cursor: str | None) -> tuple[list[InboundMessage], str]:
        try:
            im = self._imap()
        except AuthError:
            raise
        except (OSError, imaplib.IMAP4.error) as exc:
            raise SyncError(f"IMAP に接続できません：{exc}") from exc
        try:
            typ, _ = im.select(self._quote(self.cfg["inbox_folder"]), readonly=True)
            if typ != "OK":
                raise SyncError(f"受信フォルダー {self.cfg['inbox_folder']} を開けません")
            uidvalidity = ""
            resp = im.response("UIDVALIDITY")
            if resp and resp[1] and resp[1][0]:
                uidvalidity = resp[1][0].decode() if isinstance(resp[1][0], bytes) else str(resp[1][0])
            last_validity, last_uid = (cursor.split(":", 1) + ["0"])[:2] if cursor else ("", "0")
            if cursor and last_validity == uidvalidity:
                typ, data = im.uid("SEARCH", None, f"UID {int(last_uid) + 1}:*")
            else:  # 初回、または UIDVALIDITY が変わった：直近の期間だけ取得（重複はメッセージ ID で除く）
                since = (datetime.now(timezone.utc) - timedelta(days=int(self.cfg.get("sync_days") or 14))).strftime("%d-%b-%Y")
                typ, data = im.uid("SEARCH", None, f"SINCE {since}")
                last_uid = "0"
            uids = [int(x) for x in (data[0].split() if data and data[0] else [])]
            uids = sorted(u for u in uids if u > int(last_uid))
            out: list[InboundMessage] = []
            for uid in uids[:500]:
                typ, fetched = im.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in fetched or [] if isinstance(p, tuple) and len(p) > 1), None)
                if raw:
                    out.append(parse_raw(raw, f"imap:{uidvalidity}:{uid}"))
                last_uid = str(uid)
            return out, f"{uidvalidity}:{last_uid}"
        except (OSError, imaplib.IMAP4.error) as exc:
            raise SyncError(f"受信中にエラーが起きました：{exc}") from exc
        finally:
            try:
                im.logout()
            except Exception:
                pass

    def find_sent(self, message_id_header: str | None, provider_message_id: str | None) -> bool | None:
        """送信済みフォルダーで Message-ID を探す。見つからなくても、保存されない設定なら「不明」を返す。"""
        if not message_id_header or not self.cfg.get("imap_host"):
            return None
        try:
            im = self._imap()
        except Exception:
            return None
        try:
            typ, _ = im.select(self._quote(self.cfg["sent_folder"]), readonly=True)
            if typ != "OK":
                return None
            typ, data = im.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id_header}"')
            found = bool(data and data[0] and data[0].split())
            if found:
                return True
            # Gmail の SMTP はサーバー側で送信済みに保存するため、見つからなければ未送信と判断できる。
            # このアプリが後から保存する方式では、本文送信が途中で切れた場合に保存されないので「不明」とする
            auto_saved = (self.cfg.get("smtp_host") or "").endswith("gmail.com")
            return False if auto_saved else None
        except Exception:
            return None
        finally:
            try:
                im.logout()
            except Exception:
                pass
