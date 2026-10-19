"""Render JSON Schema files for every evalkit model (see scripts/export_schemas.py)."""

import json
from pathlib import Path

from pydantic import BaseModel

from evalkit import schemas

MODELS: tuple[type[BaseModel], ...] = (
    schemas.Turn,
    schemas.TestCase,
    schemas.RetrievedChunk,
    schemas.ChunkUsed,
    schemas.KnowledgeDetails,
    schemas.ProductRecord,
    schemas.DatabaseDetails,
    schemas.AgentResponse,
    schemas.CaseResult,
    schemas.Label,
    schemas.SampleStratum,
    schemas.LabelSample,
    schemas.MetricResult,
    schemas.RunCounts,
    schemas.RunMeta,
)


def render_schemas() -> dict[str, str]:
    """Map ``<Model>.json`` to its JSON Schema text (2-space indent, trailing newline)."""
    return {
        f"{model.__name__}.json": json.dumps(
            model.model_json_schema(), indent=2, ensure_ascii=False
        )
        + "\n"
        for model in MODELS
    }


def write_schemas(out_dir: Path) -> list[Path]:
    """Write every schema to ``out_dir`` and delete stale ``*.json`` files there."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_schemas()
    for stale in out_dir.glob("*.json"):
        if stale.name not in rendered:
            stale.unlink()
    written = []
    for name, text in rendered.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
