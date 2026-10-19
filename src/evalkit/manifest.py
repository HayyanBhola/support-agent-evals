"""Lightweight dataset versioning: ``datasets/MANIFEST.json`` (no DVC; the data is small
text in git).

For every dataset file the manifest records its sha256, its number of records, a version and
the files it was built from. A run of a listed file gets ``RunMeta.dataset_version`` =
``<name>@<version>`` (e.g. ``golden_v1@1.0``), but only if the file's sha256 still matches
the manifest; a mismatch means the file changed without a new version, so no version is
recorded and a warning is logged.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from evalkit.hashing import file_sha256
from evalkit.schemas import Sha256Hex

MANIFEST_NAME: Final = "MANIFEST.json"
log = logging.getLogger(__name__)


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: Sha256Hex
    records: int
    kind: Literal[
        "testcases",
        "chunks",
        "label_samples",
        "labels",
        "splits",
        "synthetic_negatives",
        "bias_perturbations",
        "routing_cases",
    ] = "testcases"
    version: str
    sources: list[str] = []
    description: str = ""
    metadata: dict[str, Any] = {}  # e.g. run_id, seed, rubric_hash of a generated file


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    datasets: dict[str, ManifestEntry]


class DatasetSpec(BaseModel):
    """What a person decides about a file: version, provenance, description."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    sources: list[str] = []
    kind: Literal[
        "testcases",
        "chunks",
        "label_samples",
        "labels",
        "splits",
        "synthetic_negatives",
        "bias_perturbations",
        "routing_cases",
    ] = "testcases"
    description: str = ""
    metadata: dict[str, Any] = {}


def count_records(path: Path) -> int:
    """Non-empty lines of a JSONL file; for a split file (``.json`` with ``splits``), the
    number of case_ids across its splits."""
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return sum(len(ids) for ids in data["splits"].values())
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def build_manifest(
    datasets_dir: Path,
    specs: Mapping[str, DatasetSpec],
    keep: Manifest | None = None,
    sources_on_disk_ok: bool = False,
) -> Manifest:
    """Hash and count every file in ``specs``; sources must be listed too. Entries of
    ``keep`` that ``specs`` doesn't list (written by commands, e.g. a labeling sample) are
    kept with their spec fields and re-hashed."""
    if keep is not None:
        kept = {
            name: DatasetSpec(
                **e.model_dump(include={"version", "sources", "kind", "description", "metadata"})
            )
            for name, e in keep.datasets.items()
            if name not in specs
        }
        specs = {**kept, **specs}
    missing = sorted(
        {
            s
            for spec in specs.values()
            for s in spec.sources
            if s not in specs and not (sources_on_disk_ok and (datasets_dir / s).exists())
        }
        | {name for name in specs if not (datasets_dir / name).exists()}
    )
    if missing:
        raise ValueError(f"not listed or not on disk: {', '.join(missing)}")
    return Manifest(
        datasets={
            name: ManifestEntry(
                sha256=file_sha256(datasets_dir / name),
                records=count_records(datasets_dir / name),
                **spec.model_dump(),
            )
            for name, spec in sorted(specs.items())
        }
    )


def write_manifest(manifest: Manifest, path: Path) -> None:
    text = json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def read_manifest(path: Path) -> Manifest:
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def stale_entries(manifest: Manifest, datasets_dir: Path) -> list[str]:
    """Files whose sha256 or record count no longer match the manifest."""
    problems = []
    for name, entry in manifest.datasets.items():
        path = datasets_dir / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif file_sha256(path) != entry.sha256:
            problems.append(f"{name}: sha256 changed")
        elif count_records(path) != entry.records:
            problems.append(f"{name}: record count changed")
    return problems


def dataset_version(dataset: Path, manifest_path: Path | None = None) -> str | None:
    """``<name>@<version>`` if ``dataset`` is listed in its manifest with a matching hash."""
    manifest_path = manifest_path or dataset.parent / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    entry = read_manifest(manifest_path).datasets.get(dataset.name)
    if entry is None:
        return None
    if file_sha256(dataset) != entry.sha256:
        log.warning(
            "%s does not match %s (sha256 changed): no dataset_version recorded",
            dataset.name,
            manifest_path,
        )
        return None
    return f"{dataset.stem}@{entry.version}"


def unlisted(datasets_dir: Path, manifest: Manifest, patterns: Sequence[str]) -> list[str]:
    """Dataset files on disk that the manifest doesn't list (paths relative to
    ``datasets_dir``, so a file in a subfolder such as ``splits/`` matches its entry)."""
    on_disk = {
        p.relative_to(datasets_dir).as_posix()
        for pattern in patterns
        for p in datasets_dir.glob(pattern)
    }
    return sorted(on_disk - set(manifest.datasets))


def upsert_entry(datasets_dir: Path, name: str, spec: DatasetSpec) -> Manifest:
    """Add or replace one entry (hashed now) in ``datasets_dir/MANIFEST.json``."""
    path = datasets_dir / MANIFEST_NAME
    current = read_manifest(path) if path.exists() else Manifest(datasets={})
    specs = {
        n: DatasetSpec(
            **e.model_dump(include={"version", "sources", "kind", "description", "metadata"})
        )
        for n, e in current.datasets.items()
    }
    specs[name] = spec
    # a source may be a file next to it that this manifest doesn't list (yet)
    manifest = build_manifest(datasets_dir, specs, sources_on_disk_ok=True)
    write_manifest(manifest, path)
    return manifest


def case_dataset_files(datasets_dir: Path) -> list[Path]:
    """The TestCase files in ``datasets_dir``: by MANIFEST kind when there is a manifest
    (chunks, label samples and labels are other records), else every ``*.jsonl``."""
    path = datasets_dir / MANIFEST_NAME
    if not path.exists():
        return sorted(datasets_dir.glob("*.jsonl"))
    return sorted(
        datasets_dir / name
        for name, entry in read_manifest(path).datasets.items()
        if entry.kind == "testcases" and (datasets_dir / name).exists()
    )
