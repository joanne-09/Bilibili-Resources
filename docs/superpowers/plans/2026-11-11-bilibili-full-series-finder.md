# Bilibili Full-Series Finder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local web app (used from an iPad on the same Wi-Fi) that searches Bilibili for a series name and lists only complete uploads sorted by play count, plus a "newly uploaded" tab.

**Architecture:** A Python FastAPI service on the Windows PC queries Bilibili's web search/view endpoints anonymously (WBI-signed, throttled), runs every candidate through a pure, network-free classifier, merges collections, and streams progress + results as NDJSON to a single static HTML page.

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, httpx, opencc-python-reimplemented, pytest, pytest-asyncio. Frontend: one HTML file + vanilla JS/CSS, no build step.

**Spec:** `docs/superpowers/specs/2026-11-11-bilibili-full-series-finder-design.md`

## Global Constraints

- No Bilibili login, no stored user cookies; anonymous visitor cookies only, held in memory.
- No netdisk search, no in-app playback, no downloading, no database. Caches are in-memory with TTL.
- Never silently drop a possibly-complete upload: anything not clearly excluded is `complete` or `uncertain` (with a reason); excluded items are returned too, with a reason.
- The word `reaction` in a title is never an exclusion signal.
- A Bilibili failure or throttle must surface as an error message, never as "no results".
- All UI text is Traditional Chinese. User input is converted to Simplified before searching.
- Search requests to Bilibili are spaced ≥ 2.0 s apart; view requests ≥ 0.5 s apart.
- Server binds `0.0.0.0:8756`.
- Automated tests never hit the real Bilibili; use `httpx.MockTransport` and in-code fixtures.
- Titles/descriptions from Bilibili are untrusted text: render with `textContent`, never `innerHTML`.

## Review Focus

1. Traditional-Chinese input (`絕命毒師`) must search as `绝命毒师` — test in Task 1.
2. Throttled response (code 0, no `numResults`) must retry then raise, not return `[]` — test in Task 3.
3. A candidate whose view call fails (deleted video, code -404/62002) must be skipped; the search still returns the others — test in Task 4.
4. Empty / whitespace-only query must return a clear error event, with no Bilibili request made — test in Task 5.
5. A title containing HTML (`<img onerror=…>`) must render as literal text — manual check in Task 6.

## Amendment (2026-11-11): search by English or Taiwan title

Added by the user after plan review; spec §9 updated. It changes these points in the tasks below:

- **New module `app/aliases.py`, built as Task 3b** (after Task 3, before Task 4), test file `tests/test_aliases.py`:

  ```python
  has_cjk(text: str) -> bool
  class AliasResolver:
      def __init__(self, transport: httpx.AsyncBaseTransport | None = None): ...
      async def resolve(self, query: str) -> list[str]   # never raises
      async def aclose(self) -> None
  ```

  - `resolve` returns the search names, primary first, deduped by `normalize()`: `[chinese_name, english_name]` when found, always including `to_simplified(query.strip())`. The primary is the Chinese name whenever one is known.
  - Source 1 — Douban: GET `https://movie.douban.com/j/subject_suggest?q=` (desktop UA, `Referer: https://movie.douban.com/`, timeout 6 s). Take the first entry whose `episode` is non-empty and where `normalize(query)` is contained in `normalize(title)` or `normalize(sub_title)`. Chinese name = `title` with a trailing `第…季` (and surrounding spaces) removed; English name = `sub_title.strip()`.
  - Source 2 — Wikidata, only when Douban gave nothing and the query has no CJK: GET `https://www.wikidata.org/w/api.php?action=wbsearchentities&language=en&uselang=zh-cn&format=json&limit=3` (UA `series-title-lookup/0.1`). Take the first entry whose `label` contains CJK and whose `description` contains `电视`, `剧`, or `television series`, excluding descriptions containing `season` or `集别`.
  - Any HTTP error, timeout, or bad JSON → fall through; final fallback is `[to_simplified(query.strip())]`.
  - Results cached in a `TTLCache(86400)` keyed by `normalize(query)`.
  - Tests (MockTransport): `breaking bad` → `["绝命毒师", "breaking bad"]`-equivalent (assert `names[0] == "绝命毒师"` and `"Breaking Bad"` in names); `六人行` → `names[0] == "老友记"`; Douban `[]` + Wikidata hit for `wednesday` → `names[0] == "星期三"`; Douban 500 and Wikidata timeout → `["wednesday"]`; an unrelated Douban suggestion (title/sub_title not containing the query) is ignored; second call for the same query makes no HTTP request; a movie entry (`episode == ""`) is skipped.

