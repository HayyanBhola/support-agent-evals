from pathlib import Path

import diskcache

from evalkit.cache import CacheMode, ResponseCache, cache_key
from evalkit.schemas import AgentResponse

from .agent_double import fixture, make_case

CASE = make_case("k1", "What is your shipping policy?")
OK = AgentResponse.model_validate(fixture("knowledge"))
ERROR = AgentResponse.model_validate(fixture("error"))


def test_key_is_stable_and_covers_what_changes_the_answer() -> None:
    base = cache_key("glowcare", CASE, "gemini/a")
    assert base == cache_key("glowcare", make_case("other-id", CASE.question), "gemini/a")
    assert base != cache_key("glowcare", CASE, "gemini/b")
    assert base != cache_key("other", CASE, "gemini/a")
    assert base != cache_key("glowcare", make_case("k1", "Different?"), "gemini/a")
    with_history = make_case("k1", CASE.question, history=[{"role": "user", "content": "x"}])
    assert base != cache_key("glowcare", with_history, "gemini/a")


def test_normal_mode_round_trips_ok_responses(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path, CacheMode.NORMAL)
    assert cache.get("k") is None and not cache.contains("k")
    assert cache.put("k", OK) is True
    hit = cache.get("k")
    assert hit is not None and hit.response == OK and hit.cached_at.tzinfo is not None
    assert cache.contains("k")
    cache.close()


def test_failures_are_never_cached(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path, CacheMode.NORMAL)
    assert cache.put("k", ERROR) is False
    assert cache.get("k") is None
    cache.close()


def test_off_mode_neither_reads_nor_writes(tmp_path: Path) -> None:
    store = tmp_path / "cache"
    cache = ResponseCache(store, CacheMode.OFF)
    assert cache.put("k", OK) is False and cache.get("k") is None
    cache.close()
    assert not store.exists()


def test_refresh_mode_skips_reads_but_overwrites(tmp_path: Path) -> None:
    normal = ResponseCache(tmp_path, CacheMode.NORMAL)
    normal.put("k", OK)
    normal.close()
    refresh = ResponseCache(tmp_path, CacheMode.REFRESH)
    assert refresh.get("k") is None
    newer = OK.model_copy(update={"reply": "fresh answer"})
    assert refresh.put("k", newer) is True
    refresh.close()
    normal = ResponseCache(tmp_path, CacheMode.NORMAL)
    hit = normal.get("k")
    assert hit is not None and hit.response.reply == "fresh answer"
    normal.close()


def test_unreadable_entry_is_dropped(tmp_path: Path) -> None:
    with diskcache.Cache(str(tmp_path)) as raw:
        raw.set("k", '{"not": "a cached response"}')
    cache = ResponseCache(tmp_path, CacheMode.NORMAL)
    assert cache.get("k") is None
    cache.close()
    with diskcache.Cache(str(tmp_path)) as raw:
        assert "k" not in raw
