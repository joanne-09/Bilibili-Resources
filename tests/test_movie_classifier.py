from app.movie_classifier import classify_movie, prefilter_movie
from tests.test_classifier import detail, hit

DUNE = ["沙丘", "Dune"]


def test_single_film_matching_runtime_is_complete():
    r = classify_movie(detail("沙丘 2021 4K 中英双字", [155], w=3840, h=2160), DUNE, 155)
    assert (r.status, r.form, r.form_label, r.quality, r.subtitle) == ("complete", "movie", "正片 · 155 分", "4K", "雙語")
    assert (r.coverage, r.key, r.url) == ("", "BVd", "https://www.bilibili.com/video/BVd")


def test_runtime_tolerance_is_15_percent():
    assert classify_movie(detail("沙丘", [133]), DUNE, 155).status == "complete"     # 85.8%
    assert classify_movie(detail("沙丘", [176]), DUNE, 155).status == "complete"     # 113.5%


def test_two_part_split_is_complete():
    r = classify_movie(detail("沙丘 上下两部分", [80, 76]), DUNE, 155)
    assert (r.status, r.form_label) == ("complete", "分段 · 2 段")


def test_far_shorter_than_runtime_is_excluded_as_probable_recap():
    r = classify_movie(detail("沙丘 完整版", [62]), DUNE, 155)
    assert (r.status, r.reason) == ("excluded", "時長遠短於片長（可能是解說）")


def test_recap_keyword_excludes_regardless_of_duration():
    r = classify_movie(detail("《沙丘》电影解说 完整版", [150]), DUNE, 155)
    assert (r.status, r.reason) == ("excluded", "解說／速看")


def test_in_between_duration_is_uncertain_with_both_numbers():
    r = classify_movie(detail("沙丘 国语版", [110]), DUNE, 155)
    assert (r.status, r.reason) == ("uncertain", "時長 110 分，片長應為 155 分")


def test_without_runtime_uses_plausible_film_length():
    assert classify_movie(detail("沙丘 2021 完整版 中字", [110]), DUNE, None).status == "complete"
    bare = classify_movie(detail("沙丘", [110]), DUNE, None)
    assert (bare.status, bare.reason) == ("uncertain", "不知道片長，無法確認是正片")
    short = classify_movie(detail("沙丘", [55]), DUNE, None)
    assert (short.status, short.reason) == ("uncertain", "時長偏短，無法確認是正片")
    long = classify_movie(detail("沙丘", [300]), DUNE, None)
    assert (long.status, long.reason) == ("uncertain", "時長異常，無法確認是正片")


def test_too_short_to_be_a_film_is_excluded():
    r = classify_movie(detail("沙丘 名场面合集", [12, 9, 14]), DUNE, None)
    assert r.status == "excluded"
    assert classify_movie(detail("沙丘", [25]), DUNE, 155).reason == "時長過短"


def test_series_collection_is_kept_when_one_part_matches():
    r = classify_movie(detail("沙丘 1-2 合集 4K", [155, 166]), DUNE, 155)
    assert (r.status, r.form, r.form_label) == ("complete", "series", "系列合集 · 2 部")


def test_reaction_wording_is_not_an_exclusion():
    assert classify_movie(detail("沙丘 完整版 reaction", [155]), DUNE, 155).status == "complete"


def test_english_name_counts_as_relevant():
    assert classify_movie(detail("Dune.2021.2160p", [155]), DUNE, 155).status == "complete"


def test_prefilter_movie():
    assert prefilter_movie(hit("几分钟看完《沙丘》", 90), DUNE) == "解說／速看"
    assert prefilter_movie(hit("星球大战 全集", 120), DUNE) == "與搜尋無關"
    assert prefilter_movie(hit("沙丘 预告", 3), DUNE) == "剪輯／片段"
    assert prefilter_movie(hit("沙丘 配乐欣赏", 20), DUNE) == "原聲／音樂"
    assert prefilter_movie(hit("沙丘 删减镜头", 20), DUNE) == "時長過短"
    assert prefilter_movie(hit("Dune 2021 中字", 155), DUNE) is None


BATMAN = ["蝙蝠侠：黑暗骑士", "The Dark Knight"]


def test_game_playthroughs_are_not_films():      # seen live: LEGO Batman videos of film-like length
    for title in ["儿子本地双人合作《乐高蝙蝠侠：黑暗骑士之遗》全流程", "《蝙蝠侠：黑暗骑士》游戏实况 最高难度",
                  "【4K60帧】蝙蝠侠：黑暗骑士 全Boss战+结局", "蝙蝠侠：黑暗骑士 一命通关攻略"]:
        r = classify_movie(detail(title, [150]), BATMAN, 152)
        assert (r.status, r.reason) == ("excluded", "遊戲影片"), title


def test_close_reading_videos_are_recaps():      # seen live: 221-minute "逐帧细读"
    r = classify_movie(detail("超八万字！三个半小时逐帧细读《蝙蝠侠：黑暗骑士》", [221]), BATMAN, 152)
    assert (r.status, r.reason) == ("excluded", "解說／速看")


def test_game_zone_hits_are_prefiltered():
    h = hit("《乐高蝙蝠侠：黑暗骑士之遗》豪华版", 824, typename="单机游戏")
    assert prefilter_movie(h, BATMAN) == "遊戲影片"


def test_essays_soundtracks_and_board_games_are_not_films():     # seen live for "dune"
    cases = {"万字拆解《沙丘》：结合原著，看懂故事": "解說／速看",
             "科幻史诗《沙丘》六部曲｜讲了什么故事": "解說／速看",
             "【电影原声】《沙丘》电影原声 DUNE O.S.T.": "原聲／音樂",
             "《沙丘:帝国》桌游实战": "遊戲影片"}
    for title, reason in cases.items():
        r = classify_movie(detail(title, [84]), DUNE, None)
        assert (r.status, r.reason) == ("excluded", reason), title
