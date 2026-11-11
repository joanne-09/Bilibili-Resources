# Streaming Catalog, Movie Search, Throttle Backfill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (the user chose inline execution) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a streaming-catalog tab (Netflix / HBO Max / Prime Video, US: available + upcoming with dates) whose items launch the existing Bilibili search, a movie mode for that search, and a polite backfill for searches Bilibili refused.

**Architecture:** A TMDB client supplies catalog data (key read from a local, git-ignored config file). A separate pure movie classifier sits beside the series classifier. The pipeline records which Bilibili searches were skipped; a background backfiller retries them slowly, one at a time, mutating the stored outcome that the page re-polls from the local server.

**Tech Stack:** unchanged — Python 3.12, FastAPI, httpx, pytest; stdlib `tomllib` for config.

**Spec:** `docs/superpowers/specs/2026-11-11-streaming-catalog-and-backfill-design.md`

## Global Constraints

- Never circumvent Bilibili's bot detection: no fingerprint/ticket forgery, no captcha solving, no user login cookie.
- The TMDB key is only ever read from `config.local.toml` (git-ignored). Never logged, never sent to the browser.
- Missing/invalid key or TMDB failure must show its cause — never "this platform has nothing".
- All existing tests keep passing; series classification behavior does not change.
- Catalog region `US`; titles requested in `zh-CN`; UI text Traditional Chinese.
- Backfill: one Bilibili search per tick, base interval 180 s, at most 3 attempts per skipped query, interval doubles after a refusal, at most 20 jobs, foreground searches take priority.
- Automated tests never contact TMDB or Bilibili.
- Footer credit: `片單資料來自 TMDB 與 JustWatch`.

## Review Focus

1. No config file at all → search and 最新上線 still work; catalog tab shows setup steps (Task 5 test).
2. TMDB returns 401 (bad key) → message says the key is wrong, not "empty" (Task 1 test).
3. A 100-minute "解说" titled like a film with runtime known → excluded by keyword regardless of duration (Task 2 test).
4. A catalog title whose Chinese name contains `&`, `#`, `?` → the launched search still carries the exact name (Task 6 manual check: `encodeURIComponent`).
5. Backfill tick while a foreground search holds the lock → tick does nothing and does not queue up (Task 4 test).

## Tasks

### Task 1: Config and TMDB client

**Files:** create `app/config.py`, `app/tmdb.py`, `config.example.toml`, `tests/test_config.py`, `tests/test_tmdb.py`; modify `.gitignore` (add `config.local.toml`).

**Produces:**

```python
# app/config.py
@dataclass
class Config: tmdb_key: str | None = None
load_config(path: Path = ROOT / "config.local.toml") -> Config      # missing file / missing key / blank -> tmdb_key None; malformed TOML -> None too

# app/tmdb.py
class TmdbError(Exception): ...            # user-facing Traditional Chinese message
class TmdbNotConfigured(TmdbError): ...
PLATFORMS: dict[str, Platform]             # keys "netflix", "hbo", "prime"; Platform(label, provider_id, network_ids, provider_names)
@dataclass
class Title: tmdb_id: int; kind: str; title: str; original_title: str; year: str; date: str; poster: str; season: int | None = None
class TmdbClient:
    def __init__(self, key: str | None, transport=None, today: Callable[[], date] = date.today): ...
    configured: bool
    async def available(self, platform: str, kind: str, page: int = 1) -> tuple[list[Title], bool]   # (items, has_more)
    async def upcoming(self, platform: str, kind: str) -> list[Title]                              # next 90 days, soonest first
    async def runtime(self, movie_id: int) -> int | None                                           # minutes
    async def aclose(self) -> None
```

