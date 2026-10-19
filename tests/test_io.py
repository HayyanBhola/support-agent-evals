from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from evalkit.io import (
    JsonlError,
    append_jsonl,
    drop_partial_last_line,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from evalkit.schemas import AgentResponse, CaseResult, TestCase

from .conftest import RESPONSES
from .strategies import valid_cases

URDU_CASE = TestCase(
    id="urdu-01",
    question="Mera order kahan hai? آپ کا آرڈر 💧",
    category="orders",
    expected_route="clarify",
    must_include=["order number"],
    source="human",
)


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cases=st.lists(valid_cases(), max_size=5))
def test_any_valid_case_round_trips(tmp_path: Path, cases: list[TestCase]) -> None:
    path = tmp_path / "cases.jsonl"
    assert write_jsonl(path, cases) == len(cases)
    assert list(read_jsonl(path, TestCase)) == cases


def test_agent_responses_round_trip_inside_case_results(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 13, tzinfo=UTC)
    results = [
        CaseResult(
            run_id="run-1",
            case_id=p.stem,
            response=AgentResponse.model_validate_json(p.read_bytes()),
            failure_type="agent_error" if p.stem == "error" else None,
            attempts=1,
            started_at=now,
            finished_at=now,
        )
        for p in sorted(RESPONSES.glob("*.json"))
    ]
    write_jsonl(tmp_path / "results.jsonl", results)
    assert list(read_jsonl(tmp_path / "results.jsonl", CaseResult)) == results


def test_written_file_is_utf8_with_lf_only(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    write_jsonl(path, [URDU_CASE, URDU_CASE])
    data = path.read_bytes()
    assert b"\r\n" not in data and data.count(b"\n") == 2
    assert "آپ کا آرڈر 💧".encode() in data  # stored as UTF-8, not \u escapes


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "cases.jsonl"
    write_jsonl(path, [URDU_CASE])
    assert list(read_jsonl(path, TestCase)) == [URDU_CASE]


def test_failed_write_keeps_old_file_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    write_jsonl(path, [URDU_CASE])
    before = path.read_bytes()

    def exploding() -> Iterator[TestCase]:
        yield URDU_CASE
        raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError, match="disk on fire"):
        write_jsonl(path, exploding())
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["cases.jsonl"]


def test_read_reports_file_and_line_of_broken_line(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    good = URDU_CASE.model_dump_json()
    path.write_text(f"{good}\n\n{good}\n{{not json\n{good}\n", encoding="utf-8")
    with pytest.raises(JsonlError) as exc:
        list(read_jsonl(path, TestCase))
    assert exc.value.line_no == 4
    assert str(exc.value).startswith(f"{path}:4: ")


def test_read_reports_line_of_invalid_record(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    bad = URDU_CASE.model_dump_json().replace('"clarify"', '"billing"')
    path.write_text(f"{URDU_CASE.model_dump_json()}\n{bad}\n", encoding="utf-8")
    with pytest.raises(JsonlError, match=r"(?s):2: .*expected_route"):
        list(read_jsonl(path, TestCase))


def test_read_reports_line_of_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_bytes(URDU_CASE.model_dump_json().encode() + b"\n\xff\xfe\n")
    with pytest.raises(JsonlError, match=r":2: not valid UTF-8"):
        list(read_jsonl(path, TestCase))


def test_read_accepts_bom_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    line = URDU_CASE.model_dump_json().encode()
    path.write_bytes(b"\xef\xbb\xbf" + line + b"\r\n" + line + b"\r\n")
    assert list(read_jsonl(path, TestCase)) == [URDU_CASE, URDU_CASE]


def test_read_streams_lazily(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(f"{URDU_CASE.model_dump_json()}\n{{broken\n", encoding="utf-8")
    it = read_jsonl(path, TestCase)
    assert next(it) == URDU_CASE  # first record arrives before the bad line is read
    with pytest.raises(JsonlError):
        next(it)


def test_append_jsonl_adds_lines_and_creates_dirs(tmp_path: Path) -> None:
    path = tmp_path / "run" / "results.jsonl"
    append_jsonl(path, URDU_CASE)
    append_jsonl(path, URDU_CASE)
    assert path.read_bytes().count(b"\n") == 2 and b"\r" not in path.read_bytes()
    assert list(read_jsonl(path, TestCase)) == [URDU_CASE, URDU_CASE]


@pytest.mark.parametrize(
    ("content", "expected", "removed"),
    [
        (b"", b"", False),
        (b'{"a":1}\n', b'{"a":1}\n', False),
        (b'{"a":1}\n{"b":', b'{"a":1}\n', True),
        (b'{"b":', b"", True),
    ],
)
def test_drop_partial_last_line(
    tmp_path: Path, content: bytes, expected: bytes, removed: bool
) -> None:
    path = tmp_path / "results.jsonl"
    path.write_bytes(content)
    assert drop_partial_last_line(path) is removed
    assert path.read_bytes() == expected


def test_write_json_round_trips_atomically(tmp_path: Path) -> None:
    path = tmp_path / "meta" / "case.json"
    write_json(path, URDU_CASE)
    text = path.read_bytes()
    assert text.endswith(b"}\n") and b"\r\n" not in text
    assert read_json(path, TestCase) == URDU_CASE
    path.write_bytes(b"\xef\xbb\xbf" + text)  # BOM tolerated
    assert read_json(path, TestCase) == URDU_CASE
    assert [p.name for p in path.parent.iterdir()] == ["case.json"]
