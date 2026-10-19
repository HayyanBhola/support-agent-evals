"""The frozen judge prompts (Day 17): ``prompts/judges/FROZEN.json``.

After the v1 -> v2 -> v3 rounds on dev, the prompts are frozen to stop tuning them to the
dev labels (overfitting). FROZEN.json records, per criterion, the final prompt version, its
file and prompt_sha256, the rubric_hash it was built from, the input fields it sends and any
cases the judge skips. A test fails if a frozen prompt file changes without FROZEN.json being
updated, and ``judge run`` refuses a non-frozen prompt version unless ``--allow-unfrozen``.
"""

import json
import re
from pathlib import Path
from typing import Any, Final

from evalkit.judges.client import JUDGE_MODEL, OPENAI_JUDGE_MODEL
from evalkit.judges.inputs import JUDGE_SKIPS, input_fields
from evalkit.judges.prompts import PROMPT_DIR, load_prompt, prompt_path, prompt_sha256
from evalkit.schemas import Criterion

FROZEN_FILE: Final = "FROZEN.json"
# The final version per judged criterion (escalation_appropriate is a deterministic metric).
FINAL_VERSIONS: Final[dict[Criterion, str]] = {
    Criterion.RELEVANCE: "v3",
    Criterion.COMPLETENESS: "v3",
    Criterion.SAFETY: "v3",
    Criterion.TONE: "v2",
    Criterion.FAITHFULNESS: "v2",
    Criterion.CORRECTNESS: "v2",
}
SKIP_NOTES: Final[dict[tuple[Criterion, str], str]] = {
    (Criterion.CORRECTNESS, "v2"): "no reference_answer and no gold_db_facts: n_a for the judge",
}
_HEADER_HASH = re.compile(r"rubric_hash ([0-9a-f]{64})")


def entry(criterion: Criterion, version: str, prompt_dir: Path = PROMPT_DIR) -> dict[str, Any]:
    text = load_prompt(criterion, prompt_dir, version)
    header = _HEADER_HASH.search(text)
    return {
        "version": version,
        "file": prompt_path(criterion, prompt_dir, version).name,
        "prompt_sha256": prompt_sha256(text),
        "rubric_hash": header.group(1) if header else None,
        "input_fields": input_fields(criterion, version),
        "judge_skips": SKIP_NOTES.get((criterion, version))
        if (criterion, version) in JUDGE_SKIPS
        else None,
    }


def build_frozen(prompt_dir: Path = PROMPT_DIR) -> dict[str, Any]:
    return {
        "note": (
            "Judge prompts frozen on Day 17 after the v1-v3 rounds on dev: no more tuning on "
            "dev (overfitting). Evaluated once on the locked test split on Day 19 "
            "(reports/judges/FINAL_test_v1.md): the test split is USED (test_split_used), so "
            "any prompt change needs a new test set."
        ),
        "primary_judge": OPENAI_JUDGE_MODEL,
        "primary_judge_note": (
            "OpenAI gpt-5-mini is the primary judge since Day 18. OpenAI also generated the "
            "synthetic test cases (Day 5); see reports/judges/bias_by_source_dev.md."
        ),
        "cross_family_check": {
            "model": JUDGE_MODEL,
            "scope": "relevance only: v1 (96 dev), v2 (78 dev, cached), v3 (9 dev)",
            "why_partial": "dropped for Groq free-tier rate limits (200k tokens/day), not quality",
        },
        "criteria": {c.value: entry(c, v, prompt_dir) for c, v in FINAL_VERSIONS.items()},
    }


def write_frozen(prompt_dir: Path = PROMPT_DIR) -> Path:
    """Rewrite FROZEN.json; a ``test_split_used`` record is kept (it is never cleared)."""
    used = used_record(prompt_dir)
    data = build_frozen(prompt_dir) | ({TEST_USED: used} if used else {})
    return _write(prompt_dir, data)


def _write(prompt_dir: Path, data: dict[str, Any]) -> Path:
    path = prompt_dir / FROZEN_FILE
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


TEST_USED: Final = "test_split_used"


def used_record(prompt_dir: Path = PROMPT_DIR) -> dict[str, Any] | None:
    """The record of the one final test evaluation, or None while test is unused."""
    data = read_frozen(prompt_dir)
    record = data.get(TEST_USED) if data else None
    return dict(record) if record else None


def mark_test_used(record: dict[str, Any], prompt_dir: Path = PROMPT_DIR) -> Path:
    """Record in FROZEN.json that the test split was used by the final evaluation."""
    data = read_frozen(prompt_dir)
    if data is None:
        raise ValueError(f"no {FROZEN_FILE} in {prompt_dir}")
    return _write(prompt_dir, data | {TEST_USED: record})


def frozen_problems(prompt_dir: Path = PROMPT_DIR) -> list[str]:
    """Why FROZEN.json doesn't match the prompt files (empty when it does)."""
    data = read_frozen(prompt_dir)
    if data is None:
        return [f"no {FROZEN_FILE} in {prompt_dir}: run `evalkit judge freeze`"]
    recorded = data.get("criteria", {})
    problems = []
    if set(recorded) != {c.value for c in FINAL_VERSIONS}:
        problems.append(f"{FROZEN_FILE} lists {sorted(recorded)}, not the six judged criteria")
    for crit, version in FINAL_VERSIONS.items():
        item = recorded.get(crit.value)
        if item is None:
            continue
        if item.get("version") != version:
            problems.append(f"{crit.value}: frozen at {item.get('version')}, code says {version}")
            continue
        try:
            current = entry(crit, version, prompt_dir)
        except OSError as e:
            problems.append(f"{crit.value}: {e}")
            continue
        if current != item:
            problems.append(f"{crit.value} {version}: the prompt file or its inputs changed")
    return problems


def read_frozen(prompt_dir: Path = PROMPT_DIR) -> dict[str, Any] | None:
    path = prompt_dir / FROZEN_FILE
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def frozen_version(criterion: Criterion, prompt_dir: Path = PROMPT_DIR) -> str | None:
    data = read_frozen(prompt_dir)
    if data is None:
        return None
    item = data["criteria"].get(criterion.value)
    return str(item["version"]) if item else None
