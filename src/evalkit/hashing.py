"""Deterministic hashes for cache keys, config_hash and dataset fingerprints."""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel


def _to_json(obj: object) -> object:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    raise TypeError(f"stable_hash cannot hash {type(obj).__name__}; convert it to JSON first")


def canonical_json(obj: object) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8 text, NaN/Infinity rejected.

    Pydantic models (also nested inside dicts/lists) are dumped in JSON mode. Anything else
    that is not plain JSON (datetime, set, bytes, ...) raises TypeError instead of being
    silently stringified, so two different values can never share a hash by accident.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_to_json,
    )


def stable_hash(obj: object) -> str:
    """sha256 hex digest of ``canonical_json(obj)``."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """sha256 hex digest of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