**Behavior:**
- Auth: key starting with `eyJ` → `Authorization: Bearer <key>`; otherwise `api_key=<key>` query param. Base `https://api.themoviedb.org/3`.
- Defaults (verify live in Task 7): Netflix provider 8 / network 213; HBO Max provider 1899 / networks 49, 3186; Prime Video provider 9 / network 1024. On first use, GET `/watch/providers/tv?watch_region=US` and replace `provider_id` when a provider whose name is in the platform's `provider_names` (`Netflix`; `HBO Max`, `Max`; `Amazon Prime Video`) is listed; failure of this lookup keeps defaults.
- `available`: GET `/discover/{tv|movie}` with `with_watch_providers`, `watch_region=US`, `with_watch_monetization_types=flatrate`, `sort_by=popularity.desc`, `language=zh-CN`, `page`. `has_more = page < total_pages`.
- `upcoming` tv: GET `/discover/tv` with `with_networks` (ids joined by `|`), `air_date.gte=today`, `air_date.lte=today+90d`, `sort_by=popularity.desc`, `language=zh-CN`; then GET `/tv/{id}` for each of the first 20 and keep those with `next_episode_to_air.air_date` inside the window → `date`, `season = season_number`. Sort by date.
- `upcoming` movie: GET `/discover/movie` with `with_watch_providers`, `watch_region=US`, `primary_release_date.gte/lte`, `sort_by=primary_release_date.asc`, `language=zh-CN`; `date = release_date`.
- `Title.title` = `name`/`title`; `original_title` = `original_name`/`original_title`; `year` = first 4 chars of first_air_date/release_date; `poster` = `https://image.tmdb.org/t/p/w342{poster_path}` or `""`.
- Cache every successful call 6 h (`TTLCache`), keyed by path + params (without the key).
- Errors: not configured → `TmdbNotConfigured("尚未設定 TMDB 金鑰")`; 401 → `TmdbError("TMDB 金鑰無效，請檢查 config.local.toml")`; other HTTP/network → `TmdbError("無法取得片單資料（TMDB）")`; unknown platform/kind → `ValueError`.

- [ ] Tests first (MockTransport): config (missing file, blank key, valid key, malformed TOML); bearer vs api_key auth; available maps fields + has_more + request params; upcoming tv keeps only in-window next episodes, sorted, with season; upcoming movie params; runtime; 401 message; not configured raises without any HTTP request; provider id override from the providers list; second identical call is cached; the key never appears in an error message.
- [ ] Run → fail. Implement. Run → pass. Commit `feat: add config loader and TMDB client`.

### Task 2: Movie classifier

**Files:** create `app/movie_classifier.py`, `tests/test_movie_classifier.py`.

**Produces:** `prefilter_movie(hit, names) -> str | None`; `classify_movie(detail, names, runtime_min: int | None) -> ResultItem`.

**Rules:**
- Shared keyword groups from `app.classifier` exclude first (reason unchanged). Not mentioning any name → `與搜尋無關`. `prefilter_movie`: hit shorter than 40 min → `時長過短`.
- Units = parts ≥ 40 min. None → excluded `時長過短`. `longest` = max unit, `total` = sum of all parts ≥ 10 min.
- With runtime R: `longest` within 85–115 % of R → complete; else `total` within 85–115 % and ≥ 2 parts → complete, form label `分段 · N 段`; else `longest < 0.6 R` → excluded `時長遠短於片長（可能是解說）`; else uncertain `時長 {longest} 分，片長應為 {R} 分`.
- Without runtime: `longest` 70–240 min → complete; 40–70 → uncertain `時長偏短，無法確認是正片`; > 240 → uncertain `時長異常，無法確認是正片`.
- Form: one unit → `form="movie"`, label `正片 · {m} 分`; ≥ 2 units each ≥ 70 min → `form="series"`, label `系列合集 · N 部`.
- `coverage=""`; quality/subtitle via the series classifier's helpers; `key=bvid`.

- [ ] Tests first: exact-runtime single film complete; 2-part split complete; 30-min recap-length with runtime 120 excluded as too short; 100-min video titled `…电影解说` excluded by keyword; 95-min video vs runtime 150 uncertain with both numbers; no-runtime 110 complete / 55 uncertain / 300 uncertain; trilogy collection `系列合集 · 3 部` complete when one part matches; unrelated title excluded; `reaction` in title not excluded; prefilter cases.
- [ ] Run → fail. Implement. Run full suite → pass. Commit `feat: add movie classifier`.

