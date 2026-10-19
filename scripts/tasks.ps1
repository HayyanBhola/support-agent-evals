# Windows task runner (mirrors the Makefile). Usage: .\scripts\tasks.ps1 <task>
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "lint", "test", "test-live", "schemas", "eval-smoke", "eval-full", "report", "label", "overlap", "agreement", "split", "score")]
    [string]$Task = "test",
    # label: the labeler name (required by the labeling app); agreement: the second labeler
    [string]$Labeler = "",
    # score, eval-full, report: the saved run (default: the Day 7 run)
    [string]$RunId = "20261013-191847-f23b"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Invoke-Step([string]$Cmd) {
    Write-Host "> $Cmd"
    Invoke-Expression $Cmd
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Task) {
    "install" {
        Invoke-Step "uv sync"
        Invoke-Step "uv run pre-commit install"
    }
    "lint" {
        Invoke-Step "uv run ruff check ."
        Invoke-Step "uv run ruff format --check ."
        Invoke-Step "uv run mypy"
    }
    # One pytest run, then two coverage gates: 100% for the data contracts, 90% for the runtime.
    "test" {
        Invoke-Step "uv run pytest --cov --cov-report="
        Invoke-Step "uv run coverage report --include='*/evalkit/schemas.py,*/evalkit/io.py,*/evalkit/hashing.py' --fail-under=100"
        Invoke-Step "uv run coverage report --include='*/evalkit/adapters/*,*/evalkit/cache.py,*/evalkit/runner.py,*/evalkit/importers/*,*/evalkit/snapshot_router.py,*/evalkit/route_report.py,*/evalkit/generation/*,*/evalkit/review/*,*/evalkit/manifest.py,*/evalkit/metrics/*,*/evalkit/rubrics.py,*/evalkit/labeling/*,*/evalkit/calibration/*,*/evalkit/stats/*,*/evalkit/judges/*,*/evalkit/synthetic/*,*/evalkit/bias/*,*/evalkit/budget.py' --fail-under=90"
    }
    "test-live" { Invoke-Step "uv run pytest -m live" }
    "schemas" { Invoke-Step "uv run python scripts/export_schemas.py" }
    "eval-smoke" { Invoke-Step "uv run evalkit run --dataset datasets/sample_5.jsonl" }
    # Day 21: deterministic metrics + the six frozen OpenAI judges on every case, then MLflow.
    # Paid (OpenAI): judges on the 120-case sample, capped at 400 calls / $0.20; cache reused.
    "eval-full" { Invoke-Step "uv run evalkit score-full $RunId --judge-sample datasets/judge_sample_v1.json --max-calls 400 --max-usd 0.20 --daily-cap 5000" }
    "report" { Invoke-Step "uv run evalkit report --run $RunId" }
    "overlap" {
        Invoke-Step "uv run evalkit label overlap"
        Invoke-Step "uv run evalkit label relabel-set"
    }
    # hayyan vs -Labeler; hayyan-r2 (the blind self-relabel) goes to reports/self_consistency.md
    "agreement" {
        if (-not $Labeler) { Write-Host "usage: scripts/tasks.ps1 agreement -Labeler <name>"; exit 2 }
        $out = if ($Labeler -eq "hayyan-r2") { " --out reports/self_consistency.md" } else { "" }
        Invoke-Step "uv run evalkit agreement report --a hayyan --b $Labeler$out"
    }
    # Day 13: the dev/test split of the human labels, then the leakage check
    "split" {
        Invoke-Step "uv run evalkit split make"
        Invoke-Step "uv run evalkit split check"
    }
    # Day 15: every deterministic metric on a saved run -> metrics.jsonl and scorecard.md
    "score" { Invoke-Step "uv run evalkit score $RunId" }
    "label" {
        if (-not $Labeler) { Write-Host "usage: scripts/tasks.ps1 label -Labeler <name>"; exit 2 }
        Invoke-Step "uv run --group labeling streamlit run app/labeler.py -- --labeler $Labeler"
    }
}