- **Task 2:** `prefilter(hit, names: list[str] | None)` and `classify(detail, names: list[str] | None)` take a list instead of one string. "Relevant" = any name's `normalize()` is contained in the normalized title; a season is used when any name is contained in the season title. Existing tests pass `["摩登家庭"]` in place of `"摩登家庭"`. Add:

  ```python
  def test_either_name_makes_a_title_relevant():
      names = ["绝命毒师", "Breaking Bad"]
      assert prefilter(hit("Breaking.Bad S01-S05 1080P 全集", 2900), names) is None
      assert prefilter(hit("绝命毒师 全五季 未删减", 2900), names) is None
      assert prefilter(hit("以前的化学课都弱爆了", 90), names) == "與搜尋無關"
  ```

- **Task 4:** `search_series(client, names: list[str], progress)`. The five searches run on `names[0]`; for each further name add one search `(name + " 全集", 4)` (max 2 extra). `Outcome` gains `names: list[str]`.
  ```python
  async def test_second_name_adds_one_search_and_matches():
      out = await search_series(fake, ["绝命毒师", "Breaking Bad"], noop)
      assert fake.search_calls[5] == ("Breaking Bad 全集", "click", 4)
      assert "EN1" in [i.bvid for i in out.complete]      # hit titled "Breaking Bad 全集 1080P"
  ```
- **Task 5:** the server owns one `AliasResolver`; `/api/search` emits progress `正在查詢劇名對應…`, calls `resolve(q)`, then `search_series`. The search cache key is `"|".join(normalize(n) for n in names)`, so `breaking bad` and `绝命毒师` share one cached result. The `result` event gains `"names": [...]`. `create_app(client=None, resolver=None)`. Add a test that `q=breaking bad` with a fake resolver returning `["绝命毒师", "Breaking Bad"]` yields `result["names"] == ["绝命毒师", "Breaking Bad"]` and that a following `q=绝命毒师` (resolver returns the same list) is `cached: true`.
- **Task 6:** hint text becomes `可輸入英文原名、台灣或大陸譯名，會自動對應。`; when `names` has more than one entry show `同時搜尋：{names joined by " / "}` above the summary line. Empty-result text becomes `找不到完整版資源。可以試試別的譯名或英文原名。`
- **Task 7:** live-check `breaking bad` vs `绝命毒师` (same result list expected) and `六人行`.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | deps, pytest config (`asyncio_mode = "auto"`) |
| `app/models.py` | dataclasses shared by all modules |
| `app/textnorm.py` | simplified conversion, title normalization, numeral/duration parsing |
| `app/classifier.py` | pure verdict logic (no I/O) |
| `app/bilibili.py` | HTTP client: cookies, WBI signing, search, view, throttle, retry |
| `app/cache.py` | TTL cache |
| `app/pipeline.py` | orchestrates search + latest |
| `app/server.py` | FastAPI app, NDJSON endpoints, static files |
| `app/static/index.html` | the whole frontend |
| `run.py`, `start.bat` | launcher, prints LAN URL |
| `tests/…` | one test file per module |

---

### Task 1: Project scaffold, models, text normalization

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/models.py`, `app/textnorm.py`, `tests/__init__.py`, `tests/test_textnorm.py`

**Interfaces — Produces:**

```python
# app/models.py  (all @dataclass)
SearchHit(bvid: str, title: str, duration_s: int, play: int, pubdate: int,
          typename: str, author: str, cover: str, desc: str, tag: str)
Part(name: str, duration_s: int)
SeasonEp(bvid: str, title: str, duration_s: int, play: int)
VideoDetail(bvid: str, title: str, desc: str, play: int, pubdate: int, author: str,
            cover: str, width: int, height: int, parts: list[Part],
            season_id: int | None, season_title: str | None, season_eps: list[SeasonEp])
ResultItem(key: str, bvid: str, url: str, title: str, cover: str, play: int, pubdate: int,
           total_minutes: int, form: str, form_label: str, coverage: str, quality: str,
           subtitle: str, uploader: str, status: str, reason: str)
# status ∈ {"complete","uncertain","excluded"}; form ∈ {"merged","episodic","collection",""}

# app/textnorm.py
to_simplified(text: str) -> str          # OpenCC 't2s'
normalize(text: str) -> str              # simplified, lowercased, strip <em> tags, drop everything except CJK/letters/digits
cn_to_int(text: str) -> int | None       # "十一"->11, "五"->5, "11"->11, "二十四"->24
parse_mmss(text: str) -> int             # "8135:14" -> 488114 ; "18:0" -> 1080
```

- [ ] **Step 1:** Create `pyproject.toml` (project name `bili-finder`, requires-python `>=3.12`, deps listed in Tech Stack; dev group pytest + pytest-asyncio; `[tool.pytest.ini_options] asyncio_mode = "auto"`), run `uv sync`. Expected: resolves and creates `.venv`.

- [ ] **Step 2: Write failing tests** in `tests/test_textnorm.py`:

```python
from app.textnorm import to_simplified, normalize, cn_to_int, parse_mmss

