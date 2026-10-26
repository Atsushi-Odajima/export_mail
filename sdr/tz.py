"""日時とタイムゾーン（内部は UTC。画面は日本時間・英国時間を切り替え）。

英国（Europe/London）は夏時間があり、
- 3 月の切り替え日の 1:00〜1:59 は存在しない時刻
- 10 月の切り替え日の 1:00〜1:59 は 2 回ある時刻
になる。存在しない時刻はエラーにし、2 回ある時刻は利用者に「1 回目（夏時間）／2 回目（冬時間）」を選んでもらう。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ_CHOICES = {"Asia/Tokyo": "日本時間", "Europe/London": "英国時間"}


class TimeInputError(ValueError):
    pass


class AmbiguousTimeError(TimeInputError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_to_utc(date_str: str, time_str: str, tz_name: str, fold: int | None = None) -> datetime:
    """画面で入力した現地日時を UTC にする。存在しない時刻・2 回ある時刻は例外で知らせる。"""
    if tz_name not in TZ_CHOICES:
        raise TimeInputError("タイムゾーンは日本時間か英国時間を選んでください")
    try:
        naive = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise TimeInputError("日付は YYYY-MM-DD、時刻は HH:MM の形で入力してください") from exc
    zone = ZoneInfo(tz_name)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    # 往復して同じ時刻に戻らなければ存在しない時刻
    back = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if back != naive:
        raise TimeInputError(
            f"{date_str} {time_str}（{TZ_CHOICES[tz_name]}）は夏時間の切り替えで存在しない時刻です。"
            "1 時間後などの時刻を指定してください")
    if first.utcoffset() != second.utcoffset():
        if fold not in (0, 1):
            raise AmbiguousTimeError(
                f"{date_str} {time_str}（{TZ_CHOICES[tz_name]}）は夏時間の終了で 2 回ある時刻です。"
                "「1 回目（夏時間）」か「2 回目（冬時間）」を選んでください")
        return (second if fold == 1 else first).astimezone(timezone.utc)
    return first.astimezone(timezone.utc)


def fmt(value: str | datetime | None, tz_name: str = "Asia/Tokyo", with_tz: bool = True) -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return "—"
    local = dt.astimezone(ZoneInfo(tz_name))
    label = TZ_CHOICES.get(tz_name, tz_name)
    abbr = ""
    if tz_name == "Europe/London":
        abbr = "BST" if local.dst() and local.dst() != timedelta(0) else "GMT"
    s = local.strftime("%Y-%m-%d %H:%M")
    return f"{s}（{label}{' ' + abbr if abbr else ''}）" if with_tz else s


def split_local(value: str | None, tz_name: str) -> tuple[str, str]:
    dt = parse_iso(value)
    if not dt:
        return "", ""
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
