"""Write JSON Schema for every evalkit model to docs/schemas/*.json.

Usage: uv run python scripts/export_schemas.py
tests/test_schema_export.py fails if the committed files are out of date.
"""

from pathlib import Path

from evalkit.schema_export import write_schemas

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "schemas"

if __name__ == "__main__":
    for path in write_schemas(OUT_DIR):
        print(path.relative_to(OUT_DIR.parent.parent))
