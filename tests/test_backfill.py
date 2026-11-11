from app.backfill import Backfiller
from app.pipeline import Outcome, search_series
from tests.test_pipeline import FakeClient, detail, hit, modern_family, noop


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def partial_outcome(fake, fail_on=2):
    """A real pipeline run that Bilibili cut short at search number `fail_on`."""
    fake.fail_on = {fail_on}
    out = await search_series(fake, ["摩登家庭"], noop)
    fake.fail_on = set()
    return out


def fresh():
    hits, details = modern_family()
    fake = FakeClient(hits, details)
    clock = Clock()
    return fake, clock, Backfiller(clock=clock, base_interval=180, max_attempts=3, max_jobs=20)


async def test_complete_outcomes_are_not_queued():
    fake, clock, bf = fresh()
    bf.add("k", Outcome())
    assert bf.get("k") is None and bf.pending("k") == 0
    assert await bf.tick(fake) is None and fake.search_calls == []


async def test_tick_runs_one_search_and_merges_new_results():
    fake, clock, bf = fresh()
    out = await partial_outcome(fake)                       # 3 searches skipped
    bf.add("k", out)
    assert bf.pending("k") == 3 and bf.get("k") is out
    fake.details["N"] = detail("N", "摩登家庭 全11季 蓝光 未删减", [380] * 20, play=50)
    fake.by_keyword["摩登家庭 全集"] = [hit("N", "摩登家庭 全11季 蓝光 未删减", play=50)]
    calls = len(fake.search_calls)

    assert await bf.tick(fake) is None                      # job not finished yet
    assert len(fake.search_calls) == calls + 1              # exactly one search per tick
    assert fake.search_calls[-1] == ("摩登家庭 全集", "click", 4) and fake.patience[-1] is False
    assert [i.bvid for i in out.complete] == ["B", "N", "A"]
    assert out.skipped == [("摩登家庭 全集", 3), ("摩登家庭 全集", 2)]
    assert bf.pending("k") == 2 and out.partial is True


async def test_job_finishes_and_clears_partial():
    fake, clock, bf = fresh()
    out = await partial_outcome(fake)
    bf.add("k", out)
    assert await bf.tick(fake) is None
    assert await bf.tick(fake) is None
    assert await bf.tick(fake) == "k"
    assert out.skipped == [] and out.partial is False
    assert bf.get("k") is None


async def test_refusal_backs_off_before_retrying():
    fake, clock, bf = fresh()
    out = await partial_outcome(fake, fail_on=4)            # only the last search skipped
    bf.add("k", out)
    calls = len(fake.search_calls)
    fake.fail_on = {calls + 1}
    await bf.tick(fake)                                     # refused: attempt 1
    assert len(fake.search_calls) == calls + 1 and bf.pending("k") == 1

    clock.now = 300                                         # backoff is 180 * 2 = 360 s
    await bf.tick(fake)
    assert len(fake.search_calls) == calls + 1              # too early: no request
    clock.now = 361
    assert await bf.tick(fake) == "k"
    assert len(fake.search_calls) == calls + 2 and out.skipped == []


async def test_gives_up_after_max_attempts_but_keeps_the_link():
    fake, clock, bf = fresh()
    out = await partial_outcome(fake, fail_on=4)
    bf.add("k", out)
    calls = len(fake.search_calls)
    fake.fail_on = {calls + 1, calls + 2, calls + 3}
    finished = None
    for _ in range(3):
        finished = await bf.tick(fake)
        clock.now += 800   # past each backoff (360 s, then 720 s) but inside the job's 30-minute life
    assert finished == "k" and len(fake.search_calls) == calls + 3
    assert out.skipped == [("摩登家庭 全集", 2)] and out.partial is True
    assert bf.pending("k") == 0 and bf.get("k") is None


async def test_oldest_job_is_evicted_beyond_the_limit():
    fake, clock, bf = fresh()
    for i in range(21):
        bf.add(f"k{i}", Outcome(skipped=[("x", 4)], partial=True, names=["x"]))
    assert bf.get("k0") is None and bf.get("k1") is not None and bf.get("k20") is not None


async def test_stale_jobs_are_dropped():
    fake, clock, bf = fresh()
    bf.add("k", Outcome(skipped=[("x", 4)], partial=True, names=["x"]))
    clock.now = 31 * 60
    assert await bf.tick(fake) is None
    assert bf.get("k") is None and fake.search_calls == []