def test_traditional_input_becomes_simplified():
    assert to_simplified("絕命毒師") == "绝命毒师"

def test_normalize_strips_evasion_and_markup():
    assert normalize("《摩登-家庭 第五季》") == "摩登家庭第五季"
    assert normalize('<em class="keyword">摩登家庭</em> 全11季') == "摩登家庭全11季"
    assert normalize("Breaking Bad") == "breakingbad"

def test_cn_to_int():
    assert [cn_to_int(x) for x in ["五", "十", "十一", "二十四", "11", "abc"]] == [5, 10, 11, 24, 11, None]

def test_parse_mmss():
    assert parse_mmss("8135:14") == 488114
    assert parse_mmss("18:0") == 1080
```

- [ ] **Step 3:** Run `uv run pytest tests/test_textnorm.py -v`. Expected: FAIL (import error).
- [ ] **Step 4:** Implement `app/models.py` and `app/textnorm.py` per the Interfaces block.
- [ ] **Step 5:** Run the tests. Expected: 4 passed.
- [ ] **Step 6:** Commit: `feat: scaffold project, models, text normalization`.

---

### Task 2: Classifier

**Files:**
- Create: `app/classifier.py`, `tests/test_classifier.py`

**Interfaces:**
- Consumes: `SearchHit`, `VideoDetail`, `Part`, `SeasonEp`, `ResultItem`, `normalize`, `cn_to_int`.
- Produces:

```python
prefilter(hit: SearchHit, query: str | None) -> str | None
    # returns an exclusion reason (Traditional Chinese) or None to keep as candidate
classify(detail: VideoDetail, query: str | None) -> ResultItem
    # always returns an item; status says where it goes
```

**Rules (pinned values):**

Keyword groups, matched against `to_simplified(title).lower()`:
- RECAP → reason `解說／速看`: `解说 解读 一口气 速看 分钟看完 详解 解析 回顾 盘点 影评 讲解`
- STUDY → `學英語筆記`: `学英语 单词 笔记 跟读 精讲 口语 听力`
- CLIP → `剪輯／片段`: `混剪 片段 名场面 花絮 预告 笑场 cut`
- NETDISK → `網盤引流`: `网盘 自取 免费分享 私信 加群`

`prefilter` order: keyword groups → (if `query`) `normalize(query) not in normalize(title)` → `與搜尋無關`. (If `query` is None: title+tag must contain `美剧`, else `非美劇`.) Then `duration_s < 600` → `時長過短`. Otherwise None. Description text is never used to exclude.

`classify`:
1. Re-run the keyword groups on `detail.title` (season hits arrive without prefilter) → `excluded`.
2. Units: if `query` and `season_eps` has ≥ 2 entries and `normalize(query) in normalize(season_title)` → units = season episode durations, `form="collection"`, `key=f"season:{season_id}"`, `play=sum(ep.play)`, `url` → first episode's bvid. Else units = `parts` durations, `key=bvid`, `play=detail.play`.
3. Content units = units with duration ≥ 15 min. `total = sum(content)`, `n = len(content)`, `avg = total / n`.
4. `n == 0` or `total < 60 min` → `excluded`, reason `時長過短`.
5. Form (when not collection): `avg >= 90 min` → `merged`, label `合併長片 · {n} 段`; else `episodic`, label `一集一段 · {n} 集`. Collection label: `合集 · {n} 支影片`.
6. Uncertain checks, first hit wins:
   - Title has an episode range (two numbers joined by `-`/`~`/`–`, not followed by `季`, e.g. `（1-9）`, `1-12集`) and has none of `全集`, `全N集`, `完结` → reason `只有 {a}–{b} 集`.
   - Title claims `全N集` and `total < N * 18 min` → `時長不足：聲稱 {N} 集，實際僅 {hours:.1f} 小時`.
   - No `全集`/`全N集`/`全N季`/`完结`/`未删减`/`无删` claim, and (`episodic` with `n < 6`, or `merged` with `total < 180 min`) → `內容偏少，無法確認完整`.
   - Otherwise `complete`, reason `""`.
7. Labels:
   - `coverage`: `全十一季`/`全11季` → `全 11 季`; `S1~S5`/`1-10季` → `第 1–5 季`; `第五季`/`第5季`/`S05` → `第 5 季`; else `""`.
   - `subtitle` (first match): `纯英|英文字幕|无中文` → `純英文`; `双语|双字|中英` → `雙語`; `中字|中文字幕|熟肉` → `中字`; else `未知`.
   - `quality` from `min(width, height)`: ≥2160 `4K`, ≥1080 `1080P`, ≥720 `720P`, else `{n}P`; 0 → `未知`.
   - `url = https://www.bilibili.com/video/{bvid}`; `total_minutes = total // 60`.

