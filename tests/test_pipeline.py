import pytest

from app.bilibili import BiliError, BiliThrottled
from app.models import Part, SearchHit, SeasonEp, VideoDetail
from app.pipeline import latest, search_series

DAY = 86400
NOW = 1_000_000_000


async def noop(message):
    pass


def hit(bvid, title, minutes=600, play=10, pubdate=0, tag=""):
    return SearchHit(bvid=bvid, title=title, duration_s=minutes * 60, play=play, pubdate=pubdate, tag=tag)


def detail(bvid, title, part_minutes, play=10, pubdate=0, **kw):
    return VideoDetail(bvid=bvid, title=title, play=play, pubdate=pubdate, width=1920, height=1080,
                       parts=[Part("p", m * 60) for m in part_minutes], **kw)


class FakeClient:
    """search: every call returns `hits` unless `by_keyword` has the keyword."""

    def __init__(self, hits=(), details=None, by_keyword=None, fail_on=()):
        self.hits, self.details = list(hits), details or {}
        self.by_keyword, self.fail_on = by_keyword or {}, set(fail_on)
        self.search_calls, self.view_calls, self.patience = [], [], []

    async def search(self, keyword, order="click", duration=0, page=1, patient=True):
        self.search_calls.append((keyword, order, duration))
        self.patience.append(patient)
        if len(self.search_calls) in self.fail_on:
            raise BiliThrottled("B 站暫時限制查詢，請過幾分鐘再試")
        return list(self.by_keyword.get(keyword, self.hits))

    async def view(self, bvid):
        self.view_calls.append(bvid)
        return self.details.get(bvid)


def modern_family():
    hits = [
        hit("A", "摩登家庭 全11季 未删减", play=10),
        hit("B", "【摩登-家庭】全十一季 全250集 超清中字", play=99),
        hit("C", "【摩登家庭|第一季（1-9）】中英字幕", minutes=172, play=50),
        hit("D", "一口气看完摩登家庭全集解说", play=500),
        hit("E", "以前的化学课都弱爆了", play=900),
    ]
    details = {
        "A": detail("A", "摩登家庭 全11季 未删减", [380] * 20, play=10),
        "B": detail("B", "【摩登-家庭】全十一季 全250集 超清中字", [375] * 22, play=99),
        "C": detail("C", "【摩登家庭|第一季（1-9）】中英字幕", [172], play=50),
    }
    return hits, details


async def test_searches_primary_name_five_ways():
    fake = FakeClient()
    await search_series(fake, ["绝命毒师"], noop)
    assert fake.search_calls == [
        ("绝命毒师 全集", "click", 4), ("绝命毒师", "click", 4), ("绝命毒师 全集", "click", 3),
        ("绝命毒师 全集", "click", 2)]
    assert fake.patience == [True, False, False, False]


async def test_groups_and_sorts_by_play():
    hits, details = modern_family()
    out = await search_series(FakeClient(hits, details), ["摩登家庭"], noop)
    assert [i.bvid for i in out.complete] == ["B", "A"]
    assert [i.bvid for i in out.uncertain] == ["C"]
    assert {i.bvid: i.reason for i in out.excluded} == {"D": "解說／速看", "E": "與搜尋無關"}
    assert out.names == ["摩登家庭"]


async def test_deleted_candidate_is_skipped_not_fatal():
    hits, details = modern_family()
    del details["A"]
    out = await search_series(FakeClient(hits, details), ["摩登家庭"], noop)
    assert [i.bvid for i in out.complete] == ["B"]


async def test_same_season_appears_once():
    eps = [SeasonEp(f"E{i}", f"绝命毒师 S01E0{i}", 47 * 60, 100) for i in range(1, 8)]
    season = dict(season_id=9, season_title="绝命毒师 第一季", season_eps=eps)
    hits = [hit("E1", "绝命毒师 S01E01", minutes=47, play=100), hit("E2", "绝命毒师 S01E02", minutes=47, play=90)]
    details = {b: detail(b, f"绝命毒师 {b}", [47], **season) for b in ("E1", "E2")}
    fake = FakeClient(hits, details)
    out = await search_series(fake, ["绝命毒师"], noop)
    assert [i.key for i in out.complete] == ["season:9"]
    assert fake.view_calls == ["E1"]


