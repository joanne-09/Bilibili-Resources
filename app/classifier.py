"""Decides whether an upload is a complete series. Pure logic, no I/O.

Principle: judge whether the upload *as a whole* is a full season/series, never
whether a single part is long enough. When unsure, mark "uncertain" rather than
excluding.
"""
import re

from app.models import ResultItem, SearchHit, VideoDetail
from app.textnorm import cn_to_int, normalize, strip_tags, to_simplified

_NUM = r"[0-9一二两三四五六七八九十]+"
_DASH = r"[-~–—]"

_KEYWORD_GROUPS: list[tuple[str, re.Pattern]] = [
    ("解說／速看", re.compile("解说|解读|一口气|速看|分钟看完|详解|解析|回顾|盘点|影评|讲解")),
    ("學英語筆記", re.compile("学英语|单词|笔记|跟读|精讲|口语|听力")),
    ("剪輯／片段", re.compile(r"混剪|片段|名场面|花絮|预告|笑场|(?<![a-z])cut(?![a-z])")),
    ("網盤引流", re.compile("网盘|自取|免费分享|私信|加群")),
]

_EP_RANGE = re.compile(rf"(?<!\d)(\d{{1,3}})\s*{_DASH}\s*(\d{{1,3}})(?!\d)(?!\s*季)")
_WHOLE_CLAIM = re.compile(rf"全集|全\s*{_NUM}\s*集|完结")
_ANY_CLAIM = re.compile(rf"全集|全\s*{_NUM}\s*[集季]|完结|未删减|无删")
_CLAIMED_EPS = re.compile(rf"全\s*({_NUM})\s*集")
_ALL_SEASONS = re.compile(
    rf"全\s*({_NUM})\s*季|(?<![第0-9一二两三四五六七八九十])({_NUM})\s*季全(?![集0-9一二两三四五六七八九十])")
_SEASON_RANGE = re.compile(
    rf"(?<![a-z])s(\d{{1,2}})\s*{_DASH}\s*s?(\d{{1,2}})(?!\d)|(?<!\d)(\d{{1,2}})\s*{_DASH}\s*(\d{{1,2}})\s*季")
_ONE_SEASON = re.compile(rf"第\s*({_NUM})\s*季|(?<![a-z])s(\d{{1,2}})(?!\d)")

MIN_UNIT_S = 15 * 60       # a part shorter than this is not an episode (outros, notes)
MIN_TOTAL_S = 60 * 60
MERGED_AVG_S = 90 * 60     # average part this long => several episodes merged per part
MIN_EP_S = 18 * 60         # shortest plausible episode, for "全N集" sanity checks


def _clean(title: str) -> str:
    return to_simplified(strip_tags(title)).lower()


def _keyword_reason(title: str) -> str | None:
    text = _clean(title)
    for reason, pattern in _KEYWORD_GROUPS:
        if pattern.search(text):
            return reason
    return None


def _mentions(names: list[str], text: str) -> bool:
    haystack = normalize(text)
    return any(n and n in haystack for n in map(normalize, names))


def prefilter(hit: SearchHit, names: list[str] | None) -> str | None:
    """Cheap check on a search hit. Returns an exclusion reason, or None to keep."""
    reason = _keyword_reason(hit.title)
    if reason:
        return reason
    if "国产" in hit.typename:
        return "國產劇分區"  # a same-named Chinese drama, not the US series
    if names:
        if not _mentions(names, hit.title):
            return "與搜尋無關"
    elif "美剧" not in _clean(hit.title + " " + hit.tag):
        return "非美劇"
    if hit.duration_s < 600:
        return "時長過短"
    return None


def excluded_from_hit(hit: SearchHit, reason: str) -> ResultItem:
    return ResultItem(
        key=hit.bvid, bvid=hit.bvid, url=_url(hit.bvid), title=strip_tags(hit.title),
        cover=hit.cover, play=hit.play, pubdate=hit.pubdate,
        total_minutes=hit.duration_s // 60, form="", form_label="", coverage="",
        quality="", subtitle="", uploader=hit.author, status="excluded", reason=reason)


def _url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def _claimed_seasons(text: str) -> int | None:
    if m := _ALL_SEASONS.search(text):
        return cn_to_int(m.group(1) or m.group(2))
    return None