- [ ] **Step 1: Write failing tests.** In `tests/test_classifier.py` add helpers `hit(title, dur_min, **kw)` and `detail(title, part_minutes: list[int], w=1920, h=1080, **kw)`, then:

```python
def test_merged_full_series_is_complete():          # BV1G1s2zvEbF
    r = classify(detail("【摩登家庭 全十一季】全250集 超清中字（未删减版）reaction", [379,391,375,374]*5+[323,325]), "摩登家庭")
    assert (r.status, r.form, r.coverage, r.quality, r.subtitle) == ("complete", "merged", "全 11 季", "1080P", "中字")
    assert r.form_label == "合併長片 · 22 段"

def test_one_episode_per_part_is_complete():        # user requirement
    r = classify(detail("摩登家庭 第一季 中英双字", [22]*24), "摩登家庭")
    assert (r.status, r.form, r.form_label, r.coverage, r.subtitle) == ("complete", "episodic", "一集一段 · 24 集", "第 1 季", "雙語")

def test_evasion_title_and_short_outro_part():      # BV1Ny6xBSEp4
    r = classify(detail("《摩登-家庭 第五季》全24集 超清双字", [286,285,286,285,11]), "摩登家庭")
    assert (r.status, r.form_label, r.coverage) == ("complete", "合併長片 · 4 段", "第 5 季")

def test_collection_merges_and_sums_play():
    eps = [SeasonEp(f"BV{i}", f"绝命毒师 S01E0{i}", 47*60, 1000) for i in range(1, 8)]
    r = classify(detail("绝命毒师 S01E03", [47], season_id=9, season_title="绝命毒师 第一季", season_eps=eps), "绝命毒师")
    assert (r.status, r.form, r.key, r.play, r.bvid) == ("complete", "collection", "season:9", 7000, "BV1")

def test_unrelated_season_is_ignored():
    eps = [SeasonEp("BVx", "别的剧", 3000, 5)] * 3
    r = classify(detail("绝命毒师 全集 未删减", [280, 290, 300], season_id=1, season_title="美剧合集", season_eps=eps), "绝命毒师")
    assert (r.key, r.form) == (r.bvid, "merged")

def test_partial_range_is_uncertain():
    r = classify(detail("【摩登家庭|第一季（1-9）】中英字幕", [172]), "摩登家庭")
    assert (r.status, r.reason) == ("uncertain", "只有 1–9 集")

def test_season_range_is_not_partial():
    for t in ["《怪奇物语》未删减版S1~S5季全集 4K", "老友记 1-10季全集"]:
        assert classify(detail(t, [400]*6), None).status == "complete"

def test_claimed_episodes_but_too_short_is_uncertain():
    r = classify(detail("某剧 全24集", [100]), "某剧")
    assert r.status == "uncertain" and r.reason.startswith("時長不足")

def test_recap_and_study_and_tiny_are_excluded():
    assert classify(detail("一口气看完经典高分美剧《风骚律师》1-5季全集", [80,76,97,78,76]), "风骚律师").status == "excluded"
    assert classify(detail("《摩登家庭》第一季|单词笔记|英语学习", [7]*84), "摩登家庭").status == "excluded"
    assert classify(detail("摩登家庭 全集", [7]*84), "摩登家庭").reason == "時長過短"

def test_reaction_is_not_an_exclusion():
    assert classify(detail("2026美剧《塔尔萨之王 第四季》全集 蓝光超清未删减reaction", [391]), None).status == "complete"

def test_prefilter():
    assert prefilter(hit("美剧 老友记 1-10季超清中英字幕1080p全集 网盘分享 自取", 1), "老友记") == "網盤引流"
    assert prefilter(hit("安徽蒙城惊现“绝命毒师”", 4), "绝命毒师") == "時長過短"
    assert prefilter(hit("以前的化学课都弱爆了", 90), "绝命毒师") == "與搜尋無關"
    assert prefilter(hit("【摩登-家庭】全11季 超清中字", 7483), "摩登家庭") is None
    assert prefilter(hit("【无职转生 全三季 全集】", 2234), None) == "非美劇"
    assert prefilter(hit("星期三第一季 全集中英双字无删减", 691, tag="美剧,悬疑"), None) is None

def test_4k_quality():
    assert classify(detail("某剧 全集", [300, 300], w=3840, h=2160), None).quality == "4K"
```

- [ ] **Step 2:** Run `uv run pytest tests/test_classifier.py -v`. Expected: FAIL (import error).
- [ ] **Step 3:** Implement `app/classifier.py` per the Rules block.
- [ ] **Step 4:** Run the tests. Expected: all pass.
- [ ] **Step 5:** Commit: `feat: add completeness classifier`.

---

### Task 3: Bilibili client

**Files:**
- Create: `app/bilibili.py`, `tests/test_bilibili.py`

