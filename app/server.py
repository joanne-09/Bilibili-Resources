"""Local web server: streams search progress and results as NDJSON."""
import asyncio
import dataclasses
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from app.aliases import AliasResolver
from app.bilibili import BiliClient, BiliError
from app.cache import TTLCache
from app.pipeline import Outcome, latest, search_series
from app.textnorm import normalize

log = logging.getLogger("bili-finder")
STATIC = Path(__file__).parent / "static"
SEARCH_TTL_S = 15 * 60
LATEST_TTL_S = 30 * 60
_NO_STORE = {"Cache-Control": "no-store"}


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _result(outcome: Outcome, cached: bool) -> dict:
    return {"type": "result", "cached": cached, **dataclasses.asdict(outcome)}


def create_app(client=None, resolver=None) -> FastAPI:
    state = {"client": client, "resolver": resolver}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = []
        if state["client"] is None:
            state["client"] = BiliClient()
            owned.append(state["client"])
        if state["resolver"] is None:
            state["resolver"] = AliasResolver()
            owned.append(state["resolver"])
        yield
        for resource in owned:
            await resource.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    search_cache, latest_cache = TTLCache(SEARCH_TTL_S), TTLCache(LATEST_TTL_S)
    busy = asyncio.Lock()  # one pipeline at a time: protects Bilibili's rate budget

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

        return StreamingResponse(events(), media_type="application/x-ndjson", headers=_NO_STORE)

    @app.get("/api/search")
    async def api_search(q: str = ""):
        query = q.strip()

        async def job(progress):
            if not query:
                return {"type": "error", "message": "請輸入劇名"}
            await progress("正在查詢劇名對應…")
            names = await state["resolver"].resolve(query)
            key = "|".join(normalize(n) for n in names)
            cached = search_cache.get(key)
            if cached is not None:
                return _result(cached, cached=True)
            outcome = await search_series(state["client"], names, progress)
            search_cache.set(key, outcome)
            return _result(outcome, cached=False)

        if not query:  # answer without queueing behind a running search
            return StreamingResponse(iter([_line({"type": "error", "message": "請輸入劇名"})]),
                                     media_type="application/x-ndjson", headers=_NO_STORE)
        return stream(job)

    @app.get("/api/latest")
    async def api_latest(refresh: int = 0):
        async def job(progress):
            cached = None if refresh else latest_cache.get("latest")
            if cached is not None:
                return _result(cached, cached=True)
            outcome = await latest(state["client"], progress)
            latest_cache.set("latest", outcome)
            return _result(outcome, cached=False)

        return stream(job)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers=_NO_STORE)

    return app


app = create_app()
