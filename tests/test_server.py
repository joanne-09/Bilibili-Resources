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

    async def resolve_full(self, query):
        return await self.resolve(query), "tv"

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


def test_partial_result_is_flagged_and_only_briefly_cached():
    hits, details = modern_family()
    client = FakeClient(hits, details, fail_on={2})
    with serve(client) as tc:
        first = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
        second = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
    assert first["partial"] is True and first["cached"] is False
    # an immediate retry must not hammer Bilibili again while it is pushing back
    assert second["cached"] is True and second["partial"] is True
    assert len(client.search_calls) == 2


# ---- catalog, movie mode, backfill -----------------------------------------

from app.tmdb import Title, TmdbError  # noqa: E402
from tests.test_pipeline import detail, dune, hit  # noqa: E402


class FakeTmdb:
    def __init__(self, configured=True, error=None, runtime=155):
        self.configured, self.error, self._runtime, self.calls = configured, error, runtime, []

    async def available(self, platform, kind, page=1, genre=None):
        self.calls.append(("available", platform, kind, page))
        if self.error:
            raise self.error
        if platform not in ("netflix", "hbo", "prime") or kind not in ("tv", "movie"):
            raise ValueError("bad")
        return [Title(1396, kind, "绝命毒师", "Breaking Bad", "2008", "2008-01-20", "https://img/p.jpg")], True

    async def upcoming(self, platform, kind, genre=None):
        self.calls.append(("upcoming", platform, kind))
        if self.error:
            raise self.error
        return [Title(5, kind, "镀金时代", "The Gilded Age", "2022", "2026-11-15", "", season=4)]

    async def find_movie_runtime(self, names):
        return None

    async def runtime(self, movie_id):
        self.calls.append(("runtime", movie_id))
        if self.error:
            raise self.error
        return self._runtime

    async def aclose(self):
        pass


class CountingResolver(FakeResolver):
    calls = 0

    async def resolve(self, query):
        CountingResolver.calls += 1
        return await super().resolve(query)


def serve2(fake, tmdb=None, resolver=None):
    return TestClient(create_app(client=fake, resolver=resolver or FakeResolver(),
                                 tmdb=tmdb or FakeTmdb(), backfill_interval=None))


def test_platforms_listed(fake):
    with serve2(fake) as tc:
        assert tc.get("/api/platforms").json() == [
            {"id": "netflix", "label": "Netflix"}, {"id": "hbo", "label": "HBO Max"},
            {"id": "prime", "label": "Prime Video"}]


def test_catalog_not_configured_says_so(fake):
    tmdb = FakeTmdb(configured=False)
    with serve2(fake, tmdb) as tc:
        resp = tc.get("/api/catalog", params={"platform": "netflix", "section": "available", "kind": "tv"})
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "items": [], "has_more": False}
    assert tmdb.calls == []


def test_catalog_available_and_upcoming(fake):
    with serve2(fake) as tc:
        a = tc.get("/api/catalog", params={"platform": "netflix", "section": "available", "kind": "tv", "page": 2}).json()
        u = tc.get("/api/catalog", params={"platform": "hbo", "section": "upcoming", "kind": "tv"}).json()
    assert a["configured"] is True and a["has_more"] is True
    assert a["items"][0] == {"tmdb_id": 1396, "kind": "tv", "title": "绝命毒师", "original_title": "Breaking Bad",
                             "year": "2008", "date": "2008-01-20", "poster": "https://img/p.jpg", "season": None, "display_title": ""}
    assert (u["items"][0]["date"], u["items"][0]["season"], u["has_more"]) == ("2026-11-15", 4, False)


def test_catalog_tmdb_error_is_a_502_with_the_message(fake):
    with serve2(fake, FakeTmdb(error=TmdbError("TMDB 金鑰無效，請檢查 config.local.toml"))) as tc:
        resp = tc.get("/api/catalog", params={"platform": "netflix", "section": "available", "kind": "tv"})
    assert resp.status_code == 502 and resp.json() == {"error": "TMDB 金鑰無效，請檢查 config.local.toml"}


