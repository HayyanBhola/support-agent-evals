"""``voice_third_person`` (Day 15): the reply talks about the customer or itself in the
third person, as if reading internal documents.

Rubric v2 took voice out of tone (Day 12); this deterministic rule reports it instead.
Customer voice: "customers should...", "the customer", "a customer", "new users are
advised". Self voice: "the assistant", "the AI assistant", "the bot", "the chatbot".
Matching is case-insensitive on word boundaries. Phrases in the allowlist
(``rubrics/voice_allowlist.txt``, one per line, ``#`` comments) are removed before
matching: "I'm the GlowCare assistant" is first person, "customer support" is a team name.

``passed`` means the reply is free of third-person voice.
"""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from evalkit.schemas import AgentResponse, MetricResult, TestCase

VOICE_THIRD_PERSON: Final = "voice_third_person"
ALLOWLIST_FILE: Final = Path("rubrics") / "voice_allowlist.txt"
PATTERNS: Final = {
    "customer": re.compile(r"\b(?:the |a |our )?(?:customer|user)s?\b", re.IGNORECASE),
    "self": re.compile(
        r"\b(?:the|this) (?:ai |glowcare |virtual |chat )?(?:assistant|bot|chatbot)\b"
        r"|\bthe ai\b",
        re.IGNORECASE,
    ),
}


def load_allowlist(path: Path = ALLOWLIST_FILE) -> list[str]:
    if not path.exists():
        return []
    lines = (line.split("#", 1)[0].strip() for line in path.read_text("utf-8").splitlines())
    return [line.lower() for line in lines if line]


def third_person(reply: str, allowlist: Iterable[str] = ()) -> list[str]:
    """The third-person phrases in ``reply`` (lower-case), after removing allowlisted ones."""
    text = reply.lower()
    for phrase in sorted(allowlist, key=len, reverse=True):  # longest first
        text = text.replace(phrase, " ")
    found = []
    for pattern in PATTERNS.values():
        found += [m.group(0).strip() for m in pattern.finditer(text)]
    return found


def voice_third_person(
    case: TestCase, response: AgentResponse | None, allowlist: Iterable[str] = ()
) -> MetricResult | None:
    if response is None or not response.ok:
        return None
    found = third_person(response.reply, allowlist)
    return MetricResult(
        case_id=case.id,
        metric=VOICE_THIRD_PERSON,
        passed=not found,
        details={"phrases": found},
    )
