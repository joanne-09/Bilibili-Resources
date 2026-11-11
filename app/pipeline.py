"""Search orchestration: gather candidates from Bilibili, classify, merge, sort."""
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.bilibili import BiliError
from app.classifier import classify, excluded_from_hit, prefilter
from app.models import ResultItem, SearchHit
from app.movie_classifier import classify_movie, prefilter_movie

Progress = Callable[[str], Awaitable[None]]
Query = tuple[str, int]  # (keyword, Bilibili duration bucket)

MAX_SEARCH_CANDIDATES = 20
MAX_LATEST_CANDIDATES = 30
LATEST_WINDOW_S = 30 * 86400
LATEST_KEYWORDS = ["美剧 全集", "美剧 未删减", "美剧 双字"]


@dataclass
class Outcome:
    complete: list[ResultItem] = field(default_factory=list)
    uncertain: list[ResultItem] = field(default_factory=list)
    excluded: list[ResultItem] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    fetched_at: float = 0.0
    partial: bool = False  # Bilibili pushed back mid-way; some searches were skipped
    kind: str = "tv"       # "tv" | "movie": which classifier judged the results
    runtime: int | None = None        # known film length in minutes (movie mode)
    skipped: list[Query] = field(default_factory=list)  # searches Bilibili refused / we did not run


def bilibili_search_url(keyword: str, duration: int) -> str:
    """Bilibili's own search page for a query, for the user to open themselves."""
    return (f"https://search.bilibili.com/all?keyword={urllib.parse.quote(keyword, safe='')}"
            f"&order=click&duration={duration}")


async def _gather(client, plan: list[Query], order: str,
                  progress: Progress) -> tuple[list[SearchHit], list[Query]]:
    """Run each (keyword, duration) search, most valuable first; dedupe by bvid.

    Returns (hits, skipped). The first search waits out a throttle and its
    failure propagates. After that, the moment Bilibili pushes back we stop
    asking and go with what we have: more requests would only prolong the
    block. `skipped` is the refused search plus every one after it.
    """
    hits: dict[str, SearchHit] = {}
    for i, (keyword, duration) in enumerate(plan, 1):
        await progress(f"正在查 B 站（{i}/{len(plan)}）…")
        try:
            found = await client.search(keyword, order=order, duration=duration, patient=(i == 1))
        except BiliError:
            if i == 1:
                raise
            return list(hits.values()), list(plan[i - 1:])
        for h in found:
            hits.setdefault(h.bvid, h)
    return list(hits.values()), []


def _items(out: Outcome) -> list[ResultItem]:
    return [*out.complete, *out.uncertain, *out.excluded]


async def _judge(client, candidates: list[SearchHit], names: list[str] | None,
                 out: Outcome, progress: Progress) -> None:
    seen_keys: set[str] = {i.key for i in _items(out)}
    judged = 0
    covered: set[str] = set()  # bvids already represented by a collection result
    for i, h in enumerate(candidates, 1):
        if i == 1 or i % 5 == 0:
            await progress(f"正在確認候選（{i}/{len(candidates)}）· 已找到 {len(out.complete)} 個完整版")
        if h.bvid in covered:
            continue
        try:
            detail = await client.view(h.bvid)
        except BiliError:
            if not judged:
                raise  # nothing confirmed at all: report the failure, not "no results"
            out.partial = True
            return
        judged += 1
        if detail is None:
            continue  # taken down since it was indexed
        if out.kind == "movie":
            item = classify_movie(detail, names or [], out.runtime)
        else:
            item = classify(detail, names)
        if item.key in seen_keys:
            continue
        seen_keys.add(item.key)
        if item.form == "collection":
            covered.update(ep.bvid for ep in detail.season_eps)
        if not item.cover:
            item.cover = h.cover
        getattr(out, item.status).append(item)


async def _absorb(client, out: Outcome, hits: list[SearchHit], progress: Progress,
                  limit: int, order_key: str) -> None:
    """Prefilter and judge the hits not already in `out`, then re-sort its groups."""
    names = out.names or None
    known = {i.bvid for i in _items(out)}
    candidates = []
    for h in hits:
        if h.bvid in known:
            continue
        known.add(h.bvid)
        reason = prefilter_movie(h, out.names) if out.kind == "movie" else prefilter(h, names)
        if reason:
            out.excluded.append(excluded_from_hit(h, reason))
        else:
            candidates.append(h)
    candidates.sort(key=lambda h: getattr(h, order_key), reverse=True)
    try:
        await _judge(client, candidates[:limit], names, out, progress)
    finally:
        for group in (out.complete, out.uncertain, out.excluded):
            group.sort(key=lambda i: getattr(i, order_key), reverse=True)


async def extend_outcome(client, out: Outcome, hits: list[SearchHit], progress: Progress,
                         limit: int) -> None:
    """Merge the results of one more search into an existing outcome (backfill)."""
    await _absorb(client, out, hits, progress, limit, "play")


def _plan(names: list[str], kind: str) -> list[Query]:
    """Searches for a title, ordered by how much each tends to find, since a
    refusal cuts the list short. Plain-name searches come first: Bilibili
    answers those reliably, while "name + 全集" style queries are the ones its
    risk control tends to refuse."""
    primary = names[0]
    plan = [(primary, 4)] + [(other, 4) for other in names[1:3]]
    if kind == "movie":
        return plan + [(f"{primary} 完整版", 4)]
    # The shorter-duration buckets catch one-video-per-episode collections.
    return plan + [(f"{primary} 全集", 4), (f"{primary} 全集", 3), (f"{primary} 全集", 2)]


async def search_series(client, names: list[str], progress: Progress, kind: str = "tv",
                        runtime: int | None = None) -> Outcome:
    """Find complete uploads of a series (kind="tv") or a film (kind="movie").

    names: search names, primary first (see AliasResolver.resolve).
    """
    out = Outcome(names=list(names), kind=kind, runtime=runtime if kind == "movie" else None)
    found, out.skipped = await _gather(client, _plan(names, kind), "click", progress)
    out.partial = bool(out.skipped)
    await _absorb(client, out, found, progress, MAX_SEARCH_CANDIDATES, "play")
    out.fetched_at = time.time()
    return out


async def latest(client, progress: Progress, now: float | None = None) -> Outcome:
    """Recently uploaded complete US series, newest first."""
    now = time.time() if now is None else now
    out = Outcome()
    found, out.skipped = await _gather(client, [(k, 4) for k in LATEST_KEYWORDS], "pubdate", progress)
    out.partial = bool(out.skipped)
    recent = [h for h in found if h.pubdate >= now - LATEST_WINDOW_S]
    await _absorb(client, out, recent, progress, MAX_LATEST_CANDIDATES, "pubdate")
    out.fetched_at = time.time()
    return out