**Interfaces:**
- Consumes: `SearchHit`, `VideoDetail`, `Part`, `SeasonEp`, `parse_mmss`.
- Produces:

```python
class BiliError(Exception): ...            # message is user-facing Traditional Chinese
mixin_key(img_key: str, sub_key: str) -> str
sign(params: dict, key: str, wts: int) -> dict     # adds wts and w_rid
class BiliClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None,
                 search_interval: float = 2.0, view_interval: float = 0.5,
                 retry_waits: tuple[float, ...] = (3.0, 6.0, 12.0)): ...
    async def search(self, keyword: str, order: str = "click", duration: int = 0, page: int = 1) -> list[SearchHit]
    async def view(self, bvid: str) -> VideoDetail | None     # None when the video is gone
    async def aclose(self) -> None
```

**Behavior (verified against the live site on 2026-11-11):**
- Session headers: a desktop Chrome `User-Agent`, `Referer: https://www.bilibili.com/`.
- Lazy init, once: GET `https://www.bilibili.com/`; GET `https://api.bilibili.com/x/frontend/finger/spi` → set cookies `buvid3=data.b_3`, `buvid4=data.b_4`; GET `https://api.bilibili.com/x/web-interface/nav` → `data.wbi_img.img_url/sub_url`, keys are the filename stems. Cache the mixin key 1 hour.
- `mixin_key`: concatenate `img_key + sub_key`, reorder by the standard 64-entry WBI table (`[46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]`), take the first 32 chars.
- `sign`: add `wts`; strip `!'()*` from string values; `w_rid = md5(urlencode(sorted(params)) + key)`.
- Search: GET `https://api.bilibili.com/x/web-interface/wbi/search/type` with `search_type=video, keyword, order, duration, page`. `duration` codes: 0 all, 1 <10 min, 2 10–30, 3 30–60, 4 >60.
  - `code != 0` → `BiliError("B 站查詢失敗（代碼 {code}）")`.
  - `code == 0` and `"numResults" not in data` → **throttled**: sleep each of `retry_waits` and retry; still throttled → `BiliError("B 站暫時限制查詢，請稍後再試")`.
  - `numResults == 0` → `[]`.
  - Map results: strip `<em …>`/`</em>` from title; `duration_s = parse_mmss(duration)`; `cover = "https:" + pic` when `pic` starts with `//`.
- View: GET `https://api.bilibili.com/x/web-interface/view?bvid=`. `code != 0` → `None`. Map `title, desc, stat.view, pubdate, owner.name, pic, dimension.width/height, pages[].part/duration`; `ugc_season` → `id, title`, episodes flattened across `sections[].episodes[]` as `SeasonEp(bvid, title, arc.duration, arc.stat.view)`.
- Throttle: an `asyncio.Lock` plus last-call timestamps so search calls start ≥ `search_interval` apart and view calls ≥ `view_interval` apart.
- Network/timeout errors (`httpx.HTTPError`) → `BiliError("無法連線到 B 站")`.

- [ ] **Step 1: Write failing tests** (`httpx.MockTransport` handler routing by URL path; all intervals and waits set to 0):

```python
def test_mixin_key_known_vector():
    assert mixin_key("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eea6f01bf08b70ac45") == "ea1db124af3c7062474693fa704f4ff8"

def test_sign_is_md5_of_sorted_query_plus_key():
    out = sign({"b": "x!'()*y", "a": 1}, "KEY", wts=100)
    assert out["wts"] == 100
    assert out["w_rid"] == hashlib.md5(b"a=1&b=xy&wts=100KEY").hexdigest()

async def test_search_maps_fields():
    # handler returns one result: title '<em class="keyword">摩登家庭</em> 全集', duration "8135:14", pic "//i0.hdslb.com/a.jpg", play 5
    hits = await client.search("摩登家庭", duration=4)
    assert (hits[0].title, hits[0].duration_s, hits[0].cover) == ("摩登家庭 全集", 488114, "https://i0.hdslb.com/a.jpg")

async def test_search_request_is_signed():
    # handler records request.url.params
    assert {"w_rid", "wts", "keyword", "order", "duration"} <= set(seen_params)

async def test_throttled_then_ok_retries():
    # first search response {"code":0,"data":{"seid":"1"}}, second is a normal result
    assert len(await client.search("x")) == 1 and search_calls == 2

async def test_throttled_forever_raises_not_empty():
    with pytest.raises(BiliError, match="暫時限制"):
        await client.search("x")

async def test_true_zero_results_returns_empty():
    # {"code":0,"data":{"numResults":0}}
    assert await client.search("x") == []

async def test_error_code_raises():
    # {"code":-412}
    with pytest.raises(BiliError, match="-412"): await client.search("x")

async def test_view_maps_parts_and_season():
    d = await client.view("BV1")
    assert [p.duration_s for p in d.parts] == [60, 120]
    assert (d.season_id, d.season_title, len(d.season_eps), d.season_eps[0].play) == (9, "S", 2, 7)
    assert (d.width, d.height) == (1920, 1080)

async def test_view_of_deleted_video_returns_none():
    # {"code":-404}
    assert await client.view("BVgone") is None
```