async def test_second_name_adds_one_search_and_matches():
    en = hit("EN1", "Breaking Bad 全集 1080P", play=7)
    fake = FakeClient(by_keyword={"Breaking Bad 全集": [en]},
                      details={"EN1": detail("EN1", "Breaking Bad 全集 1080P", [300] * 10, play=7)})
    out = await search_series(fake, ["绝命毒师", "Breaking Bad"], noop)
    assert fake.search_calls[2] == ("Breaking Bad 全集", "click", 4)
    assert len(fake.search_calls) == 5
    assert [i.bvid for i in out.complete] == ["EN1"]


async def test_first_search_failure_propagates():
    with pytest.raises(BiliError):
        await search_series(FakeClient(fail_on={1}), ["x"], noop)


async def test_throttle_on_a_later_search_stops_searching_and_marks_partial():
    hits, details = modern_family()
    fake = FakeClient(hits, details, fail_on={3})
    out = await search_series(fake, ["摩登家庭"], noop)
    assert [i.bvid for i in out.complete] == ["B", "A"]
    assert len(fake.search_calls) == 3      # no further searches once Bilibili pushes back
    assert out.partial is True


async def test_unthrottled_search_is_not_partial():
    hits, details = modern_family()
    assert (await search_series(FakeClient(hits, details), ["摩登家庭"], noop)).partial is False


async def test_only_top_20_candidates_are_viewed():
    hits = [hit(f"V{i}", "某剧 全集", play=i) for i in range(30)]
    fake = FakeClient(hits)
    await search_series(fake, ["某剧"], noop)
    assert len(fake.view_calls) == 20 and fake.view_calls[0] == "V29"


async def test_latest_filters_age_and_sorts_newest_first():
    t = "2026美剧《某剧 第一季》全集 未删减"
    hits = [hit("Y", t, pubdate=NOW - 10 * DAY), hit("X", t, pubdate=NOW - DAY),
            hit("Z", t, pubdate=NOW - 40 * DAY), hit("W", "【无职转生 全三季 全集】", pubdate=NOW - DAY)]
    details = {b: detail(b, t, [400], pubdate=h.pubdate) for b, h in zip("YXZ", hits)}
    fake = FakeClient(hits, details)
    out = await latest(fake, noop, now=NOW)
    assert [i.bvid for i in out.complete] == ["X", "Y"]
    assert [i.bvid for i in out.excluded] == ["W"]
    assert [c[0] for c in fake.search_calls] == ["美剧 全集", "美剧 未删减", "美剧 双字"]
    assert all(c[1] == "pubdate" and c[2] == 4 for c in fake.search_calls)


async def test_progress_is_reported():
    hits, details = modern_family()
    msgs = []

    async def collect(message):
        msgs.append(message)

    await search_series(FakeClient(hits, details), ["摩登家庭"], collect)
    assert msgs[0] == "正在查 B 站（1/4）…"
    assert any(m.startswith("正在確認候選") for m in msgs)


class FlakyViewClient(FakeClient):
    """view() starts failing (network down / blocked) from the Nth call on."""

    def __init__(self, *args, view_fails_from=1, **kw):
        super().__init__(*args, **kw)
        self.view_fails_from = view_fails_from

    async def view(self, bvid):
        self.view_calls.append(bvid)
        if len(self.view_calls) >= self.view_fails_from:
            raise BiliError("無法連線到 B 站")
        return self.details.get(bvid)


async def test_detail_lookups_all_failing_is_an_error_not_an_empty_result():
    hits, details = modern_family()
    with pytest.raises(BiliError):
        await search_series(FlakyViewClient(hits, details, view_fails_from=1), ["摩登家庭"], noop)


async def test_detail_lookup_failing_midway_keeps_results_and_marks_partial():
    hits, details = modern_family()
    fake = FlakyViewClient(hits, details, view_fails_from=2)
    out = await search_series(fake, ["摩登家庭"], noop)
    assert [i.bvid for i in out.complete] == ["B"]
    assert out.partial is True and len(fake.view_calls) == 2
