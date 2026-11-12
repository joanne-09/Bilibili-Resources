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


# ---- Traditional-Chinese display titles, genres ----------------------------

class LangApi(Api):
    """Answers /discover differently per requested language."""

    def __init__(self, by_lang, extra=None):
        super().__init__(extra or {})
        self.by_lang = by_lang

    def __call__(self, request):
        if "/discover/" in request.url.path:
            self.requests.append(request)
            return httpx.Response(200, json={"page": 1, "total_pages": 1,
                                             "results": self.by_lang[request.url.params["language"]]})
        return super().__call__(request)


async def test_display_title_is_taiwan_title_and_search_title_stays_mainland():
    api = LangApi({
        "zh-TW": [{"id": 1668, "name": "六人行", "original_name": "Friends", "first_air_date": "1994-09-22"}],
        "zh-CN": [{"id": 1668, "name": "老友记", "original_name": "Friends", "first_air_date": "1994-09-22"}]})
    items, _ = await client(api).available("hbo", "tv")
    assert (items[0].display_title, items[0].title, items[0].original_title) == ("六人行", "老友记", "Friends")


async def test_display_title_falls_back_to_converted_mainland_title():
    # TMDB has no Taiwan translation: it echoes the original title for zh-TW.
    api = LangApi({
        "zh-TW": [{"id": 5, "name": "The Gilded Age", "original_name": "The Gilded Age", "first_air_date": "2022-01-24"}],
        "zh-CN": [{"id": 5, "name": "镀金时代", "original_name": "The Gilded Age", "first_air_date": "2022-01-24"}]})
    items, _ = await client(api).available("hbo", "tv")
    assert (items[0].display_title, items[0].title) == ("鍍金時代", "镀金时代")


async def test_title_missing_from_the_mainland_page_uses_converted_taiwan_title_for_search():
    api = LangApi({
        "zh-TW": [{"id": 9, "name": "絕命毒師", "original_name": "Breaking Bad", "first_air_date": "2008-01-20"}],
        "zh-CN": []})
    items, _ = await client(api).available("netflix", "tv")
    assert (items[0].display_title, items[0].title) == ("絕命毒師", "绝命毒师")


async def test_untranslated_title_shows_the_original():
    api = LangApi({"zh-TW": [{"id": 3, "name": "Oobah", "original_name": "Oobah", "first_air_date": "2026-11-22"}],
                   "zh-CN": [{"id": 3, "name": "Oobah", "original_name": "Oobah", "first_air_date": "2026-11-22"}]})
    items, _ = await client(api).available("hbo", "tv")
    assert (items[0].display_title, items[0].title) == ("Oobah", "Oobah")


async def test_genres_are_listed_in_traditional_chinese():
    api = Api({"/genre/tv/list": {"genres": [{"id": 18, "name": "劇情"}, {"id": 35, "name": "喜劇"}]}})
    genres = await client(api).genres("tv")
    assert [(g.id, g.name) for g in genres if g.id > 0] == [(18, "劇情"), (35, "喜劇")]
    assert api.last("/genre/tv/list").url.params["language"] == "zh-TW"


async def test_genre_filter_is_sent_for_available_and_upcoming():
    api = Api({"/discover/tv": {"page": 1, "total_pages": 1, "results": []},
               "/discover/movie": {"page": 1, "total_pages": 1, "results": []}})
    c = client(api)
    await c.available("netflix", "tv", genre=18)
    assert api.last("/discover/tv").url.params["with_genres"] == "18"
    await c.upcoming("netflix", "tv", genre=35)
    assert api.last("/discover/tv").url.params["with_genres"] == "35"
    await c.upcoming("netflix", "movie", genre=28)
    assert api.last("/discover/movie").url.params["with_genres"] == "28"
    await c.available("netflix", "movie")
    assert "with_genres" not in api.last("/discover/movie").url.params


async def test_genre_names_are_traditional_even_when_tmdb_sends_simplified_or_english():
    api = Api({"/genre/tv/list": {"genres": [
        {"id": 10765, "name": "Sci-Fi & Fantasy"}, {"id": 35, "name": "喜剧"}, {"id": 99999, "name": "新类型"}]}})
    assert [g.name for g in await client(api).genres("tv") if g.id > 0] == ["科幻與奇幻", "喜劇", "新類型"]


# ---- extra TV categories TMDB has no genre for (Romance, Horror, ...) -------

async def test_tv_genres_include_keyword_based_extras_after_the_official_ones():
    api = Api({"/genre/tv/list": {"genres": [{"id": 18, "name": "剧情"}]}})
    genres = await client(api).genres("tv")
    names = [g.name for g in genres]
    assert names[0] == "劇情" and {"愛情", "恐怖", "驚悚"} <= set(names)
    assert all(g.id < 0 for g in genres if g.name in ("愛情", "恐怖", "驚悚"))   # negative id = keyword filter
    assert next(g.id for g in genres if g.name == "愛情") == -9840


async def test_movie_genres_get_no_extras():
    api = Api({"/genre/movie/list": {"genres": [{"id": 10749, "name": "爱情"}]}})
    assert [(g.id, g.name) for g in await client(api).genres("movie")] == [(10749, "愛情")]


