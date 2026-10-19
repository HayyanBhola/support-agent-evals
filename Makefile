.PHONY: install lint test test-live schemas eval-smoke eval-full report label overlap agreement split score

install:
	uv sync
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

# One pytest run, then two coverage gates: 100% for the data contracts, 90% for the runtime.
COV_CONTRACTS = --include="*/evalkit/schemas.py,*/evalkit/io.py,*/evalkit/hashing.py"
COV_RUNTIME = --include="*/evalkit/adapters/*,*/evalkit/cache.py,*/evalkit/runner.py,*/evalkit/importers/*,*/evalkit/snapshot_router.py,*/evalkit/route_report.py,*/evalkit/generation/*,*/evalkit/review/*,*/evalkit/manifest.py,*/evalkit/metrics/*,*/evalkit/rubrics.py,*/evalkit/labeling/*,*/evalkit/calibration/*,*/evalkit/stats/*,*/evalkit/judges/*,*/evalkit/synthetic/*,*/evalkit/bias/*,*/evalkit/budget.py"

test:
	uv run pytest --cov --cov-report=
	uv run coverage report $(COV_CONTRACTS) --fail-under=100
	uv run coverage report $(COV_RUNTIME) --fail-under=90

test-live:
	uv run pytest -m live

schemas:
	uv run python scripts/export_schemas.py

eval-smoke:
	uv run evalkit run --dataset datasets/sample_5.jsonl


# Human labeling app (Day 9): make label LABELER=<name>
label:
	uv run --group labeling streamlit run app/labeler.py -- --labeler $(LABELER)

# Day 12: the overlap set (second labeler) and the blind self-relabel set
overlap:
	uv run evalkit label overlap
	uv run evalkit label relabel-set

# Day 12: make agreement B=<name> (hayyan vs B); for B=hayyan-r2 write reports/self_consistency.md
agreement:
	uv run evalkit agreement report --a hayyan --b $(B) $(if $(filter hayyan-r2,$(B)),--out reports/self_consistency.md,)

# Day 13: the dev/test split of the human labels, then the leakage check
split:
	uv run evalkit split make
	uv run evalkit split check

# Day 15: deterministic metrics on a saved run: make score RUN=<run_id> (default: the Day 7 run)
RUN ?= 20261013-191847-f23b
score:
	uv run evalkit score $(RUN)

# Day 21: deterministic metrics + the six frozen OpenAI judges on every case, then MLflow.
# Paid (OpenAI): judges on the 120-case sample, capped at 400 calls / $$0.20; cache reused.
eval-full:
	uv run evalkit score-full $(RUN) --judge-sample datasets/judge_sample_v1.json --max-calls 400 --max-usd 0.20 --daily-cap 5000

report:
	uv run evalkit report --run $(RUN)