- [ ] **Step 2:** Run `uv run pytest tests/test_bilibili.py -v`. Expected: FAIL (import error).
- [ ] **Step 3:** Implement `app/bilibili.py` per the Behavior block.
- [ ] **Step 4:** Run the tests. Expected: all pass.
- [ ] **Step 5:** Commit: `feat: add throttled, WBI-signed Bilibili client`.

---

### Task 4: Cache and pipeline

**Files:**
- Create: `app/cache.py`, `app/pipeline.py`, `tests/test_cache.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `BiliClient.search/view`, `BiliError`, `prefilter`, `classify`, `to_simplified`, `ResultItem`.
- Produces:

```python
# app/cache.py
class TTLCache:
    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic): ...
    def get(self, key: str) -> Any | None
    def set(self, key: str, value: Any) -> None

# app/pipeline.py
Progress = Callable[[str], Awaitable[None]]
@dataclass
class Outcome: complete: list[ResultItem]; uncertain: list[ResultItem]; excluded: list[ResultItem]; fetched_at: float
async def search_series(client, query: str, progress: Progress) -> Outcome
async def latest(client, progress: Progress, now: float | None = None) -> Outcome
```

**Behavior:**
- `search_series`: `q = to_simplified(query.strip())`. Run, in order, page 1, `order="click"`: `(q, 4)`, `(q + " 全集", 4)`, `(q + " 全集", 3)`, `(q + " 全集", 2)`, `(q + " 未删减", 0)`. Dedupe by bvid. `prefilter(hit, q)`: rejected hits become `excluded` ResultItems built from the hit alone (form/quality/coverage empty, `reason` set). Keep the top 20 survivors by play; `view` each; `None` → skip. `classify(detail, q)`; dedupe by `item.key` (first wins; skip `view` for a bvid already covered by a classified season's episodes). Sort `complete` and `uncertain` by play desc.
- `latest`: keywords `["美剧 全集", "美剧 未删减", "美剧 双字"]`, `order="pubdate"`, `duration=4`, page 1. Keep hits with `pubdate >= now - 30 days`; `prefilter(hit, None)`; top 30 by pubdate; `view` + `classify(detail, None)`. Sort `complete`/`uncertain` by pubdate desc.
- Progress messages (Traditional Chinese), one per Bilibili search and every 5 views: `正在查 B 站（{i}/{n}）…`, `正在確認候選（{i}/{n}）· 已找到 {k} 個完整版`.
- A `BiliError` on the **first** search propagates. On a later search it is swallowed if at least one earlier search succeeded (partial results beat none).
- Module-level caches used by the server (Task 5), not inside these functions.

- [ ] **Step 1: Write failing tests.** Use a `FakeClient` with dict-driven `search` (keyed by `(keyword, duration)`) and `view`, recording calls.

```python
def test_ttl_cache_expires():
    t = [0.0]; c = TTLCache(10, clock=lambda: t[0])
    c.set("k", 1); assert c.get("k") == 1
    t[0] = 11; assert c.get("k") is None

async def test_traditional_query_is_searched_in_simplified():
    await search_series(fake, " 絕命毒師 ", noop)
    assert fake.search_calls[0] == ("绝命毒师", "click", 4)
    assert len(fake.search_calls) == 5

async def test_groups_and_sorts_by_play():
    # hits: full A (play 10), full B (play 99), partial "（1-9）" C, recap D, unrelated E
    out = await search_series(fake, "摩登家庭", noop)
    assert [i.bvid for i in out.complete] == ["B", "A"]
    assert [i.bvid for i in out.uncertain] == ["C"]
    assert {i.bvid: i.reason for i in out.excluded} == {"D": "解說／速看", "E": "與搜尋無關"}

async def test_deleted_candidate_is_skipped_not_fatal():
    # view("A") returns None
    out = await search_series(fake, "摩登家庭", noop)
    assert [i.bvid for i in out.complete] == ["B"]

async def test_same_season_appears_once():
    # hits E1, E2 both belong to season 9 whose title contains the query
    out = await search_series(fake, "绝命毒师", noop)
    assert [i.key for i in out.complete] == ["season:9"] and fake.view_calls == ["E1"]

async def test_first_search_failure_propagates_later_is_tolerated():
    ...  # fake raises BiliError on call 1 -> raises; on call 3 only -> returns results

