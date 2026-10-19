import json
from pathlib import Path

from evalkit.schema_export import MODELS, render_schemas, write_schemas

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "docs" / "schemas"
FIX = "run `uv run python scripts/export_schemas.py` and commit docs/schemas/"


def test_committed_schemas_are_up_to_date() -> None:
    rendered = render_schemas()
    committed = {p.name for p in SCHEMA_DIR.glob("*.json")}
    assert committed == set(rendered), f"schema files differ from models; {FIX}"
    for name, text in rendered.items():
        on_disk = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
        assert on_disk == json.loads(text), f"{name} is out of date; {FIX}"


def test_every_persisted_model_is_versioned() -> None:
    for name in ["TestCase", "CaseResult", "Label", "MetricResult", "RunMeta"]:
        schema = json.loads(render_schemas()[f"{name}.json"])
        assert schema["properties"]["schema_version"]["const"] == 1


def test_write_schemas_removes_stale_files(tmp_path: Path) -> None:
    (tmp_path / "OldModel.json").write_text("{}", encoding="utf-8")
    written = write_schemas(tmp_path)
    assert len(written) == len(MODELS)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(render_schemas())
