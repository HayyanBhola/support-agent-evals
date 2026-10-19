"""Runs sample_5 against the real agent. Skipped by default; run with: pytest -m live"""

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from evalkit.adapters.glowcare import GlowCareAdapter
from evalkit.cache import CacheMode
from evalkit.runner import RunConfig, run_eval

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent


async def test_sample_5_against_the_real_agent(tmp_path: Path) -> None:
    config = RunConfig(
        dataset=ROOT / "datasets" / "sample_5.jsonl",
        runs_dir=tmp_path / "runs",
        cache_mode=CacheMode.OFF,
    )
    async with GlowCareAdapter.from_env(ROOT / ".env") as adapter:
        outcome = await run_eval(config, adapter, Console(file=StringIO()))

    assert len(outcome.results) == 5
    assert all(r.failure_type is None for r in outcome.results), [
        (r.case_id, r.failure_type, r.error_message) for r in outcome.results
    ]
    assert all(r.response and r.response.eval_mode is True for r in outcome.results)
    routes = {r.case_id: r.response.route for r in outcome.results if r.response}
    for case_id, case in outcome.cases.items():
        if "known_bug" not in case.tags:
            assert routes[case_id] is case.expected_route, case_id
