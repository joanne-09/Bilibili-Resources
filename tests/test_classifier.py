from app.classifier import classify, prefilter
from app.models import Part, SearchHit, SeasonEp, VideoDetail


def hit(title, dur_min, **kw):
    return SearchHit(bvid=kw.pop("bvid", "BVh"), title=title, duration_s=int(dur_min * 60),
                     play=kw.pop("play", 100), pubdate=0, **kw)


def detail(title, part_minutes, w=1920, h=1080, **kw):
    return VideoDetail(bvid=kw.pop("bvid", "BVd"), title=title, play=kw.pop("play", 100),
                       width=w, height=h,
                       parts=[Part(f"P{i}", m * 60) for i, m in enumerate(part_minutes, 1)], **kw)


def test_merged_full_series_is_complete():  # BV1G1s2zvEbF
    r = classify(detail("【摩登家庭 全十一季】全250集 超清中字（未删减版）reaction",
                        [379, 391, 375, 374] * 5 + [323, 325]), ["摩登家庭"])
    assert (r.status, r.form, r.coverage, r.quality, r.subtitle) == (
        "complete", "merged", "全 11 季", "1080P", "中字")
    assert r.form_label == "合併長片 · 22 段"
    assert r.url == "https://www.bilibili.com/video/BVd"


def test_one_episode_per_part_is_complete():  # user requirement
    r = classify(detail("摩登家庭 第一季 中英双字", [22] * 24), ["摩登家庭"])
    assert (r.status, r.form, r.form_label, r.coverage, r.subtitle) == (
        "complete", "episodic", "一集一段 · 24 集", "第 1 季", "雙語")
    assert r.total_minutes == 528


def test_evasion_title_and_short_outro_part():  # BV1Ny6xBSEp4
    r = classify(detail("《摩登-家庭 第五季》全24集 超清双字", [286, 285, 286, 285, 11]), ["摩登家庭"])
    assert (r.status, r.form_label, r.coverage) == ("complete", "合併長片 · 4 段", "第 5 季")


def test_collection_merges_and_sums_play():
    eps = [SeasonEp(f"BV{i}", f"绝命毒师 S01E0{i}", 47 * 60, 1000) for i in range(1, 8)]
    r = classify(detail("绝命毒师 S01E03", [47], season_id=9, season_title="绝命毒师 第一季",
                        season_eps=eps), ["绝命毒师"])
    assert (r.status, r.form, r.key, r.play, r.bvid) == ("complete", "collection", "season:9", 7000, "BV1")
    assert r.form_label == "合集 · 7 支影片"
    assert r.title == "绝命毒师 第一季"


def test_unrelated_season_is_ignored():
    eps = [SeasonEp("BVx", "别的剧", 3000, 5)] * 3
    r = classify(detail("绝命毒师 全集 未删减", [280, 290, 300], season_id=1, season_title="美剧合集",
                        season_eps=eps), ["绝命毒师"])
    assert (r.key, r.form) == (r.bvid, "merged")


def test_partial_range_is_uncertain():
    r = classify(detail("【摩登家庭|第一季（1-9）】中英字幕", [172]), ["摩登家庭"])
    assert (r.status, r.reason) == ("uncertain", "只有 1–9 集")


def test_season_range_is_not_partial():
    for t in ["《怪奇物语》未删减版S1~S5季全集 4K", "老友记 1-10季全集"]:
        assert classify(detail(t, [400] * 6), None).status == "complete"
    assert classify(detail("《怪奇物语》未删减版S1~S5季全集 4K", [400] * 6), None).coverage == "第 1–5 季"


def test_claimed_episodes_but_too_short_is_uncertain():
    r = classify(detail("某剧 全24集", [100]), ["某剧"])
    assert r.status == "uncertain" and r.reason.startswith("時長不足")


def test_small_unclaimed_upload_is_uncertain():
    r = classify(detail("某剧 第一季", [45, 45, 45]), ["某剧"])
    assert (r.status, r.reason) == ("uncertain", "內容偏少，無法確認完整")