async def test_negative_genre_filters_by_keyword_instead_of_genre():
    api = Api({"/discover/tv": {"page": 1, "total_pages": 1, "results": []}})
    c = client(api)
    await c.available("netflix", "tv", genre=-9840)
    p = api.last("/discover/tv").url.params
    assert p["with_keywords"] == "9840" and "with_genres" not in p
    await c.upcoming("netflix", "tv", genre=-9840)
    assert api.last("/discover/tv").url.params["with_keywords"] == "9840"


# ---- origin filter (美劇 / 韓劇 / 陸劇 ...), independent of genre -------------

from app.tmdb import ORIGINS  # noqa: E402


def test_origins_cover_the_common_drama_sources():
    codes = {o.code: o.name for o in ORIGINS}
    assert {"US": "美國", "KR": "韓國", "CN": "中國大陸", "JP": "日本", "GB": "英國", "TW": "台灣"}.items() <= codes.items()


async def test_origin_is_sent_as_origin_country_and_combines_with_genre():
    api = Api({"/discover/tv": {"page": 1, "total_pages": 1, "results": []},
               "/discover/movie": {"page": 1, "total_pages": 1, "results": []}})
    c = client(api)
    await c.available("netflix", "tv", genre=18, origin="KR")
    p = api.last("/discover/tv").url.params
    assert (p["with_origin_country"], p["with_genres"]) == ("KR", "18")
    await c.upcoming("netflix", "tv", origin="KR")
    assert api.last("/discover/tv").url.params["with_origin_country"] == "KR"
    await c.upcoming("netflix", "movie", origin="CN")
    assert api.last("/discover/movie").url.params["with_origin_country"] == "CN"
    await c.available("netflix", "movie")
    assert "with_origin_country" not in api.last("/discover/movie").url.params


async def test_unknown_origin_is_a_value_error():
    with pytest.raises(ValueError):
        await client(Api()).available("netflix", "tv", origin="XX")


# ---- title search inside the catalog ----------------------------------------

class SearchApi(Api):
    """/search/multi answers per language; other paths come from routes."""

    def __init__(self, by_lang, routes=None, total_pages=1):
        super().__init__(routes or {})
        self.by_lang, self.total_pages = by_lang, total_pages

    def __call__(self, request):
        if request.url.path.endswith("/search/multi"):
            self.requests.append(request)
            return httpx.Response(200, json={"page": 1, "total_pages": self.total_pages,
                                             "results": self.by_lang[request.url.params["language"]]})
        return super().__call__(request)


FRIENDS_TW = {"id": 1668, "media_type": "tv", "name": "六人行", "original_name": "Friends",
              "first_air_date": "1994-09-22", "poster_path": "/f.jpg"}
FRIENDS_CN = {**FRIENDS_TW, "name": "老友记"}
DUNE_TW = {"id": 438631, "media_type": "movie", "title": "沙丘", "original_title": "Dune", "release_date": "2021-09-15"}
PERSON = {"id": 7, "media_type": "person", "name": "Someone"}
US = lambda *names: {"results": {"US": {"flatrate": [{"provider_id": i, "provider_name": n} for i, n in enumerate(names, 1)]}}}  # noqa: E731


async def test_search_returns_series_and_films_with_both_titles_and_skips_people():
    api = SearchApi({"zh-TW": [FRIENDS_TW, PERSON, DUNE_TW], "zh-CN": [FRIENDS_CN, PERSON, DUNE_TW]})
    items, has_more = await client(api).search("friends")
    assert [(t.kind, t.display_title, t.title, t.original_title, t.year) for t in items] == [
        ("tv", "六人行", "老友记", "Friends", "1994"), ("movie", "沙丘", "沙丘", "Dune", "2021")]
    assert has_more is False
    p = api.last("/search/multi").url.params
    assert (p["query"], p["page"]) == ("friends", "1")


async def test_search_marks_which_of_the_three_platforms_stream_each_title():
    api = SearchApi({"zh-TW": [FRIENDS_TW, DUNE_TW], "zh-CN": [FRIENDS_CN, DUNE_TW]}, routes={
        "/tv/1668/watch/providers": US("HBO Max", "Netflix Standard with Ads", "HBO Max Amazon Channel", "Hulu"),
        "/movie/438631/watch/providers": {"results": {"GB": {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]}}}})
    items, _ = await client(api).search("x")
    assert items[0].platforms == ["netflix", "hbo"]        # platform order, ad tier counts, reseller channel does not
    assert items[1].platforms == []                         # only streaming outside the US


async def test_search_survives_missing_provider_data():
    api = SearchApi({"zh-TW": [FRIENDS_TW], "zh-CN": [FRIENDS_CN]})      # provider lookups 404
    items, _ = await client(api).search("friends")
    assert [t.display_title for t in items] == ["六人行"] and items[0].platforms == []


async def test_search_reports_more_pages_and_rejects_blank_query():
    api = SearchApi({"zh-TW": [FRIENDS_TW], "zh-CN": [FRIENDS_CN]}, total_pages=3)
    assert (await client(api).search("friends", page=2))[1] is True
    with pytest.raises(ValueError):
        await client(api).search("   ")
