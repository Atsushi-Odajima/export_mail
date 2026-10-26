"""メール接続（テスト用接続先・SMTP/IMAP・Gmail API・Microsoft Graph）。

画面・AI・調査処理からは直接呼ばない。送信は必ず outbound.py の共通処理（送信前確認・二重送信防止）を通す。
"""
from .base import (AuthError, InboundMessage, MailError, OutgoingMessage, SendRejected, SendResult, SendUnknown,
                   SyncError)

__all__ = ["AuthError", "InboundMessage", "MailError", "OutgoingMessage", "SendRejected", "SendResult", "SendUnknown",
           "SyncError", "get_provider"]


def get_provider(account: dict, secrets, conn_factory=None, http=None):
    """アカウント設定から接続方式の処理を作る。"""
    provider = account["provider"]
    if provider == "test":
        from .testbox import TestProvider
        return TestProvider(account, secrets, conn_factory)
    if provider == "smtp_imap":
        from .smtp_imap import SmtpImapProvider
        return SmtpImapProvider(account, secrets)
    if provider == "gmail":
        from .gmail import GmailProvider
        return GmailProvider(account, secrets, http=http)
    if provider == "graph":
        from .graph import GraphProvider
        return GraphProvider(account, secrets, http=http)
    raise MailError(f"不明な接続方式です: {provider}")
