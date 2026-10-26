"""起動中のアプリに対して、テスト用接続先だけを使った一連の操作を画面と同じ HTTP で確認する（配布物の確認用）。

自社情報 → テスト用接続先 → 送信モード「テスト」 → 案件 → テンプレートで英文作成 → 承認 → 送信
→ 疑似返信 → 受信確認 → 受信一覧・ダッシュボード

外部のメールサービスには接続しない（テスト用接続先はアプリ内の「テスト用受信箱」に記録するだけ）。
宛先も架空の .example アドレスを使う。
使い方: python scripts/e2e_test_mode.py http://127.0.0.1:8799
"""
from __future__ import annotations

import re
import sys
from urllib.parse import parse_qs, urlparse

import httpx

TO_ADDRESS = "e2e-check@dealer.example"


def main(base: str) -> None:
    c = httpx.Client(base_url=base, trust_env=False, timeout=30)

    def csrf() -> str:
        return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/").text).group(1)

    def post(url: str, data: dict, expect: str | None = None) -> str:
        r = c.post(url, data={"csrf_token": csrf(), **data})
        if r.status_code != 303:
            raise SystemExit(f"{url}: 想定外の応答 {r.status_code}")
        loc = r.headers["location"]
        q = parse_qs(urlparse(loc).query)
        if q.get("err"):
            raise SystemExit(f"{url}: エラー表示 {q['err'][0]}")
        msg = q.get("msg", [""])[0]
        if expect and expect not in msg:
            raise SystemExit(f"{url}: 期待した表示「{expect}」がありません（{msg}）")
        print(f"OK {url}: {msg}")
        return urlparse(loc).path

    home = c.get("/").text
    assert "ダッシュボード" in home, "ダッシュボードが表示されません"
    post("/profile", {"company_name": "Samurai Auto (E2E check)", "sender_name": "Taro Test", "sender_title": "Export Manager",
                      "intro_en": "We export quality used cars from Japan.", "vehicles_en": "Japanese used cars",
                      "services_en": "sourcing and export paperwork", "signature_en": "Taro Test"}, "保存")
    post("/accounts/save", {"provider": "test", "label": "E2E テスト", "from_address": "me@samurai.example",
                            "from_name": "Taro Test", "daily_limit": "10", "simulate": "正常"}, "保存")
    post("/settings/send-mode", {"send_mode": "テスト"}, "テスト")

    dealer = re.search(r'href="/dealers/(DLR-[0-9A-Z]+)"', c.get("/dealers").text).group(1)
    deal_path = post("/deals/new", {"dealer_id": dealer, "title": "E2E 確認"}, "案件を作成")
    page = c.get(deal_path).text
    account = re.search(r'<select name="account_id"><option value="([^"]+)"', page).group(1)
    draft_path = post(deal_path + "/drafts/new", {"method": "template", "step": "initial", "account_id": account,
                                                  "to_address": TO_ADDRESS}, "下書きを作成")
    body = c.get(draft_path).text
    text = re.search(r'<textarea[^>]*name="body"[^>]*>(.*?)</textarea>', body, flags=re.S).group(1)
    assert "{{" not in text and "unsubscribe" in text, "英文に未入力の差し込み項目、または配信停止の案内がありません"
    post(draft_path + "/approve", {}, "承認しました")
    post(draft_path + "/send-now", {"confirm": "1"}, "受け付けました")

    box = c.get("/testbox").text
    sent_id = re.search(r'name="sent_id" value="(\d+)"', box).group(1)
    assert TO_ADDRESS in box, "テスト用受信箱に送信記録がありません"
    post("/testbox/inject", {"sent_id": sent_id, "kind": "reply"}, "入れました")
    post("/sync/now", {"next": "/inbox"}, "新しく保存 1 件")
    inbox = c.get("/inbox").text
    assert "返信" in inbox, "受信一覧に返信がありません"
    dash = c.get("/").text
    sent7 = int(re.search(r'id="stat-sent7">(\d+)<', dash).group(1))
    replies = int(re.search(r'id="stat-replies">(\d+)<', dash).group(1))
    print(f"ダッシュボード: 7日間の送信 {sent7} / 新着返信 {replies}")
    assert sent7 >= 1 and replies >= 1, "ダッシュボードの集計に反映されていません"
    print("一連の操作（テスト用接続先）OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # Windows の CI（cp1252）でも日本語を出せるように
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765")
