"""Log a scored run to MLflow (Day 21): local, under ``./mlruns`` (gitignored).

MLflow 3.x refuses the plain file store (``./mlruns`` as a folder of files is in maintenance
mode), so the tracking store is SQLite at ``mlruns/mlflow.db`` and the artifacts sit in
``mlruns/artifacts``: everything stays in ``./mlruns``. View with
``uv run mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db``. Telemetry is disabled.

Params: dataset version and sha256, rubric hash, FROZEN.json sha256, judge and agent
models, evalkit's git sha. Metrics: every pass rate with its CI bounds and n, escalation
recall / precision / F1, every breakdown rate, latency and tokens. Artifacts: the result
files (scores.jsonl, summary.json, the judges' raw verdicts).
"""

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

TRACKING_DIR: Final = Path("mlruns")
_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9_\-. /]")  # MLflow metric names allow only these
EXPERIMENT: Final = "glowcare-support-agent"
PARAMS: Final = (
    "run_id",
    "dataset",
    "dataset_sha256",
    "rubric_hash",
    "frozen_sha256",
    "judge_model",
    "agent_model",
    "git_sha",
    "agent_run_git_sha",
)


def tracking_uri(tracking_dir: Path = TRACKING_DIR) -> str:
    return f"sqlite:///{tracking_dir.as_posix()}/mlflow.db"


def params_of(summary: Mapping[str, Any]) -> dict[str, str]:
    return {k: str(summary.get(k, "")) for k in PARAMS}


def metrics_of(summary: Mapping[str, Any]) -> dict[str, float]:
    """Every number of the summary as a flat MLflow metric (undefined values left out)."""
    out: dict[str, Any] = {}
    for m, r in summary.get("metrics", {}).items():
        out |= {f"{m}.rate": r["rate"], f"{m}.ci_low": r["ci_low"], f"{m}.ci_high": r["ci_high"],
                f"{m}.n": r["n"]}  # fmt: skip
    for name, v in summary.get("escalation", {}).items():
        out[f"escalation.{name}"] = v["value"] if isinstance(v, dict) else v
        if isinstance(v, dict):
            out |= {f"escalation.{name}.ci_low": v["ci_low"],
                    f"escalation.{name}.ci_high": v["ci_high"]}  # fmt: skip
    for by, groups in summary.get("breakdowns", {}).items():
        for g, per in groups.items():
            for m, r in per.items():
                out |= {f"{by}.{g}.{m}.rate": r["rate"], f"{by}.{g}.{m}.n": r["n"]}
    for section in ("latency_ms", "tokens", "judge_usage"):
        for k, v in (summary.get(section) or {}).items():
            out[f"{section}.{k}"] = v
    # e.g. the "(none)" route group of legacy cases -> "none"
    return {_NOT_ALLOWED.sub("", k): float(v) for k, v in out.items()
            if isinstance(v, int | float)}  # fmt: skip


# Without this tag the MLflow 3 UI guesses the experiment type and, with no traces, can open
# the GenAI (traces) view, where every count is zero; "custom_model_development" is the
# "Machine learning" type, whose Runs table and Model metrics charts show these metrics.
KIND_TAG: Final = "mlflow.experimentKind"
KIND: Final = "custom_model_development"


def ensure_experiment(mlflow: Any, experiment: str, tracking_dir: Path = TRACKING_DIR) -> str:
    """Create the experiment if needed, tag it as a machine-learning experiment, select it."""
    found = mlflow.get_experiment_by_name(experiment)
    if found is None:
        location = (tracking_dir / "artifacts").resolve().as_uri()
        experiment_id = str(mlflow.create_experiment(experiment, artifact_location=location))
    else:
        experiment_id = str(found.experiment_id)
    mlflow.set_experiment(experiment)
    mlflow.set_experiment_tag(KIND_TAG, KIND)
    return experiment_id


def log_run(
    summary: Mapping[str, Any],
    artifacts: Sequence[Path],
    *,
    tracking_dir: Path = TRACKING_DIR,
    experiment: str = EXPERIMENT,
) -> str:
    """Log one scored run; returns the MLflow run id."""
    return log_metrics(
        str(summary.get("run_id", "run")), params_of(summary), metrics_of(summary), artifacts,
        tracking_dir=tracking_dir, experiment=experiment,
    )  # fmt: skip


def log_metrics(
    run_name: str,
    params: Mapping[str, str],
    metrics: Mapping[str, float],
    artifacts: Sequence[Path] = (),
    *,
    tracking_dir: Path = TRACKING_DIR,
    experiment: str = EXPERIMENT,
) -> str:
    """Log one MLflow run (params, metrics, artifact files); returns its run id. Artifacts
    from a ``full`` folder (the judges' raw verdicts) go under ``judges/``."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    # MLflow 3.x sends usage telemetry from a background thread unless disabled: off, nothing
    # leaves the machine (the thread also slowed later in-process tests)
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    import mlflow  # a dev dependency, imported only when logging

    tracking_dir.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(tracking_uri(tracking_dir))
    ensure_experiment(mlflow, experiment, tracking_dir)
    clean = {_NOT_ALLOWED.sub("", k): float(v) for k, v in metrics.items()}
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(dict(params))
        mlflow.log_metrics(clean)
        for path in artifacts:
            mlflow.log_artifact(str(path), artifact_path="judges" if path.parent.name ==
                                "full" else None)  # fmt: skip
        return str(run.info.run_id)
