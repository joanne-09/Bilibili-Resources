"""Decides whether an upload is a whole film (not a recap or a clip). Pure logic.

A film has no episode count to cross-check, so duration is the main evidence:
compared against the known runtime when we have it, otherwise against what a
feature film plausibly runs. Recap wording in the title always excludes.
"""
from app.classifier import _clean, _keyword_reason, _mentions, _quality, _subtitle, _url
from app.models import ResultItem, SearchHit, VideoDetail
from app.textnorm import strip_tags

MIN_FILM_UNIT_S = 40 * 60      # a part shorter than this is not a film
MIN_SPLIT_PART_S = 10 * 60     # parts counted when a film is uploaded in pieces
SERIES_UNIT_S = 70 * 60        # each part this long => a collection of films
LOW, HIGH = 0.85, 1.15         # acceptable share of the known runtime
RECAP_SHARE = 0.60             # below this share of the runtime it is a recap/cut
PLAUSIBLE_MIN_S, PLAUSIBLE_MAX_S = 70 * 60, 240 * 60


def prefilter_movie(hit: SearchHit, names: list[str]) -> str | None:
    """Cheap check on a search hit. Returns an exclusion reason, or None to keep."""
    reason = _keyword_reason(hit.title)
    if reason:
        return reason
    if not _mentions(names, hit.title):
        return "與搜尋無關"
    if hit.duration_s < MIN_FILM_UNIT_S:
        return "時長過短"
    return None


def classify_movie(detail: VideoDetail, names: list[str], runtime_min: int | None) -> ResultItem:
    title = strip_tags(detail.title)
    text = _clean(title)
    durations = [p.duration_s for p in detail.parts]
    units = [d for d in durations if d >= MIN_FILM_UNIT_S]
    pieces = [d for d in durations if d >= MIN_SPLIT_PART_S]
    longest = max(units, default=0)

    def item(status: str, reason: str, form: str = "", form_label: str = "") -> ResultItem:
        return ResultItem(
            key=detail.bvid, bvid=detail.bvid, url=_url(detail.bvid), title=title,
            cover=detail.cover, play=detail.play, pubdate=detail.pubdate,
            total_minutes=sum(pieces) // 60, form=form, form_label=form_label, coverage="",
            quality=_quality(detail.width, detail.height), subtitle=_subtitle(text),
            uploader=detail.author, status=status, reason=reason)

    reason = _keyword_reason(detail.title)
    if reason:
        return item("excluded", reason)
    if not _mentions(names, detail.title):
        return item("excluded", "與搜尋無關")
    if not units:
        return item("excluded", "時長過短")

    if len(units) >= 2 and all(d >= SERIES_UNIT_S for d in units):
        whole = ("series", f"系列合集 · {len(units)} 部")
    else:
        whole = ("movie", f"正片 · {longest // 60} 分")

    if runtime_min:
        runtime_s = runtime_min * 60
        if any(LOW * runtime_s <= d <= HIGH * runtime_s for d in units):
            return item("complete", "", *whole)
        if len(pieces) >= 2 and LOW * runtime_s <= sum(pieces) <= HIGH * runtime_s:
            return item("complete", "", "movie", f"分段 · {len(pieces)} 段")
        if longest < RECAP_SHARE * runtime_s:
            return item("excluded", "時長遠短於片長（可能是解說）")
        return item("uncertain", f"時長 {longest // 60} 分，片長應為 {runtime_min} 分", *whole)

    if longest < PLAUSIBLE_MIN_S:
        return item("uncertain", "時長偏短，無法確認是正片", *whole)
    if longest > PLAUSIBLE_MAX_S:
        return item("uncertain", "時長異常，無法確認是正片", *whole)
    return item("complete", "", *whole)
