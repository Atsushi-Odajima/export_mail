"""候補発見の試験（架空データ・外部には接続しない）。"""
import json

import httpx
import pytest

from sdr import discovery, repo
from sdr.jobs import JobManager

OSM = {"elements": [
    {"type": "node", "id": 1, "tags": {"shop": "car", "name": "Alpha Cars", "website": "https://alpha-cars.example/", "second_hand": "only"}},
    {"type": "node", "id": 2, "tags": {"shop": "car", "name": "Brand Motors", "website": "https://brand.example/", "brand": "Ford"}},
    {"type": "way", "id": 3, "tags": {"shop": "car", "name": "New Only", "website": "https://new-only.example/", "second_hand": "no"}},
    {"type": "node", "id": 4, "tags": {"shop": "car", "name": "Listing", "website": "https://www.autotrader.co.uk/dealers/x"}},
    {"type": "node", "id": 5, "tags": {"shop": "car", "name": "Alpha dup", "contact:website": "http://www.alpha-cars.example"}},
]}


def test_parse_overpass_filters_and_hints():
    found = discovery.parse_overpass(OSM, 10)
    urls = [f.url for f in found]
    assert urls == ["https://alpha-cars.example/", "https://brand.example/"]
    assert "second_hand=only" in found[0].hint
    assert "正規ディーラーの可能性" in found[1].hint


def test_discover_osm_uses_fallback_endpoint():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(504, text="busy")
        return httpx.Response(200, json=OSM)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = discovery.discover_osm("Leeds（リーズ）", 5, client=client)
    assert len(calls) == 2 and len(found) == 2
    assert calls[0] == discovery.OVERPASS_ENDPOINTS[0] and calls[1] == discovery.OVERPASS_ENDPOINTS[1]


def test_firecrawl_results_and_payment_required():
    def ok(request):
        assert request.headers["authorization"] == "Bearer fc-test"
        body = json.loads(request.content)
        assert body["country"] == "GB" and body["limit"] == 10
        return httpx.Response(200, json={"success": True, "creditsUsed": 2, "data": {"web": [
            {"url": "https://classic-dealer.example/", "title": "Classic Dealer", "description": "Classic cars for sale"},
            {"url": "https://www.carandclassic.com/car/1", "title": "listing"}]}})

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        found, credits = discovery.discover_firecrawl("fc-test", "クラシックカー", "Leeds（リーズ）", 10, client=client)
    assert [f.url for f in found] == ["https://classic-dealer.example/"] and credits == 2

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(402, json={}))) as client:
        with pytest.raises(discovery.DiscoveryError, match="切り替えません"):
            discovery.discover_firecrawl("fc-test", "一般中古車", "Leeds（リーズ）", 10, client=client)
    with pytest.raises(discovery.DiscoveryError, match="APIキー"):
        discovery.discover_firecrawl("", "一般中古車", "Leeds（リーズ）", 10)


def test_firecrawl_monthly_cap_blocks_before_request(paths, conn, monkeypatch):
    called = []
    monkeypatch.setattr(discovery, "discover_firecrawl", lambda *a, **k: called.append(1) or ([], 0))
    repo.set_setting(conn, "firecrawl_monthly_credit_cap", "4")
    repo.record_api_usage(conn, "firecrawl", 3, "前回")
    jm = JobManager(paths.db, paths.secrets)
    jid = repo.create_job(conn, "discover", [], {})
    jm._run_discovery(jid, "firecrawl", "Leeds（リーズ）", "一般中古車", 10)
    job = repo.job_view(conn, jid)
    assert job["status"] == "失敗" and "上限" in job["message"] and not called


def test_candidates_are_deduplicated_and_registered(conn):
    cid, note = repo.add_candidate(conn, "https://alpha-cars.example/", "OpenStreetMap", "Alpha Cars")
    assert cid and note == "候補に追加"
    assert repo.add_candidate(conn, "alpha-cars.example", "手動URL")[1] == "候補に登録済み"
    assert repo.add_candidate(conn, "https://www.gumtree.com/x", "Firecrawl")[0] is None
    ids = repo.register_candidates(conn, [cid])
    assert len(ids) == 1 and repo.get_dealer(conn, ids[0])["data_origin"] == "候補発見"
    assert repo.list_candidates(conn, "登録済み")[0]["dealer_id"] == ids[0]