async def test_latest_filters_age_and_sorts_newest_first():
    # now=1_000_000_000; hits: X pubdate now-1d, Y now-10d, Z now-40d (all valid 美剧 full uploads), W anime
    out = await latest(fake, noop, now=1_000_000_000)
    assert [i.bvid for i in out.complete] == ["X", "Y"]
    assert all(c[1] == "pubdate" and c[2] == 4 for c in fake.search_calls)

async def test_progress_is_reported():
    msgs = []; await search_series(fake, "摩登家庭", lambda m: collect(msgs, m))
    assert msgs[0] == "正在查 B 站（1/5）…"
```

- [ ] **Step 2:** Run `uv run pytest tests/test_cache.py tests/test_pipeline.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `app/cache.py` and `app/pipeline.py`.
- [ ] **Step 4:** Run the tests. Expected: all pass.
- [ ] **Step 5:** Commit: `feat: add search and latest pipelines`.

---

### Task 5: Web server and launcher

**Files:**
- Create: `app/server.py`, `run.py`, `start.bat`, `tests/test_server.py`, `app/static/index.html` (placeholder containing only `<title>B站完整美劇</title>`; Task 6 replaces it)

**Interfaces:**
- Consumes: `search_series`, `latest`, `Outcome`, `BiliClient`, `BiliError`, `TTLCache`.
- Produces (HTTP):
  - `GET /` → `app/static/index.html`
  - `GET /api/search?q=<text>` and `GET /api/latest?refresh=0|1` → `application/x-ndjson` stream, one JSON object per line:
    - `{"type":"progress","message":str}`
    - `{"type":"result","complete":[item],"uncertain":[item],"excluded":[item],"fetched_at":<epoch seconds>,"cached":bool}` — `item` is `dataclasses.asdict(ResultItem)`
    - `{"type":"error","message":str}`
  - `create_app(client=None) -> FastAPI` for tests.

**Behavior:**
- One shared `BiliClient` created at startup, closed at shutdown.
- Search cache: `TTLCache(900)` keyed by the simplified, stripped query. Latest cache: `TTLCache(1800)`, bypassed when `refresh=1`. Cache hits emit a single `result` line with `"cached": true`.
- Empty/whitespace `q` → single line `{"type":"error","message":"請輸入劇名"}`; no client call.
- `BiliError` → `error` line with its message. Any other exception → `error` line `發生未預期的錯誤`, logged with traceback.
- A global `asyncio.Lock` serializes pipeline runs (one user; protects the throttle budget).
- Responses carry `Cache-Control: no-store`.
- `run.py`: find the LAN IP (UDP socket `connect(("10.255.255.255", 1))` then `getsockname()`, fallback `127.0.0.1`), print `在 iPad 的 Safari 輸入： http://{ip}:8756`, then `uvicorn.run("app.server:app", host="0.0.0.0", port=8756)`.
- `start.bat`: `cd /d %~dp0`, `uv run python run.py`, `pause`.

- [ ] **Step 1: Write failing tests** (`fastapi.testclient.TestClient(create_app(client=fake))`, helper `lines(resp)` → list of parsed JSON):

```python
def test_empty_query_is_error_without_calling_bilibili():
    out = lines(tc.get("/api/search", params={"q": "   "}))
    assert out == [{"type": "error", "message": "請輸入劇名"}] and fake.search_calls == []

def test_search_streams_progress_then_result():
    out = lines(tc.get("/api/search", params={"q": "摩登家庭"}))
    assert out[0]["type"] == "progress" and out[-1]["type"] == "result"
    assert out[-1]["cached"] is False and out[-1]["complete"][0]["url"].startswith("https://www.bilibili.com/video/")

def test_second_identical_search_is_cached_including_traditional_spelling():
    tc.get("/api/search", params={"q": "绝命毒师"}); n = len(fake.search_calls)
    out = lines(tc.get("/api/search", params={"q": "絕命毒師"}))
    assert out[-1]["cached"] is True and len(fake.search_calls) == n

def test_bili_error_becomes_error_event_not_empty_result():
    out = lines(tc.get("/api/search", params={"q": "x"}))      # fake raises BiliError("B 站暫時限制查詢，請稍後再試")
    assert out[-1] == {"type": "error", "message": "B 站暫時限制查詢，請稍後再試"}

def test_latest_refresh_bypasses_cache():
    tc.get("/api/latest"); n = len(fake.search_calls)
    tc.get("/api/latest"); assert len(fake.search_calls) == n
    tc.get("/api/latest", params={"refresh": 1}); assert len(fake.search_calls) > n

def test_index_served():
    assert "B站完整美劇" in tc.get("/").text
```

