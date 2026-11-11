import hashlib

import httpx
import pytest

from app.bilibili import BiliClient, BiliError, mixin_key, sign

NAV = {"code": -101, "data": {"wbi_img": {
    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eea6f01bf08b70ac45.png"}}}
SPI = {"code": 0, "data": {"b_3": "B3", "b_4": "B4"}}
ONE_RESULT = {"code": 0, "data": {"numResults": 1, "result": [{
    "bvid": "BV1", "title": '<em class="keyword">摩登家庭</em> 全集', "duration": "8135:14",
    "play": 5, "pubdate": 1700000000, "typename": "日常", "author": "up",
    "pic": "//i0.hdslb.com/a.jpg", "description": "d", "tag": "美剧"}]}}
VIEW = {"code": 0, "data": {
    "bvid": "BV1", "title": "T", "desc": "D", "pubdate": 1700000000, "pic": "http://i0.hdslb.com/c.jpg",
    "owner": {"name": "up"}, "stat": {"view": 42}, "dimension": {"width": 1920, "height": 1080},
    "pages": [{"part": "a", "duration": 60}, {"part": "b", "duration": 120}],
    "ugc_season": {"id": 9, "title": "S", "sections": [
        {"episodes": [{"bvid": "BVa", "title": "e1", "arc": {"duration": 100, "stat": {"view": 7}}}]},
        {"episodes": [{"bvid": "BVb", "title": "e2", "arc": {"duration": 200, "stat": {"view": 8}}}]}]}}}


class Site:
    """Fake bilibili: scripted search/view responses, records what it saw."""

    def __init__(self, search=None, view=None):
        self.search = list(search or [ONE_RESULT])
        self.view = view or VIEW
        self.search_params = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/finger/spi"):
            return httpx.Response(200, json=SPI)
        if path.endswith("/nav"):
            return httpx.Response(200, json=NAV)
        if path.endswith("/search/type"):
            self.search_params.append(dict(request.url.params))
            body = self.search.pop(0) if len(self.search) > 1 else self.search[0]
            return httpx.Response(200, json=body)
        if path.endswith("/web-interface/view"):
            return httpx.Response(200, json=self.view)
        return httpx.Response(200, text="<html></html>")


def make(site):
    return BiliClient(transport=httpx.MockTransport(site), search_interval=0, view_interval=0,
                      retry_waits=(0, 0, 0))


THROTTLED = {"code": 0, "data": {"seid": "1"}}


def test_mixin_key_known_vector():
    assert mixin_key("7cd084941338484aae1ad9425b84077c",
                     "4932caff0ff746eea6f01bf08b70ac45") == "ee1db124af3c7062474693fa704f4ff8"


def test_sign_is_md5_of_sorted_query_plus_key():
    out = sign({"b": "x!'()*y", "a": 1}, "KEY", wts=100)
    assert out["wts"] == 100
    assert out["w_rid"] == hashlib.md5(b"a=1&b=xy&wts=100KEY").hexdigest()


async def test_search_maps_fields():
    hits = await make(Site()).search("摩登家庭", duration=4)
    h = hits[0]
    assert (h.bvid, h.title, h.duration_s, h.cover) == ("BV1", "摩登家庭 全集", 488114, "https://i0.hdslb.com/a.jpg")
    assert (h.play, h.author, h.tag, h.desc) == (5, "up", "美剧", "d")


async def test_search_request_is_signed():
    site = Site()
    await make(site).search("摩登家庭", order="pubdate", duration=4)
    p = site.search_params[0]
    assert {"w_rid", "wts", "keyword", "order", "duration", "search_type"} <= set(p)
    assert (p["keyword"], p["order"], p["duration"], p["search_type"]) == ("摩登家庭", "pubdate", "4", "video")


async def test_throttled_then_ok_retries():
    site = Site(search=[THROTTLED, ONE_RESULT])
    assert len(await make(site).search("x")) == 1
    assert len(site.search_params) == 2


async def test_throttled_forever_raises_not_empty():
    with pytest.raises(BiliError, match="暫時限制"):
        await make(Site(search=[THROTTLED])).search("x")


async def test_true_zero_results_returns_empty():
    assert await make(Site(search=[{"code": 0, "data": {"numResults": 0}}])).search("x") == []


async def test_error_code_raises():
    with pytest.raises(BiliError, match="-412"):
        await make(Site(search=[{"code": -412}])).search("x")


async def test_network_failure_raises_bili_error():
    def boom(request):
        raise httpx.ConnectError("down")
    client = BiliClient(transport=httpx.MockTransport(boom), search_interval=0, view_interval=0,
                        retry_waits=(0,))
    with pytest.raises(BiliError, match="無法連線"):
        await client.search("x")


async def test_view_maps_parts_and_season():
    d = await make(Site()).view("BV1")
    assert [p.duration_s for p in d.parts] == [60, 120]
    assert (d.season_id, d.season_title, len(d.season_eps), d.season_eps[0].play) == (9, "S", 2, 7)
    assert (d.width, d.height, d.play, d.author) == (1920, 1080, 42, "up")
    assert d.cover == "https://i0.hdslb.com/c.jpg"


async def test_view_of_deleted_video_returns_none():
    assert await make(Site(view={"code": -404})).view("BVgone") is None
