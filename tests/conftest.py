"""試験用の共通部品。

ここで使う販売店・サイト・メールアドレスはすべて「試験用の架空データ」で、実在しない。
（.example ドメインは予約済みで実在しない）
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdr.config import Paths  # noqa: E402
from sdr.db import connect, migrate  # noqa: E402
from sdr.fetcher import Fetcher, FetchSettings, HostThrottle  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class FakeWeb:
    """URL → (ステータス, 本文, 追加ヘッダー) の表で応答する偽のインターネット。"""

    def __init__(self, pages: dict[str, tuple[int, str] | tuple[int, str, dict]] | None = None):
        self.pages = dict(pages or {})
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        entry = self.pages.get(url) or self.pages.get(url.rstrip("/")) or self.pages.get(url + "/")
        if entry is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        status, body, *rest = entry
        headers = {"content-type": "text/html; charset=utf-8"}
        if rest:
            headers.update(rest[0])
        return httpx.Response(status, text=body, headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def dealer_site(base: str = "https://www.test-motors.example") -> dict:
    """架空の中古車販売店サイト一式。"""
    return {
        f"{base}/robots.txt": (200, "User-agent: *\nDisallow: /private/\n", {"content-type": "text/plain"}),
        f"{base}/": (200, fixture_text("home.html")),
        f"{base}/contact-us": (200, fixture_text("contact.html")),
        f"{base}/about-us": (200, fixture_text("about.html")),
        f"{base}/used-cars": (200, fixture_text("stock.html")),
        f"{base}/private/secret": (200, "<html>must not be fetched</html>"),
    }


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "data")
    p.ensure()
    migrate(p.db, p.backups)
    return p


@pytest.fixture
def conn(paths):
    c = connect(paths.db)
    yield c
    c.close()


def make_fetcher(web: FakeWeb, **kw) -> Fetcher:
    settings = FetchSettings(min_interval_sec=0.0, timeout_sec=5, max_bytes=kw.pop("max_bytes", 2_000_000),
                             max_retries=0, cache_hours=0)
    return Fetcher(settings, transport=web.transport(), throttle=HostThrottle(), resolve_dns=False, **kw)