@pytest.mark.parametrize("params", [
    {"platform": "hulu", "section": "available", "kind": "tv"},
    {"platform": "netflix", "section": "soon", "kind": "tv"},
    {"platform": "netflix", "section": "available", "kind": "anime"},
    {"platform": "netflix", "section": "available", "kind": "tv", "page": 0}])
def test_catalog_bad_params_are_400(fake, params):
    with serve2(fake) as tc:
        resp = tc.get("/api/catalog", params=params)
    assert resp.status_code == 400 and resp.json() == {"error": "參數錯誤"}


def test_search_with_alt_name_skips_the_resolver(fake):
    CountingResolver.calls = 0
    with serve2(fake, resolver=CountingResolver()) as tc:
        out = lines(tc.get("/api/search", params={"q": "摩登家庭", "alt": "Modern Family"}))[-1]
    assert out["names"] == ["摩登家庭", "Modern Family"] and CountingResolver.calls == 0
    assert out["kind"] == "tv"


def test_movie_search_from_catalog_uses_tmdb_runtime():
    hits, details = dune()
    tmdb = FakeTmdb(runtime=155)
    with serve2(FakeClient(hits, details), tmdb) as tc:
        out = lines(tc.get("/api/search", params={"q": "沙丘", "alt": "Dune", "kind": "movie", "tmdb_id": 7}))[-1]
    assert ("runtime", 7) in tmdb.calls
    assert out["kind"] == "movie" and [i["bvid"] for i in out["complete"]] == ["F1"]
    assert "F3" in [i["bvid"] for i in out["excluded"]]      # 62 min vs 155: needs the runtime to be excluded


def test_movie_search_survives_runtime_lookup_failure():
    hits, details = dune()
    with serve2(FakeClient(hits, details), FakeTmdb(error=TmdbError("x"))) as tc:
        out = lines(tc.get("/api/search", params={"q": "沙丘", "kind": "movie", "tmdb_id": 7}))[-1]
    assert out["type"] == "result" and [i["bvid"] for i in out["complete"]] == ["F1"]


def test_tv_and_movie_results_are_cached_separately():
    hits, details = dune()
    client = FakeClient(hits, details)
    with serve2(client) as tc:
        tc.get("/api/search", params={"q": "沙丘", "kind": "movie"})
        out = lines(tc.get("/api/search", params={"q": "沙丘", "kind": "tv"}))[-1]
    assert out["cached"] is False and out["kind"] == "tv"


def test_unknown_kind_is_an_error_event(fake):
    with serve2(fake) as tc:
        out = lines(tc.get("/api/search", params={"q": "x", "kind": "anime"}))
    assert out == [{"type": "error", "message": "參數錯誤"}]


def test_partial_result_lists_skipped_links_and_backfills():
    hits, details = modern_family()
    client = FakeClient(hits, details, fail_on={2})
    app = create_app(client=client, resolver=FakeResolver(), tmdb=FakeTmdb(), backfill_interval=None)
    with TestClient(app) as tc:
        first = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
        assert first["partial"] is True and first["backfill_pending"] == 3
        assert first["skipped"][0] == {
            "keyword": "摩登家庭 全集", "duration": 4,
            "url": "https://search.bilibili.com/all?keyword=%E6%91%A9%E7%99%BB%E5%AE%B6%E5%BA%AD%20%E5%85%A8%E9%9B%86"
                   "&order=click&duration=4"}
        calls = len(client.search_calls)
        again = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
        assert again["cached"] is True and len(client.search_calls) == calls   # served from the backfill job

        client.details["N"] = detail("N", "摩登家庭 全11季 蓝光 未删减", [380] * 20, play=50)
        client.by_keyword["摩登家庭 全集"] = [hit("N", "摩登家庭 全11季 蓝光 未删减", play=50)]
        for _ in range(3):
            tc.portal.call(app.state.backfill_tick)
        done = lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]
    assert [i["bvid"] for i in done["complete"]] == ["B", "N", "A"]
    assert (done["partial"], done["backfill_pending"], done["skipped"], done["cached"]) == (False, 0, [], True)


