"""変更されない内部IDの発行。

形式は「接頭辞-ULID」。ULID は時刻順に並ぶ 26 文字の Crockford Base32。
店名・URL・メールアドレスが変わっても ID は変えない。
"""
import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIX_DEALER = "DLR"
PREFIX_CONTACT = "CNT"
PREFIX_CANDIDATE = "CAN"
PREFIX_JOB = "JOB"
PREFIX_RUN = "RUN"


def ulid() -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    return f"{prefix}-{ulid()}"
