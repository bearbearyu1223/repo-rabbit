"""Crash-safe persistence for `.repo-rabbit/`.

Everything the user wrote or the agent answered lives in two JSONL files. They
must survive crashes, concurrent writers (the server and the CLI) and re-renders:

* appends take a lock, write one line and fsync;
* rewrites go to a temporary file in the same directory and are renamed over
  the original, so a reader only ever sees the old file or the new one;
* a line that cannot be parsed is never dropped: a rewrite moves it to
  ``cache/quarantine.jsonl``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import fcntl
except ImportError:  # Windows: writes stay atomic, but concurrent writers are not serialized.
    fcntl = None  # type: ignore[assignment]

QUESTION_STATUSES = ("open", "investigating", "answered", "failed", "noted")
ANCHOR_STATES = ("ok", "drifted", "orphaned")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Paths:
    """Every location RepoRabbit writes to, all under ``<repo>/.repo-rabbit``."""

    repo: Path

    @property
    def root(self) -> Path:
        return self.repo / ".repo-rabbit"

    @property
    def atlas(self) -> Path:
        return self.root / "atlas.json"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def index(self) -> Path:
        return self.root / "index.html"

    @property
    def questions(self) -> Path:
        return self.root / "questions.jsonl"

    @property
    def answers(self) -> Path:
        return self.root / "answers.jsonl"

    @property
    def tours(self) -> Path:
        return self.root / "tours.jsonl"

    @property
    def activity(self) -> Path:
        return self.root / "activity.jsonl"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def settings(self) -> Path:
        return self.cache / "settings.json"

    @property
    def server_state(self) -> Path:
        return self.cache / "server.json"

    @property
    def quarantine(self) -> Path:
        return self.cache / "quarantine.jsonl"

    @property
    def lock_file(self) -> Path:
        return self.cache / ".lock"

    def ensure(self) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def locked(paths: Paths) -> Iterator[None]:
    """Serialize writers across processes (the server and the CLI)."""
    paths.ensure()
    with open(paths.lock_file, "a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` so that ``path`` is always either complete-old or complete-new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777  # A rewrite must not change who can read the file.
    except FileNotFoundError:
        mode = None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):  # Directory fsync is unsupported on some filesystems.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    """Return ``(records, unparseable_lines)``. A missing file is empty."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    records: list[dict] = []
    bad: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            bad.append(line)
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            bad.append(line)
    return records, bad


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a+b") as out:
        out.seek(0, os.SEEK_END)
        if out.tell():  # A torn final line must not swallow this record.
            out.seek(-1, os.SEEK_END)
            if out.read(1) != b"\n":
                line = "\n" + line
        out.seek(0, os.SEEK_END)
        out.write(line.encode("utf-8"))
        out.flush()
        os.fsync(out.fileno())


def _rewrite_jsonl(paths: Paths, path: Path, records: Iterable[dict], bad: list[str]) -> None:
    if bad:
        with open(paths.quarantine, "a", encoding="utf-8") as out:
            out.writelines(line + "\n" for line in bad)
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


# -- questions and answers ------------------------------------------------------------


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def load_questions(paths: Paths) -> list[dict]:
    return read_jsonl(paths.questions)[0]


def load_answers(paths: Paths) -> list[dict]:
    return read_jsonl(paths.answers)[0]


def add_question(paths: Paths, fields: dict) -> dict:
    """Persist a new question. ``fields`` must already be validated."""
    record = {
        "id": new_id("q"),
        "kind": fields.get("kind", "question"),
        "question": fields["question"],
        "section_id": fields["section_id"],
        "block_id": fields["block_id"],
        "selected_text": fields.get("selected_text", ""),
        "text_offset": fields.get("text_offset"),
        "source_file": fields.get("source_file"),
        "source_start": fields.get("source_start"),
        "source_end": fields.get("source_end"),
        "parent_id": fields.get("parent_id"),
        "root_id": fields.get("root_id"),
        "created_at": now_iso(),
        "status": "noted" if fields.get("kind") == "note" else "open",
        "anchor": "ok",
    }
    with locked(paths):
        _append_jsonl(paths.questions, record)
    return record


def update_questions(paths: Paths, changes: dict[str, dict]) -> int:
    """Apply ``{question_id: {field: value}}``. Returns how many records changed."""
    if not changes:
        return 0
    with locked(paths):
        records, bad = read_jsonl(paths.questions)
        touched = 0
        for record in records:
            update = changes.get(record.get("id"))
            if update and any(record.get(k) != v for k, v in update.items()):
                record.update(update)
                touched += 1
        if touched or bad:
            _rewrite_jsonl(paths, paths.questions, records, bad)
    return touched


def record_answer(paths: Paths, question_id: str, answer: dict) -> dict:
    """Append ``answer``, and only then mark the question answered, in one critical section.

    If the append raises, the question stays open. A crash between the two
    writes leaves a saved answer on an open question, which the next
    investigation sees (``answers`` on its context) rather than losing.
    """
    with locked(paths):
        records, bad = read_jsonl(paths.questions)
        target = next((r for r in records if r.get("id") == question_id), None)
        if target is None:
            raise KeyError(question_id)
        saved = {"id": new_id("a"), "question_id": question_id, "created_at": now_iso(), **answer}
        _append_jsonl(paths.answers, saved)
        target["status"] = "answered"
        target["answered_at"] = saved["created_at"]
        target.pop("error", None)
        _rewrite_jsonl(paths, paths.questions, records, bad)
    return saved


def mark_failed(paths: Paths, question_id: str, reason: str) -> None:
    if not update_questions(paths, {question_id: {"status": "failed", "error": reason}}):
        known = {q.get("id") for q in load_questions(paths)}
        if question_id not in known:
            raise KeyError(question_id)


def claim_question(paths: Paths, question_id: str) -> None:
    """Show that the agent has picked this question up."""
    known = {q.get("id"): q for q in load_questions(paths)}
    if question_id not in known:
        raise KeyError(question_id)
    update_questions(paths, {question_id: {"status": "investigating", "started_at": now_iso()}})


def log_activity(paths: Paths, message: str, question_id: str | None = None) -> dict:
    """Record one progress line for the page to show. Never required for correctness."""
    record = {"at": now_iso(), "message": message, "question_id": question_id}
    with locked(paths):
        _append_jsonl(paths.activity, record)
    return record


def load_activity(paths: Paths) -> list[dict]:
    return read_jsonl(paths.activity)[0]


def load_tours(paths: Paths) -> list[dict]:
    return read_jsonl(paths.tours)[0]


def update_tours(paths: Paths, changes: dict[str, dict]) -> None:
    with locked(paths):
        records, bad = read_jsonl(paths.tours)
        for record in records:
            record.update(changes.get(record.get("id"), {}))
        _rewrite_jsonl(paths, paths.tours, records, bad)


def delete_tours(paths: Paths, ids: set[str]) -> None:
    with locked(paths):
        records, bad = read_jsonl(paths.tours)
        _rewrite_jsonl(paths, paths.tours, [r for r in records if r.get("id") not in ids], bad)
