from datetime import date

import httpx
import pytest

from app.config import load_config
from app.tmdb import PLATFORMS, TmdbClient, TmdbError, TmdbNotConfigured

TODAY = date(2026, 11, 11)


class Api:
    """Fake TMDB: routes by path, records requests."""

    def __init__(self, routes=None, status=200):
        self.routes, self.status, self.requests = routes or {}, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"status_message": "nope"})
        path = request.url.path.removeprefix("/3")
        body = self.routes.get(path)
        if body is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=body)

    def last(self, suffix):
        return next(r for r in reversed(self.requests) if r.url.path.endswith(suffix))


def client(api, key="k123"):
    return TmdbClient(key, transport=httpx.MockTransport(api), today=lambda: TODAY)


TV_PAGE = {"page": 1, "total_pages": 3, "results": [
    {"id": 1396, "name": "绝命毒师", "original_name": "Breaking Bad", "first_air_date": "2008-01-20",
     "poster_path": "/bb.jpg"},
    {"id": 2, "name": "无海报", "original_name": "No Poster", "first_air_date": "", "poster_path": None}]}


# ---- config -----------------------------------------------------------------

def test_config_missing_file_means_no_key(tmp_path):
    assert load_config(tmp_path / "nope.toml").tmdb_key is None


def test_config_reads_key(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[tmdb]\napi_key = "  abc  "\n', encoding="utf-8")
    assert load_config(f).tmdb_key == "abc"


@pytest.mark.parametrize("text", ['[tmdb]\napi_key = ""\n', "[other]\nx = 1\n", "this is = = not toml ["])
def test_config_blank_missing_or_malformed_means_no_key(tmp_path, text):
    f = tmp_path / "c.toml"
    f.write_text(text, encoding="utf-8")
    assert load_config(f).tmdb_key is None


# ---- client -----------------------------------------------------------------

async def test_not_configured_raises_without_any_request():
    api = Api()
    c = client(api, key=None)
    assert c.configured is False
    with pytest.raises(TmdbNotConfigured):
        await c.available("netflix", "tv")
    assert api.requests == []


async def test_v3_key_goes_in_query_and_v4_token_in_header():
    api = Api({"/discover/tv": TV_PAGE})
    await client(api, key="k123").available("netflix", "tv")
    req = api.last("/discover/tv")
    assert req.url.params["api_key"] == "k123" and "authorization" not in req.headers

    api = Api({"/discover/tv": TV_PAGE})
    await client(api, key="eyJtoken").available("netflix", "tv")
    req = api.last("/discover/tv")
    assert req.headers["authorization"] == "Bearer eyJtoken" and "api_key" not in req.url.params


async def test_available_maps_fields_and_params():
    api = Api({"/discover/tv": TV_PAGE})
    items, has_more = await client(api).available("netflix", "tv", page=1)
    first = items[0]
    assert (first.tmdb_id, first.kind, first.title, first.original_title, first.year) == (
        1396, "tv", "绝命毒师", "Breaking Bad", "2008")
    assert first.poster == "https://image.tmdb.org/t/p/w342/bb.jpg"
    assert (items[1].year, items[1].poster) == ("", "")
    assert has_more is True
    p = api.last("/discover/tv").url.params
    assert (p["with_watch_providers"], p["watch_region"], p["with_watch_monetization_types"],
            p["sort_by"], p["language"], p["page"]) == ("8", "US", "flatrate", "popularity.desc", "zh-CN", "1")


async def test_available_movie_uses_movie_fields():
    api = Api({"/discover/movie": {"page": 2, "total_pages": 2, "results": [
        {"id": 7, "title": "沙丘", "original_title": "Dune", "release_date": "2021-09-15", "poster_path": "/d.jpg"}]}})
    items, has_more = await client(api).available("hbo", "movie", page=2)
    assert (items[0].kind, items[0].title, items[0].original_title, items[0].year) == ("movie", "沙丘", "Dune", "2021")
    assert has_more is False


async def test_provider_id_is_taken_from_tmdb_provider_list_when_present():
    api = Api({"/discover/tv": TV_PAGE, "/watch/providers/tv": {"results": [
        {"provider_id": 4242, "provider_name": "HBO Max"}, {"provider_id": 8, "provider_name": "Netflix"}]}})
    await client(api).available("hbo", "tv")
    assert api.last("/discover/tv").url.params["with_watch_providers"] == "4242"


async def test_upcoming_tv_keeps_in_window_next_episodes_sorted():
    api = Api({
        "/discover/tv": {"page": 1, "total_pages": 1, "results": [
            {"id": 1, "name": "甲", "original_name": "A", "first_air_date": "2020-01-01", "poster_path": "/a.jpg"},
            {"id": 2, "name": "乙", "original_name": "B", "first_air_date": "2026-11-20", "poster_path": None},
            {"id": 3, "name": "丙", "original_name": "C", "first_air_date": "2019-01-01", "poster_path": None},
            {"id": 4, "name": "丁", "original_name": "D", "first_air_date": "2019-01-01", "poster_path": None}]},
        "/tv/1": {"next_episode_to_air": {"air_date": "2026-12-25", "season_number": 5}},
        "/tv/2": {"next_episode_to_air": {"air_date": "2026-11-20", "season_number": 1}},
        "/tv/3": {"next_episode_to_air": None},
        "/tv/4": {"next_episode_to_air": {"air_date": "2027-06-01", "season_number": 2}}})
    items = await client(api).upcoming("netflix", "tv")
    assert [(t.title, t.date, t.season) for t in items] == [("乙", "2026-11-20", 1), ("甲", "2026-12-25", 5)]
    p = api.last("/discover/tv").url.params
    assert (p["with_networks"], p["air_date.gte"], p["air_date.lte"]) == ("213", "2026-11-11", "2027-02-09")


async def test_upcoming_tv_joins_multiple_networks():
    api = Api({"/discover/tv": {"page": 1, "total_pages": 1, "results": []}})
    assert await client(api).upcoming("hbo", "tv") == []
    assert api.last("/discover/tv").url.params["with_networks"] == "|".join(map(str, PLATFORMS["hbo"].network_ids))


async def test_upcoming_movie_params_and_dates():
    api = Api({"/discover/movie": {"page": 1, "total_pages": 1, "results": [
        {"id": 9, "title": "新片", "original_title": "New", "release_date": "2026-12-01", "poster_path": None}]}})
    items = await client(api).upcoming("prime", "movie")
    assert [(t.title, t.date) for t in items] == [("新片", "2026-12-01")]
    p = api.last("/discover/movie").url.params
    assert (p["primary_release_date.gte"], p["primary_release_date.lte"], p["sort_by"], p["watch_region"]) == (
        "2026-11-11", "2027-02-09", "primary_release_date.asc", "US")


async def test_runtime():
    api = Api({"/movie/7": {"runtime": 155}})
    assert await client(api).runtime(7) == 155
    assert await client(Api({"/movie/7": {"runtime": 0}})).runtime(7) is None


async def test_bad_key_message_names_the_key_and_never_contains_it():
    with pytest.raises(TmdbError) as exc:
        await client(Api(status=401), key="SECRETKEY").available("netflix", "tv")
    assert "金鑰無效" in str(exc.value) and "SECRETKEY" not in str(exc.value)


async def test_other_failures_are_reported_as_tmdb_errors():
    with pytest.raises(TmdbError, match="無法取得片單資料"):
        await client(Api(status=500)).available("netflix", "tv")

    def boom(request):
        raise httpx.ConnectError("down")
    c = TmdbClient("k", transport=httpx.MockTransport(boom), today=lambda: TODAY)
    with pytest.raises(TmdbError, match="無法取得片單資料"):
        await c.available("netflix", "tv")


async def test_identical_call_is_cached():
    api = Api({"/discover/tv": TV_PAGE})
    c = client(api)
    await c.available("netflix", "tv")
    n = len(api.requests)
    await c.available("netflix", "tv")
    assert len(api.requests) == n


async def test_unknown_platform_or_kind_is_a_value_error():
    c = client(Api())
    with pytest.raises(ValueError):
        await c.available("hulu", "tv")
    with pytest.raises(ValueError):
        await c.available("netflix", "anime")


async def test_find_movie_runtime_searches_by_name_then_reads_runtime():
    api = Api({"/search/movie": {"results": [{"id": 438631, "title": "沙丘"}]}, "/movie/438631": {"runtime": 155}})
    assert await client(api).find_movie_runtime(["沙丘", "Dune"]) == 155
    assert api.last("/search/movie").url.params["query"] == "沙丘"


async def test_find_movie_runtime_tries_the_next_name_and_gives_none_when_unknown():
    api = Api({"/search/movie": {"results": []}})
    assert await client(api).find_movie_runtime(["沙丘", "Dune"]) is None
    assert len([r for r in api.requests if r.url.path.endswith("/search/movie")]) == 2


async def test_find_movie_runtime_is_none_when_not_configured_or_failing():
    assert await client(Api(), key=None).find_movie_runtime(["沙丘"]) is None
    assert await client(Api(status=500)).find_movie_runtime(["沙丘"]) is None
