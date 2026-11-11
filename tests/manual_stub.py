"""Manual UI check without touching Bilibili:  uv run python -m tests.manual_stub

Serves the real frontend on http://localhost:8757 with canned data.
Search "error" to see the error banner, "none" for the empty state.
"""
import asyncio
import time

import uvicorn

from app.bilibili import BiliError
from app.models import Part, SearchHit, VideoDetail
from app.server import create_app

NOW = int(time.time())
ROWS = [
    ("B1", "【摩登家庭 全十一季】全250集 超清中字（未删减版）reaction", [375] * 22, 1817040),
    ("B2", "<img src=x onerror=alert(1)> 摩登家庭 全11季 超清双字 未删减", [374] * 20, 1060964),
    ("B3", "《摩登-家庭 第五季》全24集 超清双字 " + "很长的标题" * 12, [286, 285, 286, 285, 11], 446792),
    ("B4", "摩登家庭 第一季 中英双字", [22] * 24, 123000),
    ("B5", "【摩登家庭|第一季（1-9）】中英字幕", [172], 608739),
    ("B6", "一口气看完摩登家庭全集解说", [80], 900000),
    ("B7", "美剧 摩登家庭 1-11季超清中英字幕1080p全集 网盘分享 自取", [1], 3505),
]


class StubClient:
    async def search(self, keyword, order="click", duration=0, page=1):
        await asyncio.sleep(0.4)
        if keyword.startswith("error"):
            raise BiliError("B 站暫時限制查詢，請稍後再試")
        if keyword.startswith("none"):
            return []
        prefix = "美剧 " if order == "pubdate" else ""
        return [SearchHit(b, prefix + t, sum(p) * 60, play, NOW - i * 86400, cover="")
                for i, (b, t, p, play) in enumerate(ROWS)]

    async def view(self, bvid):
        await asyncio.sleep(0.1)
        for i, (b, t, p, play) in enumerate(ROWS):
            if b == bvid:
                return VideoDetail(b, t, play=play, pubdate=NOW - i * 86400, author="某UP主",
                                   width=1920, height=1080, parts=[Part("p", m * 60) for m in p])
        return None


class StubResolver:
    async def resolve(self, query):
        q = query.strip()
        return [q] if q in ("error", "none") else ["摩登家庭", "Modern Family"]

    async def aclose(self):
        pass


if __name__ == "__main__":
    uvicorn.run(create_app(client=StubClient(), resolver=StubResolver()), host="127.0.0.1", port=8757,
                log_level="warning")
