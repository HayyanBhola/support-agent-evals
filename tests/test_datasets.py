"""Every committed dataset is valid, and the known-bug set says what it claims."""

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from evalkit.importers.chunks import ChunkRecord
from evalkit.importers.combine import DuplicateCaseIds, combine_datasets
from evalkit.io import read_jsonl, write_jsonl
from evalkit.manifest import case_dataset_files
from evalkit.schemas import Route, TestCase
from evalkit.snapshot_router import load_products, load_router

from .agent_double import make_case

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
SNAPSHOT = ROOT / "agent_snapshot"
# TestCase files, by their MANIFEST kind (chunks, label samples and labels are other records)
CASE_FILES = case_dataset_files(DATASETS)


@pytest.fixture(scope="module")
def predict() -> Callable[[str], str]:
    return load_router(SNAPSHOT)


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_dataset_is_valid_with_unique_ids(path: Path) -> None:
    cases = list(read_jsonl(path, TestCase))
    assert cases
    assert not [i for i, n in Counter(c.id for c in cases).items() if n > 1]


# Derived datasets re-use their sources' cases (and ids): combinations and review stages.
DERIVED = {
    "day4_baseline.jsonl": ("legacy_v1.jsonl", "known_bugs_v1.jsonl"),
    "synthetic_dedup_v1.jsonl": ("synthetic_raw_v1.jsonl",),
    "synthetic_reviewed_v1.jsonl": ("synthetic_dedup_v1.jsonl",),
    "golden_v1.jsonl": ("legacy_v1.jsonl", "known_bugs_v1.jsonl", "synthetic_reviewed_v1.jsonl"),
    "golden_v1.1.jsonl": ("golden_v1.jsonl",),
    "golden_v1.2.jsonl": ("golden_v1.1.jsonl",),
}


