from app.cache import TTLCache


def test_ttl_cache_expires():
    t = [0.0]
    c = TTLCache(10, clock=lambda: t[0])
    c.set("k", 1)
    assert c.get("k") == 1
    t[0] = 11
    assert c.get("k") is None


def test_missing_key_is_none():
    assert TTLCache(10).get("nope") is None
