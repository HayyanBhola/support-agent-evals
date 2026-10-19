"""On-disk cache of successful agent responses (diskcache in .cache/agent/).

Only ``ok: true`` responses are cached; failures are always retried on the next run. The
key covers everything that changes the answer: adapter, question, history, eval_mode and
the agent's LLM as reported by ``/health``.
"""

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import diskcache
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from evalkit.hashing import stable_hash
from evalkit.schemas import AgentResponse, TestCase

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(".cache") / "agent"


class CacheMode(StrEnum):
    NORMAL = "normal"  # read and write
    OFF = "off"  # --no-cache: neither read nor write
    REFRESH = "refresh"  # --refresh: don't read, overwrite


class CachedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cached_at: AwareDatetime
    response: AgentResponse


def cache_key(adapter_name: str, case: TestCase, agent_llm: str | None) -> str:
    return stable_hash(
        {
            "adapter": adapter_name,
            "question": case.question,
            "history": [turn.model_dump(mode="json") for turn in case.history],
            "eval_mode": True,
            "agent_llm": agent_llm,
        }
    )


class ResponseCache:
    def __init__(self, directory: Path = DEFAULT_CACHE_DIR, mode: CacheMode = CacheMode.NORMAL):
        self.mode = mode
        self.directory = directory
        self._store: Any = None if mode is CacheMode.OFF else diskcache.Cache(str(directory))

    def get(self, key: str) -> CachedResponse | None:
        """The cached response for ``key``, or None (always None unless mode is NORMAL)."""
        if self.mode is not CacheMode.NORMAL:
            return None
        raw = self._store.get(key)
        if raw is None:
            return None
        try:
            return CachedResponse.model_validate_json(raw)
        except ValidationError:
            log.warning("dropping unreadable cache entry %s", key[:12])
            self._store.delete(key)
            return None

    def contains(self, key: str) -> bool:
        """Whether ``get(key)`` would be a hit."""
        return self.get(key) is not None

    def put(self, key: str, response: AgentResponse) -> bool:
        """Store ``response`` if caching is on and the response is ok. Returns True if stored."""
        if self._store is None or not response.ok:
            return False
        entry = CachedResponse(cached_at=datetime.now(UTC), response=response)
        self._store.set(key, entry.model_dump_json())
        return True

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
