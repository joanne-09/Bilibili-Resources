"""Anonymous Bilibili web client: WBI-signed search and video detail, throttled.

Uses the same unofficial endpoints the bilibili.com web page calls. No login.
"""
import asyncio
import hashlib
import html
import time
import urllib.parse

import httpx

from app.models import Part, SearchHit, SeasonEp, VideoDetail
from app.textnorm import parse_mmss, strip_tags

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_API = "https://api.bilibili.com"
_KEY_TTL_S = 3600
_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19,
    29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


class BiliError(Exception):
    """A failure talking to Bilibili. The message is shown to the user."""


class BiliThrottled(BiliError):
    """Bilibili's risk control refused the search (it answers with a `v_voucher`
    instead of results). We do not try to get around it: back off and say so."""


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_TABLE)[:32]


def sign(params: dict, key: str, wts: int) -> dict:
    signed = {k: "".join(c for c in str(v) if c not in "!'()*") if isinstance(v, str) else v
              for k, v in params.items()}
    signed["wts"] = wts
    query = urllib.parse.urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return signed


def signed_query(params: dict, key: str, wts: int) -> str:
    """The exact query string to put on the wire.

    Bilibili checks w_rid against the raw query, so the request must carry the
    very bytes that were hashed; letting the HTTP library re-encode the params
    (space as %20 instead of +) silently invalidates the signature.
    """
    signed = sign(params, key, wts)
    w_rid = signed.pop("w_rid")
    return urllib.parse.urlencode(sorted(signed.items())) + "&w_rid=" + w_rid


def _https(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _stem(url: str) -> str:
    return url.rsplit("/", 1)[-1].split(".")[0]


class BiliClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None,
                 search_interval: float = 3.0, view_interval: float = 0.5,
                 retry_waits: tuple[float, ...] = (4.0, 10.0)):
        self._http = httpx.AsyncClient(
            transport=transport, timeout=15, follow_redirects=True,
            headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
        self._intervals = {"search": search_interval, "view": view_interval}
        self._last = {"search": 0.0, "view": 0.0}
        self._locks = {"search": asyncio.Lock(), "view": asyncio.Lock()}
        self._retry_waits = retry_waits
        self._key: str | None = None
        self._key_at = 0.0
        self._init_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise BiliError("無法連線到 B 站") from exc

    async def _get_json(self, url: str, **kwargs) -> dict:
        resp = await self._get(url, **kwargs)
        try:
            body = resp.json()
        except ValueError as exc:
            raise BiliError(f"B 站查詢失敗（HTTP {resp.status_code}）") from exc
        if not isinstance(body, dict):
            raise BiliError("B 站查詢失敗（回應格式異常）")
        return body

    async def _ensure_key(self) -> str:
        async with self._init_lock:
            if self._key and time.monotonic() - self._key_at < _KEY_TTL_S:
                return self._key
            await self._get("https://www.bilibili.com/")
            spi = (await self._get_json(f"{_API}/x/frontend/finger/spi")).get("data") or {}
            for name, field in (("buvid3", "b_3"), ("buvid4", "b_4")):
                if spi.get(field):
                    self._http.cookies.set(name, spi[field], domain=".bilibili.com")
            nav = (await self._get_json(f"{_API}/x/web-interface/nav")).get("data") or {}
            wbi = nav.get("wbi_img") or {}
            if not wbi.get("img_url") or not wbi.get("sub_url"):
                raise BiliError("B 站查詢失敗（無法取得簽名金鑰）")
            self._key = mixin_key(_stem(wbi["img_url"]), _stem(wbi["sub_url"]))
            self._key_at = time.monotonic()
            return self._key

    async def _paced(self, kind: str, url: str, params: dict | None = None) -> dict:
        """One request of `kind`, started no sooner than its interval after the last one."""
        async with self._locks[kind]:
            wait = self._last[kind] + self._intervals[kind] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._get_json(url, params=params)
            finally:
                self._last[kind] = time.monotonic()

    async def search(self, keyword: str, order: str = "click", duration: int = 0,
                     page: int = 1, patient: bool = True) -> list[SearchHit]:
        """duration: 0 any, 1 <10 min, 2 10-30, 3 30-60, 4 >60.

        patient=False gives up on the first throttled answer instead of waiting
        and retrying; use it for searches whose results are a bonus.
        """
        key = await self._ensure_key()
        base = {"search_type": "video", "keyword": keyword, "order": order,
                "duration": duration, "page": page}
        for wait in (None, *(self._retry_waits if patient else ())):
            if wait is not None:
                await asyncio.sleep(wait)
            query = signed_query(base, key, int(time.time()))
            body = await self._paced("search", f"{_API}/x/web-interface/wbi/search/type?{query}")
            if body.get("code") != 0:
                raise BiliError(f"B 站查詢失敗（代碼 {body.get('code')}）")
            data = body.get("data") or {}
            if "numResults" not in data:
                continue  # rate-limited: "success" with an empty shell
            return [self._hit(v) for v in data.get("result") or [] if v.get("bvid")]
        raise BiliThrottled("B 站暫時限制查詢，請過幾分鐘再試")

    @staticmethod
    def _hit(v: dict) -> SearchHit:
        return SearchHit(
            bvid=v["bvid"], title=html.unescape(strip_tags(v.get("title") or "")),
            duration_s=parse_mmss(v.get("duration") or "0"), play=int(v.get("play") or 0),
            pubdate=int(v.get("pubdate") or 0), typename=v.get("typename") or "",
            author=v.get("author") or "", cover=_https(v.get("pic") or ""),
            desc=v.get("description") or "", tag=v.get("tag") or "")

    async def view(self, bvid: str) -> VideoDetail | None:
        """Video detail, or None when the video is gone/unavailable."""
        await self._ensure_key()
        body = await self._paced("view", f"{_API}/x/web-interface/view", {"bvid": bvid})
        if body.get("code") != 0 or not body.get("data"):
            return None
        x = body["data"]
        season = x.get("ugc_season") or {}
        eps = [
            SeasonEp(bvid=e.get("bvid") or "", title=e.get("title") or "",
                     duration_s=int((e.get("arc") or {}).get("duration") or 0),
                     play=int(((e.get("arc") or {}).get("stat") or {}).get("view") or 0))
            for section in season.get("sections") or [] for e in section.get("episodes") or []]
        dim = x.get("dimension") or {}
        return VideoDetail(
            bvid=x.get("bvid") or bvid, title=x.get("title") or "", desc=x.get("desc") or "",
            play=int((x.get("stat") or {}).get("view") or 0), pubdate=int(x.get("pubdate") or 0),
            author=(x.get("owner") or {}).get("name") or "", cover=_https(x.get("pic") or ""),
            width=int(dim.get("width") or 0), height=int(dim.get("height") or 0),
            parts=[Part(p.get("part") or "", int(p.get("duration") or 0)) for p in x.get("pages") or []],
            season_id=season.get("id"), season_title=season.get("title"),
            season_eps=[e for e in eps if e.bvid])
