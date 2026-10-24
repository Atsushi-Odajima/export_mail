"""公開ページの取得（負荷・安全の制約つき）。

- robots.txt を確認し、不許可のページは取得しない。
- 同一サイトへの取得間隔（既定 2 秒以上）と同時取得 1 を守る。
- 通信の制限時間・最大応答サイズ・再試行上限・キャッシュ。
- 転送（リダイレクト）は自分で 1 回ずつたどり、転送先も安全確認する。
- 直接接続時は実際の接続先 IP も確認する（プロキシ経由時はプロキシ側の接続先を確認できない）。
- アクセス制限・CAPTCHA は回避せず、その旨を記録して止める。
"""
from __future__ import annotations

import gzip
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from .config import ROBOTS_TOKEN, USER_AGENT
from .netsafe import UnsafeURLError, assert_safe_url, is_public_ip

HTML_TYPES = ("text/html", "application/xhtml+xml")
CHALLENGE_TITLE = re.compile(
    r"<title>\s*(?:just a moment|one moment,? please|attention required|checking your browser|access denied|"
    r"please wait while we verify|ddos-guard|security check)[^<]*</title>", re.I)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status_code: int = 0
    content_type: str = ""
    text: str = ""
    error: str = ""
    from_cache: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status_code < 300 and bool(self.text)


@dataclass
class FetchSettings:
    min_interval_sec: float = 2.0
    timeout_sec: float = 15.0
    max_bytes: int = 2_000_000
    max_retries: int = 1
    cache_hours: float = 24.0
    max_redirects: int = 5


