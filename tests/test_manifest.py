"""datasets/MANIFEST.json: hashes, counts, versions, and RunMeta.dataset_version."""

import json
import logging
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from evalkit.io import read_json, write_jsonl
from evalkit.manifest import (
    MANIFEST_NAME,
    DatasetSpec,
    build_manifest,
    dataset_version,
    read_manifest,
    stale_entries,
    unlisted,
    write_manifest,
)
from evalkit.runner import META_FILE, RunConfig, run_eval
from evalkit.schemas import RunMeta

from .agent_double import FakeAgent, make_case, ok

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"


# --------------------------------------------------------------------------- the real manifest


def test_committed_manifest_matches_every_file() -> None:
    manifest = read_manifest(DATASETS / MANIFEST_NAME)
    assert stale_entries(manifest, DATASETS) == [], "run scripts/update_manifest.py"


def test_every_dataset_file_is_listed() -> None:
    manifest = read_manifest(DATASETS / MANIFEST_NAME)
    assert unlisted(DATASETS, manifest, ["*.jsonl", "splits/*.json"]) == []
    golden = manifest.datasets["golden_v1.jsonl"]
    assert (golden.version, golden.records) == ("1.0", 405)
    assert golden.sources == [
        "legacy_v1.jsonl",
        "known_bugs_v1.jsonl",
        "synthetic_reviewed_v1.jsonl",
    ]
    assert dataset_version(DATASETS / "golden_v1.jsonl") == "golden_v1@1.0"


# --------------------------------------------------------------------------- behaviour


def make_dir(tmp_path: Path) -> Path:
    write_jsonl(tmp_path / "a.jsonl", [make_case("a1", "q one")])
    write_jsonl(tmp_path / "b.jsonl", [make_case("b1", "q two"), make_case("b2", "q three")])
    specs = {
        "a.jsonl": DatasetSpec(version="1.0"),
        "b.jsonl": DatasetSpec(version="2.1", sources=["a.jsonl"], description="built"),
    }
    write_manifest(build_manifest(tmp_path, specs), tmp_path / MANIFEST_NAME)
    return tmp_path


def test_build_and_read_manifest(tmp_path: Path) -> None:
    d = make_dir(tmp_path)
    data = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert list(data["datasets"]) == ["a.jsonl", "b.jsonl"]
    assert data["datasets"]["b.jsonl"]["records"] == 2
    assert data["datasets"]["b.jsonl"]["sources"] == ["a.jsonl"]
    assert stale_entries(read_manifest(d / MANIFEST_NAME), d) == []


def test_build_manifest_needs_listed_sources_and_files(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "a.jsonl", [make_case("a1", "q one")])
    with pytest.raises(ValueError, match="x.jsonl"):
        build_manifest(tmp_path, {"a.jsonl": DatasetSpec(version="1", sources=["x.jsonl"])})
    with pytest.raises(ValueError, match="gone.jsonl"):
        build_manifest(tmp_path, {"gone.jsonl": DatasetSpec(version="1")})


def test_stale_entries_detects_changes(tmp_path: Path) -> None:
    d = make_dir(tmp_path)
    manifest = read_manifest(d / MANIFEST_NAME)
    write_jsonl(d / "a.jsonl", [make_case("a1", "q one changed")])
    (d / "b.jsonl").unlink()
    assert stale_entries(manifest, d) == ["a.jsonl: sha256 changed", "b.jsonl: missing"]


def test_stale_entries_detects_a_count_change_with_the_same_hash(tmp_path: Path) -> None:
    d = make_dir(tmp_path)
    manifest = read_manifest(d / MANIFEST_NAME)
    edited = manifest.model_copy(
        update={
            "datasets": {"a.jsonl": manifest.datasets["a.jsonl"].model_copy(update={"records": 9})}
        }
    )
    assert stale_entries(edited, d) == ["a.jsonl: record count changed"]


def test_dataset_version(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    d = make_dir(tmp_path)
    assert dataset_version(d / "b.jsonl") == "b@2.1"
    write_jsonl(d / "c.jsonl", [make_case("c1", "q")])
    assert dataset_version(d / "c.jsonl") is None  # not listed
    write_jsonl(d / "a.jsonl", [make_case("a1", "edited")])
    with caplog.at_level(logging.WARNING, logger="evalkit.manifest"):
        assert dataset_version(d / "a.jsonl") is None  # hash mismatch
    assert "sha256 changed" in caplog.text
    other = tmp_path / "other"
    other.mkdir()
    write_jsonl(other / "x.jsonl", [make_case("x1", "q")])
    assert dataset_version(other / "x.jsonl") is None  # no manifest


async def test_run_records_the_dataset_version(tmp_path: Path) -> None:
    d = make_dir(tmp_path / "ds")
    agent = FakeAgent().on("q two", ok("knowledge")).on("q three", ok("knowledge"))
    cfg = RunConfig(dataset=d / "b.jsonl", runs_dir=tmp_path / "runs", cache_dir=tmp_path / "c")
    async with agent.adapter() as adapter:
        outcome = await run_eval(cfg, adapter, Console(file=StringIO()))
    meta = read_json(outcome.run_dir / META_FILE, RunMeta)
    assert meta.dataset_version == "b@2.1"


def test_upsert_entry_and_keeping_generated_entries(tmp_path: Path) -> None:
    from evalkit.manifest import upsert_entry

    d = make_dir(tmp_path)
    write_jsonl(d / "gen.jsonl", [make_case("g1", "q gen")])
    upsert_entry(d, "gen.jsonl", DatasetSpec(version="1.0", sources=["b.jsonl"],
                                             metadata={"seed": 42}))  # fmt: skip
    m = read_manifest(d / MANIFEST_NAME)
    assert m.datasets["gen.jsonl"].metadata == {"seed": 42} and "a.jsonl" in m.datasets
    # a rebuild from specs that don't list gen.jsonl keeps it (re-hashed)
    write_jsonl(d / "gen.jsonl", [make_case("g1", "q gen"), make_case("g2", "q more")])
    rebuilt = build_manifest(d, {"a.jsonl": DatasetSpec(version="1.0"),
                                 "b.jsonl": DatasetSpec(version="2.1", sources=["a.jsonl"])},
                             keep=m)  # fmt: skip
    assert rebuilt.datasets["gen.jsonl"].records == 2
    assert rebuilt.datasets["gen.jsonl"].metadata == {"seed": 42}
    # a source that is neither listed nor on disk is still refused
    with pytest.raises(ValueError, match="nowhere.jsonl"):
        upsert_entry(d, "gen.jsonl", DatasetSpec(version="1.0", sources=["nowhere.jsonl"]))


def test_case_dataset_files_by_kind(tmp_path: Path) -> None:
    from evalkit.manifest import case_dataset_files

    d = make_dir(tmp_path)
    write_jsonl(d / "chunks.jsonl", [make_case("x", "q")])
    upsert = __import__("evalkit.manifest", fromlist=["upsert_entry"]).upsert_entry
    upsert(d, "chunks.jsonl", DatasetSpec(version="1", kind="chunks"))
    assert [p.name for p in case_dataset_files(d)] == ["a.jsonl", "b.jsonl"]
    other = tmp_path / "plain"
    other.mkdir()
    write_jsonl(other / "z.jsonl", [make_case("z", "q")])
    assert [p.name for p in case_dataset_files(other)] == ["z.jsonl"]
