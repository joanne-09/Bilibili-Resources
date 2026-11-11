import httpx

from app.aliases import AliasResolver, has_cjk

BB = [{"title": "绝命毒师 第一季", "sub_title": "Breaking Bad", "type": "movie", "year": "2008", "episode": "7"}]
FRIENDS = [{"title": "老友记 第一季", "sub_title": "Friends ", "type": "movie", "year": "1994", "episode": "24"}]
WIKI_WED = {"search": [
    {"id": "Q1", "label": "Wednesday", "description": "day of the week"},
    {"id": "Q2", "label": "星期三", "description": "American television series"},
    {"id": "Q3", "label": "星期三 第一季", "description": "season of television series"}]}


class Web:
    def __init__(self, douban=None, wikidata=None):
        self.douban, self.wikidata, self.calls = douban, wikidata, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        source = self.douban if "douban" in host else self.wikidata
        if isinstance(source, Exception):
            raise source
        if isinstance(source, int):
            return httpx.Response(source, text="nope")
        return httpx.Response(200, json=source)


def resolver(web):
    return AliasResolver(transport=httpx.MockTransport(web))


def test_has_cjk():
    assert has_cjk("绝命毒师") and not has_cjk("Breaking Bad")


async def test_english_title_resolves_to_chinese_first():
    names = await resolver(Web(douban=BB)).resolve("breaking bad")
    assert names == ["绝命毒师", "Breaking Bad"]


async def test_taiwan_title_resolves_to_mainland_title():
    names = await resolver(Web(douban=FRIENDS)).resolve("六人行")
    assert names[0] == "老友记"
    assert "六人行" in names and "Friends" in names


async def test_chinese_query_gains_english_name():
    names = await resolver(Web(douban=BB)).resolve("絕命毒師")
    assert names == ["绝命毒师", "Breaking Bad"]


async def test_wikidata_fallback_for_english_query():
    web = Web(douban=[], wikidata=WIKI_WED)
    names = await resolver(web).resolve("wednesday")
    assert names == ["星期三", "wednesday"]


async def test_no_wikidata_call_for_chinese_query():
    web = Web(douban=[], wikidata=WIKI_WED)
    assert await resolver(web).resolve("某部剧") == ["某部剧"]
    assert all("wikidata" not in h for h in web.calls)


async def test_all_sources_failing_falls_back_to_query():
    web = Web(douban=500, wikidata=httpx.ReadTimeout("slow"))
    assert await resolver(web).resolve(" wednesday ") == ["wednesday"]


async def test_movie_entry_is_skipped():
    movie = [{"title": "绝命毒师：续命之徒", "sub_title": "El Camino: A Breaking Bad Movie", "episode": ""}]
    assert (await resolver(Web(douban=movie + BB)).resolve("breaking bad"))[0] == "绝命毒师"


async def test_second_lookup_is_cached():
    web = Web(douban=BB)
    r = resolver(web)
    await r.resolve("Breaking Bad")
    n = len(web.calls)
    assert await r.resolve("breaking  bad") == ["绝命毒师", "Breaking Bad"]
    assert len(web.calls) == n


async def test_unrelated_suggestion_for_english_query_is_ignored():
    web = Web(douban=FRIENDS, wikidata={"search": []})
    assert await resolver(web).resolve("tulsa king") == ["tulsa king"]


# ---- kind detection (so the search box needs no 劇/電影 switch) --------------

DUNE_FILM = [{"title": "沙丘", "sub_title": "Dune", "type": "movie", "year": "2021", "episode": ""}]


async def test_series_is_detected_as_tv():
    assert await resolver(Web(douban=BB)).resolve_full("breaking bad") == (["绝命毒师", "Breaking Bad"], "tv")


async def test_film_is_detected_as_movie_with_both_names():
    assert await resolver(Web(douban=DUNE_FILM)).resolve_full("dune") == (["沙丘", "Dune"], "movie")


async def test_exact_title_match_beats_an_earlier_loose_match():
    spin_off = [{"title": "绝命毒师：续命之徒", "sub_title": "El Camino: A Breaking Bad Movie", "episode": ""}]
    names, kind = await resolver(Web(douban=spin_off + BB)).resolve_full("breaking bad")
    assert (names[0], kind) == ("绝命毒师", "tv")


async def test_unknown_title_defaults_to_tv():
    web = Web(douban=[], wikidata={"search": []})
    assert await resolver(web).resolve_full("zzz qqq") == (["zzz qqq"], "tv")


async def test_kind_is_cached_with_the_names():
    web = Web(douban=DUNE_FILM)
    r = resolver(web)
    await r.resolve_full("Dune")
    n = len(web.calls)
    assert await r.resolve_full("dune") == (["沙丘", "Dune"], "movie") and len(web.calls) == n