def test_recap_and_study_and_tiny_are_excluded():
    r = classify(detail("一口气看完经典高分美剧《风骚律师》1-5季全集", [80, 76, 97, 78, 76]), ["风骚律师"])
    assert (r.status, r.reason) == ("excluded", "解說／速看")
    assert classify(detail("《摩登家庭》第一季|单词笔记|英语学习", [7] * 84), ["摩登家庭"]).status == "excluded"
    assert classify(detail("摩登家庭 全集", [7] * 84), ["摩登家庭"]).reason == "時長過短"


def test_reaction_is_not_an_exclusion():
    r = classify(detail("2026美剧《塔尔萨之王 第四季》全集 蓝光超清未删减reaction", [391]), None)
    assert r.status == "complete"


def test_prefilter():
    assert prefilter(hit("美剧 老友记 1-10季超清中英字幕1080p全集 网盘分享 自取", 1), ["老友记"]) == "網盤引流"
    assert prefilter(hit("安徽蒙城惊现“绝命毒师”", 4), ["绝命毒师"]) == "時長過短"
    assert prefilter(hit("以前的化学课都弱爆了", 90), ["绝命毒师"]) == "與搜尋無關"
    assert prefilter(hit("【摩登-家庭】全11季 超清中字", 7483), ["摩登家庭"]) is None
    assert prefilter(hit("【无职转生 全三季 全集】", 2234), None) == "非美劇"
    assert prefilter(hit("星期三第一季 全集中英双字无删减", 691, tag="美剧,悬疑"), None) is None


def test_either_name_makes_a_title_relevant():
    names = ["绝命毒师", "Breaking Bad"]
    assert prefilter(hit("Breaking.Bad S01-S05 1080P 全集", 2900), names) is None
    assert prefilter(hit("绝命毒师 全五季 未删减", 2900), names) is None
    assert prefilter(hit("以前的化学课都弱爆了", 90), names) == "與搜尋無關"


def test_quality_labels():
    assert classify(detail("某剧 全集", [300, 300], w=3840, h=2160), None).quality == "4K"
    assert classify(detail("某剧 全集", [300, 300], w=1280, h=720), None).quality == "720P"
    assert classify(detail("某剧 全集", [300, 300], w=0, h=0), None).quality == "未知"


def test_subtitle_labels():
    assert classify(detail("某剧 全集 纯英文字幕", [300, 300]), None).subtitle == "純英文"
    assert classify(detail("某剧 全集", [300, 300]), None).subtitle == "未知"


def test_domestic_drama_zone_is_excluded():
    h = hit("【爱情/家庭】摩登家庭 全33集【2002】", 1531, typename="国产剧")
    assert prefilter(h, ["摩登家庭"]) == "國產劇分區"


def test_collection_with_fewer_videos_than_claimed_seasons_is_uncertain():  # BV1NnBYBaEGg
    eps = [SeasonEp(f"BV{i}", f"第{i}季", m * 60, 1000) for i, m in ((3, 1027), (4, 1226), (5, 1154))]
    r = classify(detail("《摩登-家庭 第五季》全24集 超清双字", [286] * 4, season_id=7,
                        season_title="摩登家庭 全11季合辑", season_eps=eps), ["摩登家庭"])
    assert (r.status, r.reason) == ("uncertain", "合集只有 3 支影片，聲稱全 11 季")


def test_collection_of_single_episodes_claiming_all_seasons_stays_complete():
    eps = [SeasonEp(f"BV{i}", f"E{i}", 22 * 60, 10) for i in range(48)]
    r = classify(detail("某剧 E1", [22], season_id=8, season_title="某剧 全2季", season_eps=eps), ["某剧"])
    assert r.status == "complete"


def test_coverage_with_trailing_quan():
    r = classify(detail("【摩登家庭 十一季全】超清 完整版 未删减 reaction", [360] * 17), ["摩登家庭"])
    assert r.coverage == "全 11 季"


def test_single_unclaimed_video_is_not_a_series():
    r = classify(detail("XDD阿P在家直播做饭 绝命毒师都不敢这么加料！", [65]), ["绝命毒师"])
    assert (r.status, r.reason) == ("excluded", "單支影片，非整季")
    r = classify(detail("4K超清《续命之徒：绝命毒师电影》", [174]), ["绝命毒师"])
    assert (r.status, r.reason) == ("excluded", "單支影片，非整季")


def test_single_video_claiming_a_full_season_is_kept():
    assert classify(detail("2026美剧《某剧 第四季》全集 未删减", [391]), None).status == "complete"
