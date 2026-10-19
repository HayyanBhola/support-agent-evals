import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from evalkit.hashing import canonical_json, file_sha256, stable_hash

from .strategies import valid_cases

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=20,
)


@given(st.dictionaries(st.text(), json_values, max_size=8), st.randoms())
def test_key_order_does_not_change_hash(d: dict[str, Any], rnd: Any) -> None:
    items = list(d.items())
    rnd.shuffle(items)
    assert stable_hash(dict(items)) == stable_hash(d)


def test_known_value() -> None:
    assert canonical_json({"b": 1, "a": [1, "ü"]}) == '{"a":[1,"ü"],"b":1}'
    expected = hashlib.sha256('{"a":[1,"ü"],"b":1}'.encode()).hexdigest()
    assert stable_hash({"b": 1, "a": [1, "ü"]}) == expected


def test_different_values_differ() -> None:
    assert stable_hash({"a": 1}) != stable_hash({"a": "1"})


@given(valid_cases())
def test_model_hashes_like_its_json_dump(case: Any) -> None:
    assert stable_hash(case) == stable_hash(case.model_dump(mode="json"))
    assert stable_hash({"case": case}) == stable_hash({"case": case.model_dump(mode="json")})


@pytest.mark.parametrize("bad", [{"when": datetime(2026, 10, 13, tzinfo=UTC)}, {1, 2}, b"x"])
def test_non_json_values_raise_type_error(bad: object) -> None:
    with pytest.raises(TypeError, match="convert it to JSON first"):
        stable_hash(bad)


def test_nan_is_rejected() -> None:
    with pytest.raises(ValueError):
        stable_hash({"x": math.nan})


def test_file_sha256(tmp_path: Path) -> None:
    data = "آپ کا آرڈر\n".encode() * 100_000  # > 1 chunk
    path = tmp_path / "data.jsonl"
    path.write_bytes(data)
    assert file_sha256(path) == hashlib.sha256(data).hexdigest()
    assert file_sha256(path, chunk_size=7) == hashlib.sha256(data).hexdigest()
