"""The split loader (Day 13): dev loads freely; test needs final=True, a reason and the env var."""

import subprocess
from pathlib import Path

import pytest

from evalkit.calibration import loader
from evalkit.calibration.loader import FINAL_ENV, TestSplitLocked, load_labels
from evalkit.calibration.split import read_split

ROOT = Path(__file__).resolve().parent.parent
SPLIT = ROOT / "datasets" / "splits" / "split_v1.json"


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(FINAL_ENV, raising=False)
    return tmp_path / "reports" / "test_access.log"


def test_dev_loads_and_never_touches_the_log(log: Path) -> None:
    dev = load_labels("dev", split_path=SPLIT, access_log=log)
    assert sorted(dev) == read_split(SPLIT).ids("dev") and len(dev) == 96
    assert not log.exists()


@pytest.mark.parametrize(
    ("final", "reason", "env", "needs"),
    [
        (False, None, None, "final=True, a reason, EVALKIT_FINAL_TEST=1"),
        (True, "final eval", None, "EVALKIT_FINAL_TEST=1"),
        (False, "final eval", "1", "final=True"),
        (True, "  ", "1", "a reason"),
        (True, "final eval", "yes", "EVALKIT_FINAL_TEST=1"),
    ],
)
def test_test_split_is_locked(
    log: Path,
    monkeypatch: pytest.MonkeyPatch,
    final: bool,
    reason: str | None,
    env: str | None,
    needs: str,
) -> None:
    if env is not None:
        monkeypatch.setenv(FINAL_ENV, env)
    with pytest.raises(TestSplitLocked, match="locked") as info:
        load_labels("test", final=final, reason=reason, split_path=SPLIT, access_log=log)
    assert needs.split(", ")[0] in str(info.value)
    assert not log.exists()


def test_test_split_loads_with_both_and_logs_the_access(
    log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FINAL_ENV, "1")
    test = load_labels("test", final=True, reason="final judge eval\n(Day 20)", split_path=SPLIT,
                       access_log=log)  # fmt: skip
    assert sorted(test) == read_split(SPLIT).ids("test") and len(test) == 64
    [line] = log.read_text(encoding="utf-8").splitlines()
    when, commit, caller, reason = line.split("\t")
    assert when.endswith("+00:00") and reason == "final judge eval (Day 20)"
    assert commit == loader.git_commit() and commit != ""
    module, function, lineno = caller.split(":")
    assert module.endswith("test_loader") and lineno.isdigit()
    assert function == "test_test_split_loads_with_both_and_logs_the_access"
    load_labels("test", final=True, reason="again", split_path=SPLIT, access_log=log)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2  # appended, not rewritten


def test_other_labels_files_and_errors(log: Path, tmp_path: Path) -> None:
    labels = ROOT / "datasets" / "labels_v1.jsonl"  # an older version, same case_ids
    assert len(load_labels("dev", split_path=SPLIT, labels_path=labels, access_log=log)) == 96
    assert load_labels("dev", split_path=SPLIT, labeler="hayyan", access_log=log)
    with pytest.raises(ValueError, match="have no label"):
        load_labels("dev", split_path=SPLIT, labeler="nobody", access_log=log)
    with pytest.raises(ValueError, match="unknown split"):
        load_labels("train", split_path=SPLIT, access_log=log)  # type: ignore[arg-type]


def test_git_commit_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: object, **k: object) -> None:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(loader.subprocess, "run", broken)
    assert loader.git_commit() == "unknown"
