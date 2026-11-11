"""Slowly re-asks Bilibili for the searches it refused, one per tick.

This does not work around Bilibili's risk control. It simply asks again later,
rarely, backs off further each time it is refused, and gives up after a few
tries. The outcome object is updated in place, so whoever holds it (the
server's cache) sees results fill in.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.bilibili import BiliError
from app.pipeline import Outcome, Query, extend_outcome

MAX_JOB_AGE_S = 30 * 60
BACKFILL_CANDIDATES = 10


@dataclass
class _Retry:
    attempts: int = 0
    not_before: float = 0.0


@dataclass
class _Job:
    outcome: Outcome
    created: float
    pending: dict[Query, _Retry] = field(default_factory=dict)


async def _quiet(message: str) -> None:
    pass


class Backfiller:
    def __init__(self, clock: Callable[[], float] = time.monotonic, base_interval: float = 180.0,
                 max_attempts: int = 3, max_jobs: int = 20):
        self._clock, self._base = clock, base_interval
        self._max_attempts, self._max_jobs = max_attempts, max_jobs
        self._jobs: dict[str, _Job] = {}  # insertion order = age

    def add(self, key: str, outcome: Outcome) -> None:
        """Queue the searches an outcome is missing. No-op when it is complete."""
        if not outcome.skipped:
            return
        self._jobs.pop(key, None)
        self._jobs[key] = _Job(outcome, self._clock(), {tuple(q): _Retry() for q in outcome.skipped})
        while len(self._jobs) > self._max_jobs:
            del self._jobs[next(iter(self._jobs))]

    def get(self, key: str) -> Outcome | None:
        job = self._jobs.get(key)
        return job.outcome if job else None

    def pending(self, key: str) -> int:
        job = self._jobs.get(key)
        return len(job.pending) if job else 0

    async def tick(self, client) -> str | None:
        """Run at most one refused search. Returns the key of a job that just
        finished (nothing left to retry), else None."""
        now = self._clock()
        for key in [k for k, j in self._jobs.items() if now - j.created > MAX_JOB_AGE_S]:
            del self._jobs[key]

        for key, job in self._jobs.items():
            query = next((q for q, r in job.pending.items() if r.not_before <= now), None)
            if query is None:
                continue
            await self._retry(client, job, query, now)
            if job.pending:
                return None
            job.outcome.partial = bool(job.outcome.skipped)
            del self._jobs[key]
            return key
        return None

    async def _retry(self, client, job: _Job, query: Query, now: float) -> None:
        keyword, duration = query
        try:
            hits = await client.search(keyword, order="click", duration=duration, patient=False)
            await extend_outcome(client, job.outcome, hits, _quiet, limit=BACKFILL_CANDIDATES)
        except BiliError:
            retry = job.pending[query]
            retry.attempts += 1
            if retry.attempts >= self._max_attempts:
                del job.pending[query]  # give up; it stays in outcome.skipped as a link
            else:
                retry.not_before = now + self._base * 2 ** retry.attempts
            return
        del job.pending[query]
        job.outcome.skipped = [q for q in job.outcome.skipped if tuple(q) != query]
