import json

import pytest
from fastapi.testclient import TestClient

from app.bilibili import BiliError
from app.server import create_app
from app.textnorm import to_simplified
from tests.test_pipeline import FakeClient, modern_family


class FakeResolver:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    async def resolve(self, query):
        q = to_simplified(query.strip())
        return list(self.mapping.get(q.lower(), [q]))

    async def aclose(self):
        pass


class FailingClient(FakeClient):
    async def search(self, keyword, order="click", duration=0, page=1, patient=True):
        raise BiliError("B 站暫時限制查詢，請稍後再試")


class BrokenClient(FakeClient):
    async def search(self, keyword, order="click", duration=0, page=1, patient=True):
        raise RuntimeError("boom")


def lines(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def serve(fake, resolver=None):
    return TestClient(create_app(client=fake, resolver=resolver or FakeResolver()))


@pytest.fixture
def fake():
    hits, details = modern_family()
    return FakeClient(hits, details)


def test_empty_query_is_error_without_calling_bilibili(fake):
    with serve(fake) as tc:
        out = lines(tc.get("/api/search", params={"q": "   "}))
    assert out == [{"type": "error", "message": "請輸入劇名"}]
    assert fake.search_calls == []


def test_search_streams_progress_then_result(fake):
    with serve(fake) as tc:
        resp = tc.get("/api/search", params={"q": "摩登家庭"})
    out = lines(resp)
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["cache-control"] == "no-store"
    assert out[0]["type"] == "progress" and out[-1]["type"] == "result"
    result = out[-1]
    assert result["cached"] is False and result["names"] == ["摩登家庭"]
    assert [i["bvid"] for i in result["complete"]] == ["B", "A"]
    assert result["complete"][0]["url"] == "https://www.bilibili.com/video/B"
    assert len(result["uncertain"]) == 1 and len(result["excluded"]) == 2
    assert result["fetched_at"] > 0


def test_second_identical_search_is_cached_including_traditional_spelling(fake):
    with serve(fake) as tc:
        tc.get("/api/search", params={"q": "摩登家庭"})
        n = len(fake.search_calls)
        out = lines(tc.get("/api/search", params={"q": "摩登家庭 "}))
    assert out[-1]["type"] == "result" and out[-1]["cached"] is True
    assert len(fake.search_calls) == n


def test_english_and_chinese_queries_share_names_and_cache(fake):
    resolver = FakeResolver({"modern family": ["摩登家庭", "Modern Family"],
                             "摩登家庭": ["摩登家庭", "Modern Family"]})
    with serve(fake, resolver) as tc:
        first = lines(tc.get("/api/search", params={"q": "modern family"}))[-1]
        second = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
    assert first["names"] == ["摩登家庭", "Modern Family"]
    assert first["cached"] is False and second["cached"] is True
    assert [i["bvid"] for i in first["complete"]] == [i["bvid"] for i in second["complete"]]


def test_bili_error_becomes_error_event_not_empty_result():
    with serve(FailingClient()) as tc:
        out = lines(tc.get("/api/search", params={"q": "x"}))
    assert out[-1] == {"type": "error", "message": "B 站暫時限制查詢，請稍後再試"}
    assert all(e["type"] != "result" for e in out)


def test_unexpected_error_is_reported_generically():
    with serve(BrokenClient()) as tc:
        out = lines(tc.get("/api/search", params={"q": "x"}))
    assert out[-1] == {"type": "error", "message": "發生未預期的錯誤"}


def test_failed_search_is_not_cached():
    client = FailingClient()
    with serve(client) as tc:
        tc.get("/api/search", params={"q": "x"})
        out = lines(tc.get("/api/search", params={"q": "x"}))
    assert out[-1]["type"] == "error"


def test_latest_refresh_bypasses_cache(fake):
    with serve(fake) as tc:
        first = lines(tc.get("/api/latest"))[-1]
        n = len(fake.search_calls)
        second = lines(tc.get("/api/latest"))[-1]
        assert len(fake.search_calls) == n
        tc.get("/api/latest", params={"refresh": 1})
        assert len(fake.search_calls) > n
    assert (first["type"], first["cached"], second["cached"]) == ("result", False, True)


def test_index_served(fake):
    with serve(fake) as tc:
        assert "B站完整美劇" in tc.get("/").text


def test_partial_result_is_flagged_and_not_cached():
    hits, details = modern_family()
    client = FakeClient(hits, details, fail_on={2})
    with serve(client) as tc:
        first = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
        second = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
    assert first["partial"] is True and first["cached"] is False
    assert second["cached"] is False and second["partial"] is False