def _coverage(text: str) -> str:
    n = _claimed_seasons(text)
    if n:
        return f"全 {n} 季"
    if m := _SEASON_RANGE.search(text):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if int(a) < int(b):
            return f"第 {int(a)}–{int(b)} 季"
    if m := _ONE_SEASON.search(text):
        n = cn_to_int(m.group(1)) if m.group(1) else int(m.group(2))
        if n:
            return f"第 {n} 季"
    return ""


def _subtitle(text: str) -> str:
    if re.search("纯英|英文字幕|无中文", text):
        return "純英文"
    if re.search("双语|双字|中英", text):
        return "雙語"
    if re.search("中字|中文字幕|熟肉", text):
        return "中字"
    return "未知"


def _quality(width: int, height: int) -> str:
    short = min(width, height)
    if short <= 0:
        return "未知"
    if short >= 2160:
        return "4K"
    if short >= 1080:
        return "1080P"
    if short >= 720:
        return "720P"
    return f"{short}P"


def _partial_range(text: str) -> tuple[int, int] | None:
    for m in _EP_RANGE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        prefix = text[max(0, m.start() - 1):m.start()]
        if a < b <= 300 and prefix != "s":
            return a, b
    return None


def classify(detail: VideoDetail, names: list[str] | None) -> ResultItem:
    """Full verdict from a video's detail. Always returns an item."""
    use_season = bool(
        names and len(detail.season_eps) >= 2 and detail.season_title
        and _mentions(names, detail.season_title))

    if use_season:
        durations = [ep.duration_s for ep in detail.season_eps]
        key, bvid = f"season:{detail.season_id}", detail.season_eps[0].bvid
        play, title = sum(ep.play for ep in detail.season_eps), detail.season_title
    else:
        durations = [p.duration_s for p in detail.parts]
        key, bvid, play, title = detail.bvid, detail.bvid, detail.play, strip_tags(detail.title)

    text = _clean(title)
    label_text = _clean(f"{title} {detail.title}") if use_season else text
    content = [d for d in durations if d >= MIN_UNIT_S]
    total, n = sum(content), len(content)

    def item(status: str, reason: str, form: str = "", form_label: str = "") -> ResultItem:
        return ResultItem(
            key=key, bvid=bvid, url=_url(bvid), title=title, cover=detail.cover, play=play,
            pubdate=detail.pubdate, total_minutes=total // 60, form=form, form_label=form_label,
            coverage=_coverage(label_text), quality=_quality(detail.width, detail.height),
            subtitle=_subtitle(label_text), uploader=detail.author, status=status, reason=reason)

    reason = _keyword_reason(detail.title) or (_keyword_reason(title) if use_season else None)
    if reason:
        return item("excluded", reason)
    if n == 0 or total < MIN_TOTAL_S:
        return item("excluded", "時長過短")

    if use_season:
        form, form_label = "collection", f"合集 · {n} 支影片"
    elif total / n >= MERGED_AVG_S:
        form, form_label = "merged", f"合併長片 · {n} 段"
    else:
        form, form_label = "episodic", f"一集一段 · {n} 集"

    partial = _partial_range(text)
    if partial and not _WHOLE_CLAIM.search(text):
        return item("uncertain", f"只有 {partial[0]}–{partial[1]} 集", form, form_label)

    if m := _CLAIMED_EPS.search(text):
        claimed = cn_to_int(m.group(1))
        if claimed and total < claimed * MIN_EP_S:
            return item("uncertain",
                        f"時長不足：聲稱 {claimed} 集，實際僅 {total / 3600:.1f} 小時", form, form_label)

    if use_season and total / n >= MERGED_AVG_S:
        # Each video holds a season or more; fewer videos than seasons claimed
        # usually means the uploader has not finished the collection.
        seasons = _claimed_seasons(text)
        if seasons and n < seasons:
            return item("uncertain", f"合集只有 {n} 支影片，聲稱全 {seasons} 季", form, form_label)

    if not _ANY_CLAIM.search(text):
        if (form == "episodic" and n < 6) or (form == "merged" and total < 180 * 60):
            return item("uncertain", "內容偏少，無法確認完整", form, form_label)

    return item("complete", "", form, form_label)
