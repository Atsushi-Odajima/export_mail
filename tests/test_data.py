"""保存・ID維持・手動修正の保持・移行・バックアップ・CSV・ジョブの試験（架空データ）。"""
import json
import sqlite3
import time
import zipfile

import pytest

from sdr import backup, csv_io, repo, seed
from sdr.db import MIGRATIONS, MigrationError, connect, current_version, latest_version, migrate

LATEST = latest_version()
from sdr.jobs import JobManager
from sdr.research import research_site
from conftest import FakeWeb, dealer_site, make_fetcher

BASE = "https://www.test-motors.example"


def run_research(conn, did, web):
    rid = repo.start_run(conn, did, None)
    res = research_site(repo.get_dealer(conn, did)["website_url"], make_fetcher(web))
    repo.apply_research(conn, did, rid, res)
    return res


def test_duplicate_domain_not_registered_twice(conn):
    did, created, _ = repo.create_dealer(conn, BASE + "/", "テスト")
    did2, created2, note = repo.create_dealer(conn, "test-motors.example/used-cars", "テスト")
    assert created and not created2 and did == did2 and "登録済み" in note
    with pytest.raises(repo.RepoError):
        repo.create_dealer(conn, "https://www.autotrader.co.uk/dealers/x", "テスト")
    with pytest.raises(repo.RepoError):
        repo.create_dealer(conn, "https://cars.example.ie/", "テスト")


def test_similar_name_goes_to_review_not_merged(conn):
    a, _, _ = repo.create_dealer(conn, "https://a-site.example/", "テスト", name="Smith Motors Ltd")
    b, created, _ = repo.create_dealer(conn, "https://b-site.example/", "テスト", name="Smith Motors")
    assert created and a != b
    assert repo.get_dealer(conn, b)["duplicate_status"] == "重複確認待ち"
    assert repo.get_dealer(conn, a)["duplicate_status"] is None


