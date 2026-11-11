"""Text helpers: Traditional→Simplified, title normalization, numeral parsing."""
import re

from opencc import OpenCC

_t2s = OpenCC("t2s")
_TAG = re.compile(r"<[^>]+>")
_KEEP = re.compile(r"[^0-9a-z㐀-鿿]+")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def to_simplified(text: str) -> str:
    return _t2s.convert(text)


def strip_tags(text: str) -> str:
    return _TAG.sub("", text)


def normalize(text: str) -> str:
    """Simplified, lowercased, markup removed, only CJK/letters/digits kept."""
    return _KEEP.sub("", to_simplified(strip_tags(text)).lower())


def cn_to_int(text: str) -> int | None:
    """'十一' -> 11, '二十四' -> 24, '11' -> 11; None when not a number (1-99)."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if not text or any(c not in _CN_DIGITS and c != "十" for c in text):
        return None
    if "十" not in text:
        return _CN_DIGITS[text] if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


def parse_mmss(text: str) -> int:
    """Bilibili search duration 'MM:SS' (or 'H:MM:SS') -> seconds."""
    total = 0
    for piece in str(text).split(":"):
        total = total * 60 + (int(piece) if piece.strip().isdigit() else 0)
    return total