def test_search_and_latest_work_without_tmdb_config(fake):
    with serve2(fake, FakeTmdb(configured=False)) as tc:
        assert lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]["type"] == "result"
        assert lines(tc.get("/api/latest"))[-1]["type"] == "result"


# ---- automatic series/film detection ---------------------------------------

class KindResolver(FakeResolver):
    """Says every title is a film unless told otherwise."""

    def __init__(self, kind="movie"):
        super().__init__()
        self.kind = kind

    async def resolve_full(self, query):
        return await self.resolve(query), self.kind


def test_search_without_kind_uses_the_detected_kind():
    hits, details = dune()
    with serve2(FakeClient(hits, details), resolver=KindResolver("movie")) as tc:
        out = lines(tc.get("/api/search", params={"q": "沙丘"}))[-1]
    assert out["kind"] == "movie" and [i["bvid"] for i in out["complete"]] == ["F1"]


def test_explicit_kind_overrides_detection(fake):
    with serve2(fake, resolver=KindResolver("movie")) as tc:
        out = lines(tc.get("/api/search", params={"q": "摩登家庭", "kind": "tv"}))[-1]
    assert out["kind"] == "tv" and [i["bvid"] for i in out["complete"]] == ["B", "A"]


def test_detected_series_is_searched_as_tv(fake):
    with serve2(fake) as tc:
        assert lines(tc.get("/api/search", params={"q": "摩登家庭"}))[-1]["kind"] == "tv"


def test_detected_film_gets_its_runtime_looked_up_by_name():
    hits, details = dune()

    class Tmdb(FakeTmdb):
        async def find_movie_runtime(self, names):
            self.calls.append(("find", tuple(names)))
            return 155

    tmdb = Tmdb()
    with serve2(FakeClient(hits, details), tmdb, resolver=KindResolver("movie")) as tc:
        out = lines(tc.get("/api/search", params={"q": "沙丘"}))[-1]
    assert ("find", ("沙丘",)) in tmdb.calls and out["runtime"] == 155
    assert "F3" in [i["bvid"] for i in out["excluded"]]


# ---- genres -----------------------------------------------------------------

from app.tmdb import Genre  # noqa: E402


class GenreTmdb(FakeTmdb):
    async def genres(self, kind):
        self.calls.append(("genres", kind))
        if self.error:
            raise self.error
        return [Genre(18, "劇情"), Genre(35, "喜劇")]

    async def available(self, platform, kind, page=1, genre=None):
        self.calls.append(("available", platform, kind, page, genre))
        return [Title(1668, kind, "老友记", "Friends", "1994", "1994-09-22", "", display_title="六人行")], False


def test_genres_endpoint(fake):
    with serve2(fake, GenreTmdb()) as tc:
        assert tc.get("/api/genres", params={"kind": "tv"}).json() == [{"id": 18, "name": "劇情"}, {"id": 35, "name": "喜劇"}]
        assert tc.get("/api/genres", params={"kind": "anime"}).status_code == 400
    with serve2(fake, GenreTmdb(configured=False)) as tc:
        assert tc.get("/api/genres", params={"kind": "tv"}).json() == []
    with serve2(fake, GenreTmdb(error=TmdbError("x"))) as tc:
        assert tc.get("/api/genres", params={"kind": "tv"}).json() == []     # the filter is optional


def test_catalog_passes_genre_and_returns_display_title(fake):
    tmdb = GenreTmdb()
    with serve2(fake, tmdb) as tc:
        body = tc.get("/api/catalog", params={"platform": "hbo", "section": "available", "kind": "tv", "genre": 35}).json()
        bad = tc.get("/api/catalog", params={"platform": "hbo", "section": "available", "kind": "tv", "genre": "abc"})
    assert ("available", "hbo", "tv", 1, 35) in tmdb.calls
    assert (body["items"][0]["display_title"], body["items"][0]["title"]) == ("六人行", "老友记")
    assert bad.status_code in (400, 422)
