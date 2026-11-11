"""Search orchestration: gather candidates from Bilibili, classify, merge, sort."""
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.bilibili import BiliError
from app.classifier import classify, excluded_from_hit, prefilter
from app.models import ResultItem, SearchHit

Progress = Callable[[str], Awaitable[None]]

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


async def _gather(client, plan: list[tuple[str, int]], order: str, progress: Progress) -> list[SearchHit]:
    """Run each (keyword, duration) search; dedupe hits by bvid.

    A failure on the first search propagates. Later failures are tolerated as
    long as something already succeeded: partial results beat none.
    """
    hits: dict[str, SearchHit] = {}
    succeeded = False
    for i, (keyword, duration) in enumerate(plan, 1):
        await progress(f"正在查 B 站（{i}/{len(plan)}）…")
        try:
            found = await client.search(keyword, order=order, duration=duration)
        except BiliError:
            if not succeeded:
                raise
            continue
        succeeded = True
        for h in found:
            hits.setdefault(h.bvid, h)
    return list(hits.values())


async def _judge(client, candidates: list[SearchHit], names: list[str] | None,
                 out: Outcome, progress: Progress) -> None:
    seen_keys: set[str] = set()
    covered: set[str] = set()  # bvids already represented by a collection result
    for i, h in enumerate(candidates, 1):
        if i == 1 or i % 5 == 0:
            await progress(f"正在確認候選（{i}/{len(candidates)}）· 已找到 {len(out.complete)} 個完整版")
        if h.bvid in covered:
            continue
        detail = await client.view(h.bvid)
        if detail is None:
            continue  # taken down since it was indexed
        item = classify(detail, names)
        if item.key in seen_keys:
            continue
        seen_keys.add(item.key)
        if item.form == "collection":
            covered.update(ep.bvid for ep in detail.season_eps)
        if not item.cover:
            item.cover = h.cover
        getattr(out, item.status).append(item)


async def search_series(client, names: list[str], progress: Progress) -> Outcome:
    """names: search names, primary first (see AliasResolver.resolve)."""
    primary = names[0]
    plan = [(primary, 4), (f"{primary} 全集", 4), (f"{primary} 全集", 3),
            (f"{primary} 全集", 2), (f"{primary} 未删减", 0)]
    plan += [(f"{other} 全集", 4) for other in names[1:3]]

    out = Outcome(names=list(names))
    candidates = []
    for h in await _gather(client, plan, "click", progress):
        reason = prefilter(h, names)
        if reason:
            out.excluded.append(excluded_from_hit(h, reason))
        else:
            candidates.append(h)
    candidates.sort(key=lambda h: h.play, reverse=True)

    await _judge(client, candidates[:MAX_SEARCH_CANDIDATES], names, out, progress)
    for group in (out.complete, out.uncertain, out.excluded):
        group.sort(key=lambda i: i.play, reverse=True)
    out.fetched_at = time.time()
    return out


async def latest(client, progress: Progress, now: float | None = None) -> Outcome:
    """Recently uploaded complete US series, newest first."""
    now = time.time() if now is None else now
    out = Outcome()
    candidates = []
    for h in await _gather(client, [(k, 4) for k in LATEST_KEYWORDS], "pubdate", progress):
        if h.pubdate < now - LATEST_WINDOW_S:
            continue
        reason = prefilter(h, None)
        if reason:
            out.excluded.append(excluded_from_hit(h, reason))
        else:
            candidates.append(h)
    candidates.sort(key=lambda h: h.pubdate, reverse=True)

    await _judge(client, candidates[:MAX_LATEST_CANDIDATES], None, out, progress)
    for group in (out.complete, out.uncertain, out.excluded):
        group.sort(key=lambda i: i.pubdate, reverse=True)
    out.fetched_at = time.time()
    return out
