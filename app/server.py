"""Local web server: streams search progress and results as NDJSON, serves the catalog."""
import asyncio
import dataclasses
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.aliases import AliasResolver, _dedupe
from app.backfill import Backfiller
from app.bilibili import BiliClient, BiliError
from app.cache import TTLCache
from app.config import load_config
from app.pipeline import Outcome, bilibili_search_url, latest, search_series
from app.textnorm import normalize, to_simplified
from app.tmdb import KINDS, PLATFORMS, TmdbClient, TmdbError

log = logging.getLogger("bili-finder")
STATIC = Path(__file__).parent / "static"
SEARCH_TTL_S = 15 * 60
LATEST_TTL_S = 30 * 60
PARTIAL_TTL_S = 3 * 60  # cut-short "latest": keep briefly so a retry doesn't hammer Bilibili
_NO_STORE = {"Cache-Control": "no-store"}
_NDJSON = "application/x-ndjson"


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _result(outcome: Outcome, cached: bool, pending: int = 0) -> dict:
    body = dataclasses.asdict(outcome)
    body["skipped"] = [{"keyword": k, "duration": d, "url": bilibili_search_url(k, d)}
                       for k, d in outcome.skipped]
    return {"type": "result", "cached": cached, "backfill_pending": pending, **body}


def _one(event: dict) -> StreamingResponse:
    return StreamingResponse(iter([_line(event)]), media_type=_NDJSON, headers=_NO_STORE)


def create_app(client=None, resolver=None, tmdb=None, backfill_interval: float | None = 180.0) -> FastAPI:
    """backfill_interval: seconds between retries of refused searches; None disables the
    background loop (tests drive `app.state.backfill_tick` themselves)."""
    state = {"client": client, "resolver": resolver, "tmdb": tmdb}
    search_cache, latest_cache = TTLCache(SEARCH_TTL_S), TTLCache(LATEST_TTL_S)
    partial_cache = TTLCache(PARTIAL_TTL_S)
    backfiller = Backfiller(base_interval=backfill_interval or 180.0)
    filling: dict[str, Outcome] = {}  # key -> outcome whose refused searches are being retried
    busy = asyncio.Lock()  # one pipeline at a time: protects Bilibili's rate budget

    async def backfill_tick() -> None:
        """One polite retry of a refused search, unless a foreground search is running."""
        if busy.locked():
            return
        async with busy:
            done = await backfiller.tick(state["client"])
        if done:
            outcome = filling.pop(done, None)
            if outcome is not None and not outcome.partial:
                search_cache.set(done, outcome)

    async def backfill_loop(pause: float) -> None:
        while True:
            await asyncio.sleep(pause)
            try:
                await backfill_tick()
            except Exception:
                log.exception("backfill tick failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = []
        if state["client"] is None:
            state["client"] = BiliClient()
            owned.append(state["client"])
        if state["resolver"] is None:
            state["resolver"] = AliasResolver()
            owned.append(state["resolver"])
        if state["tmdb"] is None:
            state["tmdb"] = TmdbClient(load_config().tmdb_key)
            owned.append(state["tmdb"])
        loop_task = None
        if backfill_interval is not None:
            # Tick more often than the interval; the backfiller's own per-query
            # timers decide whether a tick actually sends anything.
            loop_task = asyncio.create_task(backfill_loop(backfill_interval / 6))
        yield
        if loop_task:
            loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await loop_task
        for resource in owned:
            await resource.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.backfill_tick = backfill_tick

    def stream(job) -> StreamingResponse:
        """job(progress) -> final event dict. Progress and the final event are streamed."""
        async def events():
            queue: asyncio.Queue = asyncio.Queue()

            async def progress(message: str) -> None:
                await queue.put({"type": "progress", "message": message})

            async def run() -> None:
                try:
                    async with busy:
                        final = await job(progress)
                except BiliError as exc:
                    final = {"type": "error", "message": str(exc)}
                except Exception:
                    log.exception("pipeline failed")
                    final = {"type": "error", "message": "發生未預期的錯誤"}
                await queue.put(final)

            task = asyncio.create_task(run())
            try:
                while True:
                    event = await queue.get()
                    yield _line(event)
                    if event["type"] != "progress":
                        break
            finally:
                task.cancel()

        return StreamingResponse(events(), media_type=_NDJSON, headers=_NO_STORE)

    @app.get("/api/search")
    async def api_search(q: str = "", kind: str = "auto", alt: str = "", tmdb_id: int | None = None):
        """kind: "tv" | "movie", or "auto" to let the title lookup decide."""
        query = " ".join(q.split())
        if not query:  # answer without queueing behind a running search
            return _one({"type": "error", "message": "請輸入劇名"})
        if kind not in (*KINDS, "auto"):
            return _one({"type": "error", "message": "參數錯誤"})
        requested = kind

        async def job(progress):
            detected = "tv"
            if alt.strip():
                # Launched from the catalog: both names are already known.
                names = _dedupe([to_simplified(query), alt.strip()])
            else:
                await progress("正在查詢劇名對應…")
                names, detected = await state["resolver"].resolve_full(query)
            kind = detected if requested == "auto" else requested
            key = kind + ":" + "|".join(normalize(n) for n in names)

            cached = search_cache.get(key)
            if cached is not None:
                return _result(cached, cached=True)
            if backfiller.get(key) is not None:
                return _result(backfiller.get(key), cached=True, pending=backfiller.pending(key))
            filling.pop(key, None)

            runtime = None
            tmdb = state["tmdb"]
            if kind == "movie" and tmdb_id is not None and tmdb.configured:
                with suppress(TmdbError):  # runtime only sharpens the verdicts
                    runtime = await tmdb.runtime(tmdb_id)

            if kind == "movie" and runtime is None:
                runtime = await tmdb.find_movie_runtime(names)

            outcome = await search_series(state["client"], names, progress, kind=kind, runtime=runtime)
            if outcome.skipped:
                backfiller.add(key, outcome)
                filling[key] = outcome
            elif not outcome.partial:
                search_cache.set(key, outcome)
            return _result(outcome, cached=False, pending=backfiller.pending(key))

        return stream(job)

    @app.get("/api/latest")
    async def api_latest(refresh: int = 0):
        async def job(progress):
            cached = None if refresh else (latest_cache.get("latest") or partial_cache.get("latest"))
            if cached is not None:
                return _result(cached, cached=True)
            outcome = await latest(state["client"], progress)
            (partial_cache if outcome.partial else latest_cache).set("latest", outcome)
            return _result(outcome, cached=False)

        return stream(job)

    @app.get("/api/platforms")
    async def api_platforms():
        return [{"id": key, "label": spec.label} for key, spec in PLATFORMS.items()]

    @app.get("/api/catalog")
    async def api_catalog(platform: str = "", section: str = "", kind: str = "", page: int = 1):
        if (platform not in PLATFORMS or kind not in KINDS
                or section not in ("available", "upcoming") or not 1 <= page <= 500):
            return JSONResponse({"error": "參數錯誤"}, status_code=400, headers=_NO_STORE)
        tmdb = state["tmdb"]
        if not tmdb.configured:
            return JSONResponse({"configured": False, "items": [], "has_more": False}, headers=_NO_STORE)
        try:
            if section == "available":
                items, has_more = await tmdb.available(platform, kind, page)
            else:
                items, has_more = await tmdb.upcoming(platform, kind), False
        except TmdbError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502, headers=_NO_STORE)
        return JSONResponse({"configured": True, "items": [dataclasses.asdict(t) for t in items],
                             "has_more": has_more}, headers=_NO_STORE)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers=_NO_STORE)

    return app


app = create_app()