- [ ] **Step 2:** Run `uv run pytest tests/test_server.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `app/server.py`, `run.py`, `start.bat`, placeholder `index.html`.
- [ ] **Step 4:** Run `uv run pytest -v`. Expected: whole suite passes.
- [ ] **Step 5:** Commit: `feat: add NDJSON web server and launcher`.

---

### Task 6: Frontend

**Files:**
- Modify: `app/static/index.html` (replace placeholder; inline `<style>` and `<script>`)

**Interfaces — Consumes:** the HTTP contract from Task 5.

**Requirements (layout approved by the user as "A · 清單式"; reference mockup: `.superpowers/brainstorm/5195-1794394599/content/results-layout.html`):**
- `<meta name="viewport" content="width=device-width, initial-scale=1">`, `<meta name="referrer" content="no-referrer">` (Bilibili cover images refuse foreign referrers), `<meta name="apple-mobile-web-app-capable" content="yes">`, `<title>B站完整美劇</title>`.
- Two tabs: `搜尋` | `最新上線`. Selected tab remembered in `location.hash`.
- Search tab: text input (placeholder `輸入劇名，例如：摩登家庭`, submits on Enter) + `搜尋` button; hint line under it: `請用大陸譯名或英文原名（例如「老友记」而不是「六人行」）；繁體會自動轉簡體。`
- Summary line after a result: `找到 {n} 個完整版 · 按播放量排序 · 已隱藏 {m} 個剪輯／解說／網盤引流` (`最新上線`: `… · 按上傳時間排序 · 更新於 {HH:mm}` plus a `重新整理` button that calls `?refresh=1`).
- Each row is one `<a href=item.url target="_blank" rel="noopener">`: cover left (16:10, total duration `H:MM:SS`-style badge from `total_minutes`), full untruncated title, chip row — coverage (green, omitted when empty), `form_label`, quality, subtitle (omitted when `未知`), upload date `YYYY-MM-DD` — and play count on the right formatted `181.7 萬` (≥ 10,000) or the plain number, with `播放` beneath.
- Below: collapsed `<details>` `可能不完整（{n}）` — rows at 75% opacity with the `reason` as an amber chip. Then collapsed `<details>` `已隱藏（{m}）` — compact rows: title + reason chip, still links. Sections with 0 items are not rendered.
- While streaming: show the latest `progress.message` with a spinner; disable the search button. `error` event → red banner with the message (never the "0 results" text). Zero complete + zero uncertain → `找不到完整版資源。可以試試大陸譯名或英文原名。`
- Read the stream with `fetch` + `response.body.getReader()` + `TextDecoder`, splitting on `\n`. A new search aborts the previous one via `AbortController`.
- Every string from the API is inserted with `textContent` / `setAttribute`; no `innerHTML` with API data.
- Tap targets ≥ 44 px high; works in portrait and landscape; follows `prefers-color-scheme`; accent `#fb7299`.

- [ ] **Step 1:** Implement `app/static/index.html` per the requirements.
- [ ] **Step 2:** Run `uv run pytest tests/test_server.py::test_index_served -v`. Expected: PASS.
- [ ] **Step 3: Verify in a real browser** against a stub. Start the server with a fake client (a small `tests/manual_stub.py` that calls `create_app(client=FakeClient)` with ~6 canned items including one whose title is `<img src=x onerror=alert(1)> 全集` and one long title), open at an iPad-sized viewport (1024×768 and 768×1024) and check: rows match the mockup; the HTML title shows as literal text and no alert fires; `可能不完整` and `已隱藏` expand; the error banner appears when the stub raises; no horizontal scroll.
- [ ] **Step 4:** Commit: `feat: add list-style frontend`.

---

### Task 7: Live verification and README

**Files:**
- Create: `README.md`

- [ ] **Step 1:** Run `uv run python run.py`. Expected: prints the LAN URL and serves on 8756.
- [ ] **Step 2: Live search check.** In a browser open `http://localhost:8756`, search `摩登家庭`. Expected: progress messages appear; result arrives in under ~40 s; the top complete rows are real multi-hour uploads (spot-check two by opening them on Bilibili); no recap/clip/netdisk item sits in the complete list. Repeat with `絕命毒師` (Traditional) and one show of the implementer's choice. Record anything misclassified.
- [ ] **Step 3: Tune.** For each misclassification found, add a failing case to `tests/test_classifier.py` using the real title/part durations, fix the rule, re-run `uv run pytest -v`. Expected: all pass.
- [ ] **Step 4: Live latest check.** Open `最新上線`. Expected: rows newest-first, all within 30 days; `重新整理` re-queries; a second visit within 30 min returns instantly.
- [ ] **Step 5:** Write `README.md` in Traditional Chinese: what it does; how to start (double-click `start.bat`); allowing the Windows Firewall prompt on first run (private networks); opening the printed URL on the iPad and "加入主畫面"; the limits from spec §9 (waits of several seconds, takedowns, mainland titles, 1080P needs login on Bilibili itself).
- [ ] **Step 6:** Commit: `docs: add README; tune classifier from live results`.
