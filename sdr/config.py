"""保存先・既定設定。

データは書き込み可能な利用者用フォルダーに置き、アプリ本体の更新で消えないようにする。
- Windows: %LOCALAPPDATA%\\SamuraiDealerResearch
- その他:  ~/.local/share/samurai-dealer-research
環境変数 SDR_DATA_DIR で変更できる（試験・共有モード用）。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    env = os.environ.get("SDR_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SamuraiDealerResearch"
    return Path.home() / ".local" / "share" / "samurai-dealer-research"


def resource_dir() -> Path:
    """テンプレート等の同梱ファイルの場所（PyInstaller 実行時にも対応）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "sdr"
    return Path(__file__).resolve().parent


@dataclass
class Paths:
    data_dir: Path

    @property
    def db(self) -> Path:
        return self.data_dir / "dealers.sqlite3"

    @property
    def backups(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def secrets(self) -> Path:
        # APIキー等。DB・バックアップ・CSV には含めない。
        return self.data_dir / "secrets.json"

    def ensure(self) -> None:
        for p in (self.data_dir, self.backups, self.logs):
            p.mkdir(parents=True, exist_ok=True)


# 取得処理の既定値（設定画面で変更可。下限は fetcher 側でも強制する）
DEFAULT_SETTINGS = {
    "max_pages_per_site": "10",
    "min_interval_sec": "2.0",
    "global_concurrency": "2",
    "timeout_sec": "15",
    "max_bytes": "2000000",
    "max_retries": "1",
    "cache_hours": "24",
    "firecrawl_monthly_credit_cap": "200",
    "firecrawl_max_results_per_run": "20",
}

# 設定値の許容範囲（サイトに負荷をかけないため下限・上限を固定）
SETTING_LIMITS = {
    "max_pages_per_site": (1, 20),
    "min_interval_sec": (2.0, 60.0),
    "global_concurrency": (1, 4),
    "timeout_sec": (5, 60),
    "max_bytes": (100_000, 5_000_000),
    "max_retries": (0, 3),
    "cache_hours": (0, 24 * 30),
    "firecrawl_monthly_credit_cap": (0, 1000),
    "firecrawl_max_results_per_run": (1, 50),
}

USER_AGENT = "Mozilla/5.0 (compatible; SamuraiDealerResearch/1.0; small-scale business research; respects robots.txt)"
ROBOTS_TOKEN = "SamuraiDealerResearch"
