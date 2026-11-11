"""TMDB client: what is on each streaming platform (US) and what is coming.

"Available" comes from TMDB's watch-provider data (sourced from JustWatch).
"Upcoming" can only cover each platform's own originals: TMDB knows a show's
network and next air date, but not when a licensed title will join a catalog.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from app.cache import TTLCache

_BASE = "https://api.themoviedb.org/3"
_POSTER = "https://image.tmdb.org/t/p/w342"
_REGION = "US"
_LANGUAGE = "zh-CN"   # mainland titles: that is what Bilibili uploads are named
_CACHE_TTL_S = 6 * 3600
UPCOMING_DAYS = 90
_UPCOMING_TV_LOOKUPS = 20
KINDS = ("tv", "movie")


class TmdbError(Exception):
    """Catalog data could not be fetched. The message is shown to the user."""


class TmdbNotConfigured(TmdbError):
    pass


@dataclass
class Platform:
    label: str
    provider_id: int              # TMDB watch-provider id (re-checked against TMDB's list)
    network_ids: tuple[int, ...]  # TMDB TV network ids, for upcoming originals
    provider_names: tuple[str, ...]


PLATFORMS: dict[str, Platform] = {
    "netflix": Platform("Netflix", 8, (213,), ("Netflix",)),
    "hbo": Platform("HBO Max", 1899, (49, 3186), ("HBO Max", "Max")),
    "prime": Platform("Prime Video", 9, (1024,), ("Amazon Prime Video",)),
}


@dataclass
class Title:
    tmdb_id: int
    kind: str            # "tv" | "movie"
    title: str           # zh-CN title
    original_title: str
    year: str
    date: str            # YYYY-MM-DD: release / next air date ("" when unknown)
    poster: str
    season: int | None = None


def _title(raw: dict, kind: str, when: str | None = None, season: int | None = None) -> Title:
    released = raw.get("first_air_date" if kind == "tv" else "release_date") or ""
    poster = raw.get("poster_path")
    return Title(
        tmdb_id=int(raw.get("id") or 0), kind=kind,
        title=raw.get("name" if kind == "tv" else "title") or "",
        original_title=raw.get("original_name" if kind == "tv" else "original_title") or "",
        year=released[:4], date=released if when is None else when,
        poster=f"{_POSTER}{poster}" if poster else "", season=season)


class TmdbClient:
    def __init__(self, key: str | None, transport: httpx.AsyncBaseTransport | None = None,
                 today: Callable[[], date] = date.today):
        self._key = (key or "").strip() or None
        bearer = bool(self._key and self._key.startswith("eyJ"))
        self._params = {} if bearer or not self._key else {"api_key": self._key}
        headers = {"Authorization": f"Bearer {self._key}"} if bearer else {}
        self._http = httpx.AsyncClient(transport=transport, timeout=12, headers=headers)
        self._today = today
        self._cache = TTLCache(_CACHE_TTL_S)
        self._provider_ids: dict[str, int] | None = None

    @property
    def configured(self) -> bool:
        return self._key is not None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.configured:
            raise TmdbNotConfigured("尚未設定 TMDB 金鑰")
        params = params or {}
        cache_key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(_BASE + path, params={**params, **self._params})
        except httpx.HTTPError:
            raise TmdbError("無法取得片單資料（TMDB）") from None  # `from None`: keep the key out of tracebacks
        if resp.status_code == 401:
            raise TmdbError("TMDB 金鑰無效，請檢查 config.local.toml")
        try:
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            raise TmdbError("無法取得片單資料（TMDB）") from None
        if not isinstance(body, dict):
            raise TmdbError("無法取得片單資料（TMDB）")
        self._cache.set(cache_key, body)
        return body

    @staticmethod
    def _check(platform: str, kind: str) -> Platform:
        if platform not in PLATFORMS or kind not in KINDS:
            raise ValueError(f"unknown platform/kind: {platform}/{kind}")
        return PLATFORMS[platform]

    async def _provider_id(self, platform: str) -> int:
        """Provider ids occasionally change (HBO Max <-> Max); prefer TMDB's own list."""
        if self._provider_ids is None:
            found: dict[str, int] = {}
            try:
                body = await self._get("/watch/providers/tv", {"watch_region": _REGION})
                by_name = {p.get("provider_name"): p.get("provider_id") for p in body.get("results") or []}
                for key, spec in PLATFORMS.items():
                    for name in spec.provider_names:
                        if isinstance(by_name.get(name), int):
                            found[key] = by_name[name]
                            break
            except TmdbNotConfigured:
                raise
            except TmdbError:
                pass  # fall back to the built-in ids
            self._provider_ids = found
        return self._provider_ids.get(platform, PLATFORMS[platform].provider_id)

    def _window(self) -> tuple[str, str]:
        start = self._today()
        return start.isoformat(), (start + timedelta(days=UPCOMING_DAYS)).isoformat()

    async def available(self, platform: str, kind: str, page: int = 1) -> tuple[list[Title], bool]:
        """Most popular titles currently streaming on the platform. -> (items, has_more)"""
        self._check(platform, kind)
        if not self.configured:
            raise TmdbNotConfigured("尚未設定 TMDB 金鑰")
        body = await self._get(f"/discover/{kind}", {
            "with_watch_providers": await self._provider_id(platform), "watch_region": _REGION,
            "with_watch_monetization_types": "flatrate", "sort_by": "popularity.desc",
            "language": _LANGUAGE, "page": page})
        items = [_title(r, kind) for r in body.get("results") or [] if r.get("id")]
        return items, page < int(body.get("total_pages") or 0)

    async def upcoming(self, platform: str, kind: str) -> list[Title]:
        """Platform originals premiering in the next UPCOMING_DAYS days, soonest first."""
        spec = self._check(platform, kind)
        if not self.configured:
            raise TmdbNotConfigured("尚未設定 TMDB 金鑰")
        start, end = self._window()
        if kind == "movie":
            body = await self._get("/discover/movie", {
                "with_watch_providers": await self._provider_id(platform), "watch_region": _REGION,
                "primary_release_date.gte": start, "primary_release_date.lte": end,
                "sort_by": "primary_release_date.asc", "language": _LANGUAGE, "page": 1})
            items = [_title(r, "movie") for r in body.get("results") or [] if r.get("id")]
            return sorted((t for t in items if t.date), key=lambda t: t.date)

        body = await self._get("/discover/tv", {
            "with_networks": "|".join(map(str, spec.network_ids)),
            "air_date.gte": start, "air_date.lte": end,
            "sort_by": "popularity.desc", "language": _LANGUAGE, "page": 1})
        items = []
        for raw in (body.get("results") or [])[:_UPCOMING_TV_LOOKUPS]:
            if not raw.get("id"):
                continue
            try:
                detail = await self._get(f"/tv/{raw['id']}", {"language": _LANGUAGE})
            except TmdbNotConfigured:
                raise
            except TmdbError:
                continue
            nxt = detail.get("next_episode_to_air") or {}
            when = nxt.get("air_date") or ""
            if start <= when <= end:
                items.append(_title(raw, "tv", when=when, season=nxt.get("season_number")))
        return sorted(items, key=lambda t: t.date)

    async def find_movie_runtime(self, names: list[str]) -> int | None:
        """Runtime (minutes) of the film best matching any of the names; None if unknown.
        Never raises: the runtime only sharpens verdicts."""
        if not self.configured:
            return None
        try:
            for name in names:
                body = await self._get("/search/movie", {"query": name, "language": _LANGUAGE, "page": 1})
                results = body.get("results") or []
                if results and results[0].get("id"):
                    return await self.runtime(results[0]["id"])
        except TmdbError:
            pass
        return None

    async def runtime(self, movie_id: int) -> int | None:
        body = await self._get(f"/movie/{int(movie_id)}", {"language": _LANGUAGE})
        minutes = body.get("runtime")
        return int(minutes) if isinstance(minutes, int) and minutes > 0 else None