### Task 3: Pipeline — movie mode and skipped-query bookkeeping

**Files:** modify `app/pipeline.py`, `tests/test_pipeline.py`.

**Produces:**

```python
Outcome: + kind: str = "tv"; runtime: int | None = None; skipped: list[tuple[str, int]] = []   # (keyword, duration) not run
async def search_series(client, names, progress, kind: str = "tv", runtime: int | None = None) -> Outcome
async def extend_outcome(client, out: Outcome, hits: list[SearchHit], progress, limit: int) -> None
    # prefilter + judge hits whose bvid is not already in out, merge, re-sort by play
bilibili_search_url(keyword: str, duration: int) -> str     # https://search.bilibili.com/all?keyword=…&order=click&duration=…
```

- Movie plan: `[(primary, 4)] + [(other, 4) for other in names[1:3]] + [(primary + " 完整版", 4)]`. Movie mode uses `prefilter_movie` / `classify_movie(detail, names, runtime)`; no collection merging.
- `_gather` returns `(hits, skipped)`: on a refusal after the first search, `skipped` = that query and all remaining ones. `partial = bool(skipped)` (or a mid-way view failure, as today).
- `search_series` is rebuilt on `extend_outcome`; existing behavior and tests unchanged.

- [ ] Tests first: movie plan order; movie mode classifies with runtime (film kept, recap excluded); skipped lists the refused and remaining queries; no refusal → `skipped == []`; `extend_outcome` adds only unseen bvids and keeps play order; URL builder encodes spaces and CJK.
- [ ] Run → fail. Implement. Suite → pass. Commit `feat: movie search mode and skipped-query tracking`.

### Task 4: Backfiller

**Files:** create `app/backfill.py`, `tests/test_backfill.py`.

**Produces:**

```python
class Backfiller:
    def __init__(self, clock=time.monotonic, base_interval: float = 180.0, max_attempts: int = 3, max_jobs: int = 20): ...
    def add(self, key: str, outcome: Outcome) -> None          # no-op when outcome.skipped is empty
    def get(self, key: str) -> Outcome | None                  # the live outcome while its job exists
    def pending(self, key: str) -> int
    async def tick(self, client) -> str | None                 # runs at most one search; returns the key it finished (job complete) or None
```

- A job holds the outcome and per-query `attempts` / `not_before`. `tick` picks the oldest job with a query whose `not_before <= now`, calls `client.search(keyword, order="click", duration=d, patient=False)`.
  - Success → `extend_outcome(..., limit=10)`; remove the query from `outcome.skipped`.
  - `BiliError` → attempts += 1; `not_before = now + base_interval * 2**attempts`; at `max_attempts` the query is given up (stays in `outcome.skipped` so its link remains, but no longer pending).
  - When nothing is pending: `outcome.partial = False` only if `outcome.skipped` is empty; the job is removed and its key returned.
- More than `max_jobs` → drop the oldest. Jobs older than 30 min are dropped.

- [ ] Tests first (fake clock, fake client): add ignores complete outcomes; tick merges new results and clears the skipped query; refusal backs off (no call before `not_before`, call after); three refusals give up and return the key with the link retained; one search per tick; 21st job evicts the first; finished job returns its key and `get` then returns None.
- [ ] Run → fail. Implement. Suite → pass. Commit `feat: add slow backfill of refused searches`.

### Task 5: Server

**Files:** modify `app/server.py`, `tests/test_server.py`.

