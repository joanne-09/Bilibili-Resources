"""Find the other names of a series so English / Taiwan titles search as well as
the mainland-Chinese title Bilibili uploads actually use.

Only the series name is sent out (to Douban, then Wikidata as a fallback).
Never raises: when nothing is found the caller just searches the typed name.
"""
import re

import httpx

from app.cache import TTLCache
from app.textnorm import normalize, to_simplified

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_CJK = re.compile(r"[㐀-鿿]")
_SEASON_SUFFIX = re.compile(r"\s*第\s*[0-9一二两三四五六七八九十]+\s*季\s*$")


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _dedupe(names: list[str]) -> list[str]:
    seen, out = set(), []
    for name in names:
        key = normalize(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


class AliasResolver:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(transport=transport, timeout=6, follow_redirects=True)
        self._cache = TTLCache(86400)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def resolve(self, query: str) -> list[str]:
        """Names to search, best (mainland Chinese) first. Always includes the query."""
        q = " ".join(to_simplified(query).split())
        key = normalize(q)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)

        found, ok = await self._douban(q)
        if not found and not has_cjk(q):
            found, wiki_ok = await self._wikidata(q)
            ok = ok and wiki_ok
        names = _dedupe([*found, q])
        if ok:  # don't let a transient failure stick for a day
            self._cache.set(key, names)
        return names

    async def _json(self, url: str, params: dict, headers: dict):
        try:
            resp = await self._http.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def _douban(self, q: str) -> tuple[list[str], bool]:
        data = await self._json(
            "https://movie.douban.com/j/subject_suggest", {"q": q},
            {"User-Agent": _UA, "Referer": "https://movie.douban.com/"})
        if not isinstance(data, list):
            return [], False
        want = normalize(q)
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("episode"):
                continue  # no episode count => a film, not a series
            title = _SEASON_SUFFIX.sub("", str(entry.get("title") or "")).strip()
            english = str(entry.get("sub_title") or "").strip()
            if not title:
                continue
            # A Latin-script query must literally match; a Chinese query may be a
            # regional alias Douban knows (六人行 -> 老友记), so trust its top series.
            if not has_cjk(q) and want not in normalize(title) and want not in normalize(english):
                continue
            return [title, english], True
        return [], True

    async def _wikidata(self, q: str) -> tuple[list[str], bool]:
        data = await self._json(
            "https://www.wikidata.org/w/api.php",
            {"action": "wbsearchentities", "search": q, "language": "en", "uselang": "zh-cn",
             "format": "json", "limit": 3},
            {"User-Agent": "series-title-lookup/0.1"})
        if not isinstance(data, dict):
            return [], False
        for entry in data.get("search") or []:
            label = str(entry.get("label") or "")
            desc = str(entry.get("description") or "").lower()
            if not has_cjk(label) or "season" in desc or "集别" in desc:
                continue
            if "电视" in desc or "剧" in desc or "television series" in desc:
                return [to_simplified(label)], True
        return [], True
