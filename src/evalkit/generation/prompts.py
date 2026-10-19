"""Versioned generation prompts: ``prompts/<name>_v<N>.md``, never inline strings.

Each file starts with ``<!-- prompt: <name>_v<N> -->`` so a prompt can be identified from the
message alone. A prompt's ``sha256`` (of its text with LF newlines, so a CRLF checkout
hashes the same) is part of every cache key: editing a prompt without bumping its version
still invalidates its cached outputs.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# The repo-root prompts/ directory (src/evalkit/generation/prompts.py -> repo root).
PROMPTS_DIR: Final = Path(__file__).resolve().parents[3] / "prompts"
_HEADER = re.compile(r"^<!-- prompt: ([a-z0-9_]+_v\d+) -->\s*$")


@dataclass(frozen=True)
class Prompt:
    name: str  # e.g. "guardrail_v1"
    text: str
    sha256: str


def load_prompt(name: str, version: int = 1, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    full = f"{name}_v{version}"
    path = prompts_dir / f"{full}.md"
    raw = path.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if prompt_name_of(text) != full:
        raise ValueError(f"{path} must start with '<!-- prompt: {full} -->'")
    return Prompt(full, text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def prompt_name_of(text: str) -> str:
    """The ``<name>_v<N>`` from a prompt's header line ('' if there is none)."""
    m = _HEADER.match(text.split("\n", 1)[0])
    return m.group(1) if m else ""
