"""専用ウィンドウ版の起動まわりの試験（画面の表示そのものは Windows の CI で確認する）。"""
import json
import sys

import pytest
from fastapi.testclient import TestClient

from sdr import desktop, main
from sdr.web import create_app


@pytest.fixture
def app(paths):
    return create_app(paths, mode="local", start_jobs=False, load_seed=False)


def test_focus_requires_token(app):
    calls = []
    with TestClient(app) as c:
        assert c.post("/desktop/focus").status_code == 404  # ウィンドウ版でなければ無効
        app.state.focus_token = "secret-token"
        app.state.focus_cb = lambda: calls.append(1)
        assert c.post("/desktop/focus").status_code == 404
        assert c.post("/desktop/focus", headers={"x-sdr-focus-token": "wrong"}).status_code == 404
        assert c.post("/desktop/focus", headers={"x-sdr-focus-token": "secret-token"}).status_code == 200
    assert calls == [1]


def test_focus_disabled_in_shared_mode(paths):
    app = create_app(paths, mode="shared", secure_cookies=True, start_jobs=False, load_seed=False)
    app.state.focus_token = "secret-token"
    app.state.focus_cb = lambda: None
    with TestClient(app) as c:
        r = c.post("/desktop/focus", headers={"x-sdr-focus-token": "secret-token"})
        assert r.status_code in (401, 404)


def test_default_command(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\SamuraiDealerResearch\SamuraiDealerResearch.exe")
    assert main.default_command() == "desktop"
    monkeypatch.setattr(sys, "executable", r"C:\Apps\SamuraiDealerResearch\SamuraiDealerResearch-cli.exe")
    assert main.default_command() == "local"
    monkeypatch.delattr(sys, "frozen")
    assert main.default_command() == "local"


def test_webview_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop.load_webview() is None


def test_research_running(paths):
    from sdr.db import connect, migrate

    migrate(paths.db, paths.backups)
    assert desktop.research_running(paths) is False
    conn = connect(paths.db)
    try:
        from sdr import repo

        did, _, _ = repo.create_dealer(conn, "https://www.test-motors.example/", "テスト")
        repo.create_job(conn, "research", [did], {"label": "t"})
    finally:
        conn.close()
    assert desktop.research_running(paths) is True


def test_no_existing_instance(paths):
    assert desktop.focus_existing(paths) is False
    (paths.data_dir / main.LOCK_NAME).write_text(json.dumps({"port": 1, "focus_token": "x"}), encoding="utf-8")
    assert desktop.focus_existing(paths) is False  # 応答しない古い印は無視する


def test_windowed_stdout_is_replaced(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with pytest.raises(SystemExit):
        main.main(["--data-dir", str(tmp_path / "d"), "--help"])
    assert sys.stdout is not None and sys.stderr is not None