class HostThrottle:
    """ホスト単位の直列化と取得間隔の確保（アプリ全体で共有）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def lock_for(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def wait_turn(self, host: str, interval: float, stop: Callable[[], bool] | None = None) -> None:
        while True:
            with self._lock:
                last = self._last.get(host, 0.0)
                remaining = last + interval - time.monotonic()
                if remaining <= 0:
                    self._last[host] = time.monotonic()
                    return
            if stop and stop():
                return
            time.sleep(min(remaining, 0.5))


THROTTLE = HostThrottle()


class Cache:
    """取得キャッシュの保存先インターフェース（DB 実装は repo 側）。"""

    def get(self, url: str, max_age_hours: float) -> Optional[FetchResult]:
        return None

    def put(self, result: FetchResult) -> None:
        return None


def proxies_active() -> bool:
    proxies = urllib.request.getproxies()
    return bool(proxies.get("https") or proxies.get("http"))


class Fetcher:
    def __init__(
        self,
        settings: FetchSettings,
        cache: Cache | None = None,
        throttle: HostThrottle = THROTTLE,
        transport: httpx.BaseTransport | None = None,
        resolve_dns: bool = True,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or Cache()
        self.throttle = throttle
        self.resolve_dns = resolve_dns
        self.stop = stop
        self._robots: dict[str, tuple[float, Optional[robotparser.RobotFileParser], str]] = {}
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                     "Accept-Language": "en-GB,en;q=0.8"},
            follow_redirects=False,
            timeout=httpx.Timeout(settings.timeout_sec),
            transport=transport,
            trust_env=True,
        )
        self.check_peer = transport is None and not proxies_active()

    def close(self) -> None:
        self.client.close()

    # -- robots.txt -------------------------------------------------------
    def robots_allows(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(base)
        if cached and time.time() - cached[0] < 3600:
            parser, note = cached[1], cached[2]
        else:
            res = self._get(base + "/robots.txt", accept_any_type=True, use_cache=True)
            if res.error and res.status_code == 0:
                parser, note = None, f"robots.txt を取得できないため取得を見送りました（{res.error}）"
            elif res.status_code >= 500 or res.status_code == 429:
                parser, note = None, f"robots.txt が一時エラー（HTTP {res.status_code}）のため取得を見送りました"
            elif res.status_code >= 400:
                parser, note = robotparser.RobotFileParser(), ""  # robots.txt なし＝制限なし
                parser.parse([])
            else:
                parser = robotparser.RobotFileParser()
                parser.parse(res.text.splitlines())
                note = ""
            self._robots[base] = (time.time(), parser, note)
        if parser is None:
            return False, note
        if not parser.can_fetch(ROBOTS_TOKEN, url):
            return False, "robots.txt で取得が許可されていません"
        return True, ""

    # -- 取得本体 ---------------------------------------------------------
    def fetch(self, url: str) -> FetchResult:
        try:
            assert_safe_url(url, resolve=self.resolve_dns)
        except UnsafeURLError as exc:
            return FetchResult(url=url, error=str(exc))
        allowed, note = self.robots_allows(url)
        if not allowed:
            return FetchResult(url=url, error=note)
        return self._get(url, use_cache=True)

    def _get(self, url: str, accept_any_type: bool = False, use_cache: bool = True) -> FetchResult:
        s = self.settings
        if use_cache and s.cache_hours > 0:
            hit = self.cache.get(url, s.cache_hours)
            if hit:
                hit.from_cache = True
                return hit
        attempt = 0
        while True:
            result = self._get_once(url, accept_any_type)
            retryable = (result.status_code == 0 and "接続" in result.error) or result.status_code in (500, 502, 503, 504)
            if not retryable or attempt >= s.max_retries or (self.stop and self.stop()):
                break
            attempt += 1
            time.sleep(min(2 * attempt, 10))
        if use_cache and (result.status_code or not result.error):
            self.cache.put(result)
        return result

    def _get_once(self, url: str, accept_any_type: bool) -> FetchResult:
        s = self.settings
        current = url
        for _ in range(s.max_redirects + 1):
            try:
                assert_safe_url(current, resolve=self.resolve_dns)
            except UnsafeURLError as exc:
                return FetchResult(url=url, final_url=current, error=f"転送先を拒否: {exc}")
            host = (urlsplit(current).hostname or "").lower()
            with self.throttle.lock_for(host):
                self.throttle.wait_turn(host, s.min_interval_sec, self.stop)
                try:
                    with self.client.stream("GET", current) as resp:
                        if self.check_peer:
                            peer_err = self._check_peer(resp)
                            if peer_err:
                                return FetchResult(url=url, final_url=current, error=peer_err)
                        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                            current = urljoin(current, resp.headers["location"])
                            continue
                        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                        result = FetchResult(url=url, final_url=str(resp.url), status_code=resp.status_code,
                                             content_type=ctype)
                        if _is_challenge(resp):
                            result.error = "アクセス制限（CAPTCHA・ボット確認等）が表示されたため取得しませんでした"
                            return result
                        if resp.status_code == 429:
                            result.error = "アクセスが多すぎると応答されたため中止しました（HTTP 429）"
                            return result
                        if resp.status_code in (401, 403):
                            result.error = (f"HTTP {resp.status_code}（アクセス拒否。ボット対策の可能性があるため回避せず、"
                                            "ブラウザーでの手動確認が必要）")
                            return result
                        if resp.status_code >= 400:
                            result.error = f"HTTP {resp.status_code}"
                            return result
                        if not accept_any_type and ctype and ctype not in HTML_TYPES:
                            result.error = f"HTML以外の内容のため読み取りません（{ctype}）"
                            return result
                        body = bytearray()
                        for chunk in resp.iter_bytes():
                            body.extend(chunk)
                            if len(body) > s.max_bytes:
                                result.truncated = True
                                body = body[: s.max_bytes]
                                break
                        encoding = resp.encoding or "utf-8"
                        try:
                            result.text = bytes(body).decode(encoding, errors="replace")
                        except LookupError:
                            result.text = bytes(body).decode("utf-8", errors="replace")
                        if CHALLENGE_TITLE.search(result.text[:5000]):
                            result.text = ""
                            result.error = "アクセス制限（ボット確認ページ）が表示されたため取得しませんでした"
                        return result
                except httpx.TimeoutException:
                    return FetchResult(url=url, final_url=current, error="接続がタイムアウトしました")
                except httpx.HTTPError as exc:
                    return FetchResult(url=url, final_url=current, error=f"接続エラー: {type(exc).__name__}")
        return FetchResult(url=url, final_url=current, error="転送が多すぎるため中止しました")

    @staticmethod
    def _check_peer(resp: httpx.Response) -> str:
        stream = resp.extensions.get("network_stream")
        if stream is None:
            return ""
        try:
            addr = stream.get_extra_info("server_addr")
        except Exception:
            return ""
        if addr and not is_public_ip(str(addr[0]).split("%", 1)[0]):
            return f"実際の接続先が内部アドレス（{addr[0]}）のため中止しました"
        return ""


def _is_challenge(resp: httpx.Response) -> bool:
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if resp.status_code in (403, 503) and "captcha" in resp.headers.get("server", "").lower():
        return True
    return False


def compress(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def decompress(blob: bytes) -> str:
    return gzip.decompress(blob).decode("utf-8")