def test_source_datasets_share_no_ids() -> None:
    sources = [p for p in CASE_FILES if p.name not in DERIVED]
    ids = [c.id for p in sources for c in read_jsonl(p, TestCase)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", [n for n in DERIVED if (DATASETS / n).exists()])
def test_derived_dataset_ids_come_from_its_sources(name: str) -> None:
    source_ids = {c.id for s in DERIVED[name] for c in read_jsonl(DATASETS / s, TestCase)}
    assert {c.id for c in read_jsonl(DATASETS / name, TestCase)} <= source_ids


def test_known_bugs_are_greetings_the_snapshot_router_misses(predict: Callable[[str], str]) -> None:
    cases = list(read_jsonl(DATASETS / "known_bugs_v1.jsonl", TestCase))
    bugs = [c for c in cases if "known_bug" in c.tags]
    assert len(bugs) == 6
    for c in bugs:
        assert c.expected_route is Route.GREETING and c.should_escalate is False
        # Documents the bug at the snapshot commit. When the agent is fixed and the
        # snapshot refreshed, this fails: move the case to the regular greeting set.
        assert predict(c.question) == "knowledge", c.id


def test_known_bug_controls_match_the_snapshot_router(predict: Callable[[str], str]) -> None:
    cases = list(read_jsonl(DATASETS / "known_bugs_v1.jsonl", TestCase))
    controls = [c for c in cases if "known_bug_control" in c.tags]
    assert len(controls) == 4
    for c in controls:
        assert c.expected_route is not None and c.expected_route is not Route.GREETING
        assert predict(c.question) == c.expected_route.value, c.id


def test_legacy_routes_match_the_snapshot_router(predict: Callable[[str], str]) -> None:
    for c in read_jsonl(DATASETS / "legacy_v1.jsonl", TestCase):
        if c.history or c.expected_route is None:
            continue  # memory can change the route; null = not checked
        assert predict(c.question) == c.expected_route.value, c.id


def test_control_gold_chunks_exist() -> None:
    chunk_ids = {c.id for c in read_jsonl(DATASETS / "agent_chunks.jsonl", ChunkRecord)}
    for c in read_jsonl(DATASETS / "known_bugs_v1.jsonl", TestCase):
        assert set(c.gold_context_ids) <= chunk_ids, c.id


def test_products_are_renamed_to_the_database_key() -> None:
    products = load_products(SNAPSHOT)
    assert products[0] == {"product_id": "GC-P001", "name": "Hydra Balance Moisturizer"}


def test_combine_preserves_order_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_jsonl(a, [make_case("a1", "One?"), make_case("a2", "Two?")])
    write_jsonl(b, [make_case("b1", "Three?")])
    out = tmp_path / "out.jsonl"
    assert [c.id for c in combine_datasets([a, b], out)] == ["a1", "a2", "b1"]
    assert out.read_bytes() == a.read_bytes() + b.read_bytes()
    with pytest.raises(DuplicateCaseIds, match=r"\['a1', 'a2'\]"):
        combine_datasets([a, a], out)


def test_committed_day4_baseline_is_legacy_plus_known_bugs(tmp_path: Path) -> None:
    fresh = tmp_path / "baseline.jsonl"
    combine_datasets([DATASETS / "legacy_v1.jsonl", DATASETS / "known_bugs_v1.jsonl"], fresh)
    assert fresh.read_bytes() == (DATASETS / "day4_baseline.jsonl").read_bytes(), (
        "datasets/day4_baseline.jsonl is stale: run scripts/combine_datasets.py"
    )


def test_golden_v1_rebuilds_byte_identical(tmp_path: Path) -> None:
    from evalkit.importers.combine import build_golden

    sources = [DATASETS / s for s in DERIVED["golden_v1.jsonl"]]
    build_golden(sources, tmp_path / "a.jsonl")
    build_golden(list(reversed(sources)), tmp_path / "b.jsonl")  # input order doesn't matter
    committed = (DATASETS / "golden_v1.jsonl").read_bytes()
    assert (tmp_path / "a.jsonl").read_bytes() == committed
    assert (tmp_path / "b.jsonl").read_bytes() == committed
    cases = list(read_jsonl(DATASETS / "golden_v1.jsonl", TestCase))
    assert len(cases) == sum(len(list(read_jsonl(s, TestCase))) for s in sources) == 405
    order = ["legacy", "human", "synthetic"]
    assert [(order.index(c.source.value), c.id) for c in cases] == sorted(
        (order.index(c.source.value), c.id) for c in cases
    )


def test_build_golden_rejects_duplicate_ids(tmp_path: Path) -> None:
    from evalkit.importers.combine import DuplicateCaseIds, build_golden

    path = DATASETS / "known_bugs_v1.jsonl"
    with pytest.raises(DuplicateCaseIds, match="bug-greeting-hi-again"):
        build_golden([path, path], tmp_path / "x.jsonl")


def test_golden_v1_1_is_golden_v1_with_only_the_patched_case_changed(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_golden", ROOT / "scripts" / "build_golden.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(tmp_path / "v1.jsonl", tmp_path / "v1_1.jsonl", tmp_path / "v1_2.jsonl") == 0
    assert (tmp_path / "v1_1.jsonl").read_bytes() == (DATASETS / "golden_v1.1.jsonl").read_bytes()
    v1 = {c.id: c for c in read_jsonl(DATASETS / "golden_v1.jsonl", TestCase)}
    v11 = {c.id: c for c in read_jsonl(DATASETS / "golden_v1.1.jsonl", TestCase)}
    assert list(v1) == list(v11)
    assert (
        [i for i in v1 if v1[i] != v11[i]] == list(module.PATCHES_V1_1) == ["legacy-adv-fake-price"]
    )
    with pytest.raises(KeyError, match="not in the golden set"):
        module.apply_patches(list(v1.values()), {"nope": {}})


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_no_forbidden_price_fragment_matches_a_valid_price(path: Path) -> None:
    """Day 8 regression: a must_not_include phrase ending in a price must not match a longer
    valid price of the same case. golden_v1 (frozen, data-v1.0) keeps the one known bug."""
    from evalkit.metrics.rules import price_fragment_conflicts

    conflicts = [x for c in read_jsonl(path, TestCase) for x in price_fragment_conflicts(c)]
    known = {"legacy_v1.jsonl", "day4_baseline.jsonl", "golden_v1.jsonl"}
    if path.name in known:
        assert conflicts == ["legacy-adv-fake-price: must_not_include 'costs $1' matches $19.99"]
    else:
        assert conflicts == []


def test_golden_v1_2_only_sets_should_escalate_false_on_out_of_scope() -> None:
    v11 = {c.id: c for c in read_jsonl(DATASETS / "golden_v1.1.jsonl", TestCase)}
    v12 = {c.id: c for c in read_jsonl(DATASETS / "golden_v1.2.jsonl", TestCase)}
    assert list(v11) == list(v12)
    changed = [i for i in v11 if v11[i] != v12[i]]
    oos = [c.id for c in v11.values() if c.category == "out_of_scope"]
    assert changed == oos and len(changed) == 19
    for i in changed:
        assert v11[i].should_escalate is None and v12[i].should_escalate is False
        assert v11[i].model_copy(update={"should_escalate": False}) == v12[i]