- `create_app(client=None, resolver=None, tmdb=None, backfill_interval: float | None = 180.0)`; default `tmdb = TmdbClient(load_config().tmdb_key)`.
- `GET /api/search?q=&kind=tv|movie&alt=&tmdb_id=`: `alt` given → `names = dedupe([to_simplified(q), alt])`, resolver not called. `kind=movie` with `tmdb_id` and TMDB configured → `runtime = await tmdb.runtime(id)` (failure → None). Cache key gains the kind. Lookup order: full cache → `backfiller.get(key)` → run. A partial outcome is `backfiller.add`ed (replaces the 3-minute partial cache for searches).
- `result` event gains `kind`, `skipped: [{keyword, duration, url}]`, `backfill_pending: int`.
- `GET /api/catalog?platform=&section=available|upcoming&kind=tv|movie&page=1` → JSON `{configured, items, has_more}`; not configured → `{configured: false, items: [], has_more: false}`; `TmdbError` → HTTP 502 `{error}`; bad platform/section/kind → 400 `{error: "參數錯誤"}`.
- `GET /api/platforms` → `[{id, label}]`.
- Lifespan starts a background loop when `backfill_interval` is not None: every `interval / 6` seconds (tick granularity) call `backfiller.tick` only if the `busy` lock is free; a finished key moves its outcome into the full cache. Cancelled on shutdown.

- [ ] Tests first: catalog not configured; catalog items JSON; catalog TMDB error → 502 with message; bad params → 400; search with `alt` skips the resolver and uses both names; movie search fetches runtime and classifies with it; partial search → `skipped` with URLs and `backfill_pending`; repeat request is served from the backfiller without new Bilibili calls; after a manual `tick` the repeat request shows the merged item and `backfill_pending == 0`; no config → `/api/search` and `/api/latest` still work.
- [ ] Run → fail. Implement. Suite → pass. Commit `feat: catalog endpoints, movie search params, backfill wiring`.

### Task 6: Frontend

**Files:** modify `app/static/index.html`, `tests/manual_stub.py`.

- Third tab `串流片單`. Row 1: platform buttons from `/api/platforms`. Row 2: `現在有的 | 即將上線` and `劇 | 電影`. Poster grid (`auto-fill, minmax(140px, 1fr)`), each cell a `<button>`: poster (lazy, 2:3), title, original title, year; upcoming cells show `M月D日` and `還有 N 天` (or `今天`), plus `第 N 季` when season > 1. `載入更多` when `has_more`. Section note under upcoming: `只包含該平台原創作品的首播日期，不含授權片的上架預告。`
- Not configured → three numbered setup steps. HTTP 502 → red banner with the server's message. Empty upcoming → `這個平台近 90 天沒有查到上線預告。`
- Clicking a cell: switch to 搜尋, fill the input with `title`, set mode, start `/api/search?q=&kind=&alt=&tmdb_id=` (all via `URLSearchParams`).
- Search tab: `劇 | 電影` toggle next to the button (default 劇); a manual search sends `kind` only.
- Partial result: below the note, list `skipped` as links `在 B 站打開：{keyword}`; when `backfill_pending > 0` show `正在背景補齊，剩 {n} 個查詢` and re-request the same URL every 60 s while that result is on screen (stop when pending reaches 0, on a new search, or on tab change).
- Footer: `片單資料來自 TMDB 與 JustWatch`.
- Stub: add a fake TMDB (titles including one named `爱 & 战争 #1?`) and a partial result with skipped queries.

- [ ] Implement. Verify in a browser against the stub: grid renders at 1024×768 and 768×1024 without horizontal scroll; clicking the odd-named title searches that exact name; movie toggle sends `kind=movie`; skipped links point at `search.bilibili.com`; not-configured view; error banner.
- [ ] Commit `feat: streaming catalog tab, movie toggle, backfill UI`.

### Task 7: README and live verification

- [ ] Update `README.md` (Traditional Chinese): the new tab; how to get a TMDB key and where to paste it; what 即將上線 does and does not cover; movie mode and its accuracy; what happens when Bilibili refuses and that the tool does not work around it.
- [ ] If `config.local.toml` holds a key: live-check each platform × section × kind; correct provider/network ids and the spec's 〔待驗證〕 items from what TMDB actually returns; live-check one movie search launched from the catalog. If there is no key yet: say so plainly in the final report; do not claim the catalog is verified.
- [ ] Commit `docs: README for catalog, movies, backfill`.