def test_id_kept_and_manual_values_survive_reresearch(conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト")
    web = FakeWeb(dealer_site())
    run_research(conn, did, web)
    d = repo.get_dealer(conn, did)
    assert d["name"] == "Test Motors Ltd" and d["has_email"] and d["has_form"]
    contact_ids = {c["value"]: c["id"] for c in d["contacts"]}

    # 人が店名・分類・概要を確定し、URL も変更
    conn.execute("BEGIN")
    repo.update_manual(conn, did, {"name": "Test Motors (確認済み名称)", "categories": ["スポーツカー"],
                                   "summary": "人が書いた概要", "website_url": "https://test-motors-new.example/"}, "tester")
    conn.execute("COMMIT")
    repo.set_contact_status(conn, contact_ids["sales@test-motors.example"], "要手動確認", "tester", "電話で確認予定")
    # 新しい URL でも同じサイト構成で再調査（自動取得値は更新されるが確定値は残る）
    new_web = FakeWeb(dealer_site("https://test-motors-new.example"))
    new_web.pages["https://test-motors-new.example/contact-us"] = (200, new_web.pages["https://test-motors-new.example/contact-us"][1]
                                                                  .replace("sales@test-motors.example", "sales2@test-motors.example"))
    run_research(conn, did, new_web)
    d = repo.get_dealer(conn, did)
    assert d["id"] == did
    assert d["name"] == "Test Motors (確認済み名称)" and d["name_auto"] == "Test Motors Ltd"
    assert d["categories"] == ["スポーツカー"] and d["summary"] == "人が書いた概要"
    by_value = {c["value"]: c for c in d["contacts"]}
    old = by_value["sales@test-motors.example"]
    assert old["id"] == contact_ids["sales@test-motors.example"]  # 連絡方法の ID も維持
    assert old["status"] == "要手動確認" and old["status_is_manual"]  # 人の確定は上書きしない
    assert not old["is_current"]  # 古い値は消さずに「未検出」として残す
    assert by_value["sales2@test-motors.example"]["is_current"]
    # 根拠は正しい販売店・連絡方法に関連付く
    ev = repo.list_evidence(conn, did)
    assert all(e["dealer_id"] == did for e in ev)
    assert any(e["contact_method_id"] == old["id"] for e in ev)
    hist = repo.list_history(conn, did)
    assert any(h["field"] == "website_url" and h["origin"] == "手動" for h in hist)


def test_multiple_contacts_per_dealer_and_manual_person(conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト")
    c1 = repo.add_contact_manual(conn, did, "email", "buyer@test-motors.example", BASE + "/team", "スタッフ紹介に記載",
                                 "Jane Example", "Sales Manager", "tester")
    c2 = repo.add_contact_manual(conn, did, "phone", "+44 113 000 0000", None, None)
    contacts = repo.list_contacts(conn, did)
    assert {c["id"] for c in contacts} == {c1, c2}
    assert contacts[0]["dealer_id"] == did
    with pytest.raises(repo.RepoError):
        repo.add_contact_manual(conn, did, "email", "not-an-email", None, None)


def test_failed_research_keeps_previous_data(conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト")
    run_research(conn, did, FakeWeb(dealer_site()))
    run_research(conn, did, FakeWeb({f"{BASE}/robots.txt": (404, ""), f"{BASE}/": (503, "down")}))
    d = repo.get_dealer(conn, did)
    assert d["research_status"] == "取得失敗" and "HTTP 503" in d["failure_reason"]
    assert d["has_email"] and d["name"] == "Test Motors Ltd"


# --------------------------------------------------------------------------- 移行
def test_migration_adds_column_and_keeps_data(paths, conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト", name="Keep Me")
    conn.close()

    def v2(c):
        c.execute("ALTER TABLE dealers ADD COLUMN test_extra TEXT")

    log = migrate(paths.db, paths.backups, MIGRATIONS + [(LATEST + 1, "試験用の列追加", v2)])
    assert any("更新前のバックアップ" in l for l in log)
    assert list(paths.backups.glob(f"pre-migration-v{LATEST}-*.sqlite3"))
    c = connect(paths.db)
    try:
        assert current_version(c) == LATEST + 1
        row = c.execute("SELECT name_manual, test_extra FROM dealers WHERE id=?", (did,)).fetchone()
        assert row["name_manual"] == "Keep Me" and row["test_extra"] is None
    finally:
        c.close()


def test_failed_migration_rolls_back(paths, conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト", name="Keep Me")
    conn.close()

    def bad(c):
        c.execute("ALTER TABLE dealers ADD COLUMN half_done TEXT")
        raise RuntimeError("意図的な失敗")

    with pytest.raises(MigrationError):
        migrate(paths.db, paths.backups, MIGRATIONS + [(LATEST + 1, "失敗する移行", bad)])
    c = connect(paths.db)
    try:
        assert current_version(c) == LATEST
        cols = [r[1] for r in c.execute("PRAGMA table_info(dealers)")]
        assert "half_done" not in cols
        assert c.execute("SELECT name_manual FROM dealers WHERE id=?", (did,)).fetchone()[0] == "Keep Me"
    finally:
        c.close()


def test_newer_database_refused(paths, conn):
    conn.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(MigrationError):
        migrate(paths.db, paths.backups)


# --------------------------------------------------------------------------- バックアップ
def test_backup_and_restore_keep_ids_manual_evidence_history(paths, conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト")
    run_research(conn, did, FakeWeb(dealer_site()))
    conn.execute("BEGIN")
    repo.update_manual(conn, did, {"summary": "確定した概要"}, "tester")
    conn.execute("COMMIT")
    before = repo.get_dealer(conn, did)
    ev_count = len(repo.list_evidence(conn, did))
    hist_count = len(repo.list_history(conn, did))
    (paths.secrets).write_text(json.dumps({"firecrawl_api_key": "fc-secret"}))
    zp = backup.create_backup(paths.db, paths.backups)
    with zipfile.ZipFile(zp) as zf:
        assert set(zf.namelist()) == {"dealers.sqlite3", "manifest.json"}
        assert b"fc-secret" not in zf.read("dealers.sqlite3")
    # 変更・削除してから復元
    conn.execute("DELETE FROM evidence")
    conn.execute("UPDATE dealers SET summary_manual='壊れた値'")
    conn.close()
    manifest, pre = backup.restore_backup(zp, paths.db, paths.backups)
    assert pre.exists() and manifest["schema_version"] == LATEST
    c = connect(paths.db)
    try:
        after = repo.get_dealer(c, did)
        assert after["summary"] == "確定した概要"
        assert [x["id"] for x in after["contacts"]] == [x["id"] for x in before["contacts"]]
        assert len(repo.list_evidence(c, did)) == ev_count
        assert len(repo.list_history(c, did)) >= hist_count
        assert c.execute("SELECT COUNT(*) FROM fetch_cache").fetchone()[0] == 0
    finally:
        c.close()


def test_restore_rejects_foreign_zip(tmp_path, paths):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("evil.txt", "x")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad, paths.db, paths.backups)


def test_backup_includes_attachments_and_rejects_path_tricks(tmp_path, paths, conn):
    attach = paths.data_dir / "attachments"
    attach.mkdir()
    (attach / "ATT-01TEST").write_bytes(b"%PDF-1.4 brochure")
    zp = backup.create_backup(paths.db, paths.backups)
    with zipfile.ZipFile(zp) as zf:
        assert "attachments/ATT-01TEST" in zf.namelist()
    (attach / "ATT-01TEST").unlink()
    conn.close()
    backup.restore_backup(zp, paths.db, paths.backups)
    assert (attach / "ATT-01TEST").read_bytes() == b"%PDF-1.4 brochure"
    # 復元先の外へ書き出す名前は受け付けない
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp) as src, zipfile.ZipFile(evil, "w") as zf:
        for n in src.namelist():
            zf.writestr(n, src.read(n))
        zf.writestr("attachments/../../evil", b"x")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(evil, paths.db, paths.backups)


# --------------------------------------------------------------------------- CSV
def test_csv_export_is_excel_safe(conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト", name="=HYPERLINK(\"http://evil\")", notes="+cmd|' /C calc'!A0")
    data = csv_io.dealers_csv(repo.list_dealers(conn))
    assert data.startswith(b"\xef\xbb\xbf")  # BOM
    text = data.decode("utf-8-sig")
    assert "\"'=HYPERLINK" in text and "\"'+cmd" in text
    assert "\r\n" in text and "販売店ID" in text and "店名" in text


def test_csv_import_parsing():
    rows, errors = csv_io.parse_import("URL,店名,地域\r\nhttps://a.example/,A Cars,Leeds\r\n,空行,\r\n".encode("utf-8-sig"))
    assert rows == [{"line": 2, "url": "https://a.example/", "name": "A Cars", "region": "Leeds", "note": ""}]
    rows, _ = csv_io.parse_import("公式サイト\r\nb.example\r\n".encode("cp932"))
    assert rows[0]["url"] == "b.example"
    rows, _ = csv_io.parse_import(b"https://c.example/\nhttps://d.example/\n")
    assert [r["url"] for r in rows] == ["https://c.example/", "https://d.example/"]


# --------------------------------------------------------------------------- 初期データ
def test_seed_import_does_not_overwrite(tmp_path, conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "初期調査データ")
    run_research(conn, did, FakeWeb(dealer_site()))
    seed_file = tmp_path / "seed.json"
    seed.export_seed(conn, [did], seed_file)
    conn.execute("BEGIN")
    repo.update_manual(conn, did, {"notes": "利用者のメモ"})
    conn.execute("COMMIT")
    assert seed.import_seed(conn, seed_file) == 0
    assert repo.get_dealer(conn, did)["notes"] == "利用者のメモ"


def test_seed_import_into_empty_db(tmp_path, paths, conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "初期調査データ")
    run_research(conn, did, FakeWeb(dealer_site()))
    seed_file = tmp_path / "seed.json"
    seed.export_seed(conn, [did], seed_file)
    other = tmp_path / "other.sqlite3"
    migrate(other, tmp_path / "bk")
    c2 = connect(other)
    try:
        assert seed.import_seed(c2, seed_file) == 1
        d = repo.get_dealer(c2, did)
        assert d["id"] == did and d["has_email"] and d["data_origin"] == "初期調査データ"
        assert repo.list_evidence(c2, did)
    finally:
        c2.close()


# --------------------------------------------------------------------------- ジョブ
def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_job_runs_stops_and_resumes(paths, conn):
    sites = {}
    ids = []
    for i in range(4):
        base = f"https://dealer{i}.example"
        sites.update(dealer_site(base))
        ids.append(repo.create_dealer(conn, base + "/", "テスト")[0])
    web = FakeWeb(sites)
    jm = JobManager(paths.db, paths.secrets, transport=web.transport(), resolve_dns=False)
    jm.min_interval_floor = 0.0
    repo.set_setting(conn, "min_interval_sec", "2")
    jm.start()  # 起動時の中断復旧はジョブ作成より前（アプリ起動時と同じ順序）
    jid = repo.create_job(conn, "research", ids)
    jm.stop(jid)  # 実行中の1件があれば「停止要求」→ページ取得の合間で止まり「停止」になる
    assert _wait(lambda: repo.job_view(conn, jid)["status"] == "停止", 30)
    counts = repo.job_view(conn, jid)["counts"]
    assert counts.get("停止", 0) >= 3 and counts.get("停止", 0) + counts.get("完了", 0) == 4
    jm.resume(jid)
    try:
        assert _wait(lambda: repo.job_view(conn, jid)["status"] == "完了", 60)
    finally:
        jm.shutdown()
    job = repo.job_view(conn, jid)
    assert job["counts"] == {"完了": 4}
    assert all(repo.get_dealer(conn, d)["research_status"] == "完了" for d in ids)


def test_restart_recovers_interrupted(paths, conn):
    did, _, _ = repo.create_dealer(conn, BASE + "/", "テスト")
    jid = repo.create_job(conn, "research", [did])
    repo.claim_next_item(conn)  # 実行中のままアプリが落ちた状態
    n = repo.recover_interrupted(conn)
    assert n == 1
    job = repo.job_view(conn, jid)
    assert job["status"] == "中断" and job["counts"] == {"待機": 1}
    assert repo.get_dealer(conn, did)["research_status"] == "未調査"
