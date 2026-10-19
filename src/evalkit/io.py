"""JSONL and JSON reading and writing for evalkit models."""

import codecs
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


class JsonlError(ValueError):
    """A JSONL line could not be decoded or validated. The message starts with path:line."""

    def __init__(self, path: Path, line_no: int, message: str) -> None:
        super().__init__(f"{path}:{line_no}: {message}")
        self.path = path
        self.line_no = line_no


def read_jsonl(path: str | Path, model: type[M]) -> Iterator[M]:
    """Yield one validated ``model`` per non-blank line, streaming.

    Lines are decoded as UTF-8 one at a time (a leading BOM is ignored), so a bad byte or a
    bad record is reported with its file and 1-based line number.
    """
    p = Path(path)
    with p.open("rb") as f:
        for line_no, raw in enumerate(f, start=1):
            if line_no == 1:
                raw = raw.removeprefix(codecs.BOM_UTF8)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise JsonlError(p, line_no, f"not valid UTF-8 ({e})") from e
            if not text.strip():
                continue
            try:
                yield model.model_validate_json(text)
            except ValidationError as e:
                raise JsonlError(p, line_no, str(e)) from e


@contextmanager
def _atomic_text_file(path: Path) -> Iterator[TextIO]:
    """Yield a UTF-8/LF temp file next to ``path``; rename it over ``path`` on success.

    Readers never see a half-written file, and a failure leaves any existing file untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_jsonl(path: str | Path, items: Iterable[BaseModel]) -> int:
    """Atomically write ``items`` as UTF-8 JSONL with LF newlines; return the line count."""
    count = 0
    with _atomic_text_file(Path(path)) as f:
        for item in items:
            f.write(item.model_dump_json())
            f.write("\n")
            count += 1
    return count


def append_jsonl(path: str | Path, item: BaseModel) -> None:
    """Append one record as a line and fsync it, so progress survives a crash."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(item.model_dump_json() + "\n")
        f.flush()
        os.fsync(f.fileno())


def drop_partial_last_line(path: str | Path) -> bool:
    """Remove a trailing line that has no newline (a write cut off by a crash).

    Returns True if something was removed.
    """
    p = Path(path)
    data = p.read_bytes()
    if not data or data.endswith(b"\n"):
        return False
    keep = data.rfind(b"\n") + 1
    with p.open("r+b") as f:
        f.truncate(keep)
    return True


def write_json(path: str | Path, item: BaseModel) -> None:
    """Atomically write one model as pretty-printed UTF-8 JSON with a trailing LF."""
    with _atomic_text_file(Path(path)) as f:
        f.write(item.model_dump_json(indent=2) + "\n")


def read_json(path: str | Path, model: type[M]) -> M:
    """Read and validate one model from a JSON file (UTF-8, BOM tolerated)."""
    return model.model_validate_json(Path(path).read_bytes().removeprefix(codecs.BOM_UTF8))
