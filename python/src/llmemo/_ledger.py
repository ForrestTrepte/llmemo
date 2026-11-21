"""The spending ledger (README M7, spec §10): a committed record of paid backend calls."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import subprocess
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._entry import TIMESTAMP
from ._errors import FormatError
from ._json import canonical_bytes, parse_json
from ._stored import HEX64

LEDGER_FORMAT = 1
GITATTRIBUTES = b"# Written by llmemo.\n*.jsonl text eol=lf\n"
DEFAULT_FOLDER = "llm-spend"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _str(v: object) -> bool:
    return type(v) is str


def _nonneg_number(v: object) -> bool:
    return type(v) in (int, float) and v >= 0  # type: ignore[operator]


def _nonneg_int(v: object) -> bool:
    return type(v) is int and v >= 0


def _timestamp(v: object) -> bool:
    return type(v) is str and TIMESTAMP.fullmatch(v) is not None


def _entry_id(v: object) -> bool:
    return type(v) is str and HEX64.fullmatch(v) is not None


_COMMON = {"at": _timestamp, "by": _str}
_OPTIONAL_COMMON = {"session": _str, "run": _str}
_KINDS = {
    "call": (
        {"entryId": _entry_id, "attempt": _nonneg_int, "costUsd": _nonneg_number},
        {
            "label": _str,
            "variant": _str,
            "model": _str,
            "billing": _str,
            "codeVersion": _str,
            "duplicate": lambda v: type(v) is bool,
        },
    ),
    "key-total": (
        {"provider": _str, "usageUsd": _nonneg_number},
        {"limitUsd": _nonneg_number, "source": _str},
    ),
}


def check_row(row: object) -> bool:
    """Raise FormatError unless row is valid (§10.3). False for a kind llmemo doesn't know."""
    if not isinstance(row, dict) or "ledgerFormat" not in row:
        raise FormatError("schema", "a ledger row must be an object with ledgerFormat")
    fmt = row["ledgerFormat"]
    if type(fmt) is not int or fmt != LEDGER_FORMAT:
        raise FormatError("unsupported-format", f"ledgerFormat {fmt!r}")
    kind = row.get("kind")
    if not _str(kind):
        raise FormatError("schema", "a ledger row needs a kind")
    if kind not in _KINDS:
        return False
    required, optional = _KINDS[kind]
    for name, ok in {**_COMMON, **required}.items():
        if name not in row or not ok(row[name]):
            raise FormatError("schema", f"{kind} row: {name} is missing or invalid")
    for name, ok in {**_OPTIONAL_COMMON, **optional}.items():
        if name in row and not ok(row[name]):
            raise FormatError("schema", f"{kind} row: {name} is invalid")
    return True


def ledger_part(name: str) -> str:
    """A safe folder or file name (§10.1)."""
    part = _UNSAFE.sub("-", name)
    return "-" if part.strip(".") == "" else part


def ledger_relpath(branch: str, developer: str) -> str:
    parts = [ledger_part(p) for p in branch.split("/")]
    return "/".join([*parts, ledger_part(developer) + ".jsonl"])


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out or None


def _existing(path: Path) -> Path:
    path = path.absolute()
    while not path.exists():
        path = path.parent
    return path


def _now() -> str:
    t = datetime.now(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _run_id() -> str:
    t = datetime.now(UTC)
    return t.strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z-{secrets.token_hex(4)}"


class Ledger:
    """Appends paid calls to `<root>/<branch>/<developer>.jsonl` (spec §10).

    Pass one to `Cache(ledger=...)`; it can be shared by several caches. `root` defaults to
    `llm-spend/` at the top of the current git repository. `branch` defaults to git's
    current branch (`no-branch` if HEAD is detached), and `developer` to `GITHUB_USER`,
    else git's `user.name`, else `unknown`; never an email address. `session` defaults to
    Claude Code's `CLAUDE_CODE_SESSION_ID`. `run` identifies this run's rows, and defaults
    to a new ID.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        branch: str | None = None,
        developer: str | None = None,
        session: str | None = None,
        run: str | None = None,
    ) -> None:
        if root is None:
            top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
            if top is None:
                raise ValueError("Ledger() needs a root folder outside a git repository")
            root = Path(top) / DEFAULT_FOLDER
        self.root = Path(root)
        where = _existing(self.root)
        if branch is None:
            branch = _git(where, "symbolic-ref", "--short", "-q", "HEAD") or "no-branch"
        if developer is None:
            developer = (
                os.environ.get("GITHUB_USER") or _git(where, "config", "user.name") or "unknown"
            )
        self.branch = branch
        self.developer = developer
        self.session = session if session is not None else os.environ.get("CLAUDE_CODE_SESSION_ID")
        self.run = run if run is not None else _run_id()
        self.path = self.root / ledger_relpath(branch, developer)
        self._mutex = threading.Lock()

    def __repr__(self) -> str:
        return f"Ledger({str(self.path)!r})"

    def append(self, row: dict[str, Any]) -> None:
        """Add `ledgerFormat`, `session`, and `run`, check the row, and append it (§10.3)."""
        full: dict[str, Any] = {"ledgerFormat": LEDGER_FORMAT, **row}
        if self.session:
            full.setdefault("session", self.session)
        full.setdefault("run", self.run)
        if not check_row(full):
            raise ValueError(f"unknown ledger row kind {full.get('kind')!r}")
        line = canonical_bytes(full) + b"\n"
        with self._mutex:
            if not self.root.exists():
                self.root.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(FileExistsError):
                    fd = os.open(self.root / ".gitattributes", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(fd, "wb") as f:
                        f.write(GITATTRIBUTES)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)  # one write, so parallel appenders don't interleave
            finally:
                os.close(fd)

    def record_call(
        self,
        *,
        entry_id: str,
        attempt: int,
        cost_usd: float,
        at: str,
        by: str,
        label: str | None = None,
        variant: str | None = None,
        model: str | None = None,
        billing: str | None = None,
        code_version: str | None = None,
        duplicate: bool = False,
    ) -> None:
        """A `call` row. The Cache writes these for every backend call that reports a cost."""
        row: dict[str, Any] = {
            "kind": "call",
            "at": at,
            "by": by,
            "entryId": entry_id,
            "attempt": attempt,
            "costUsd": cost_usd,
        }
        optional = {
            "label": label,
            "variant": variant,
            "model": model,
            "billing": billing,
            "codeVersion": code_version,
            "duplicate": True if duplicate else None,
        }
        row.update({k: v for k, v in optional.items() if v is not None})
        self.append(row)

    def record_key_total(
        self,
        provider: str,
        usage_usd: float,
        *,
        limit_usd: float | None = None,
        source: str | None = None,
        by: str | None = None,
    ) -> None:
        """A `key-total` row: an API key's total spending as the provider reports it."""
        from ._cache import default_recorded_by

        row: dict[str, Any] = {
            "kind": "key-total",
            "at": _now(),
            "by": by or default_recorded_by(),
            "provider": provider,
            "usageUsd": usage_usd,
        }
        if limit_usd is not None:
            row["limitUsd"] = limit_usd
        if source is not None:
            row["source"] = source
        self.append(row)


def parse_ledger(data: bytes) -> list[dict[str, Any]]:
    """The rows of a ledger file, skipping kinds llmemo doesn't know (§10.3)."""
    rows = []
    for line in data.split(b"\n"):
        line = line.removesuffix(b"\r")
        if not line:
            continue
        row = parse_json(line)
        if check_row(row):
            rows.append(row)
    return rows


@dataclass(frozen=True)
class LedgerFile:
    """One ledger file: its branch and developer (from its path, §10.3) and its rows."""

    branch: str
    developer: str
    path: Path
    rows: list[dict[str, Any]]


def read_ledgers(root: str | os.PathLike[str] | None = None) -> list[LedgerFile]:
    """Every ledger file under `root` (default: `llm-spend/` at the top of the git repo)."""
    if root is None:
        top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
        if top is None:
            raise ValueError("read_ledgers() needs a root folder outside a git repository")
        root = Path(top) / DEFAULT_FOLDER
    base = Path(root)
    out = []
    for path in sorted(base.rglob("*.jsonl")):
        rel = path.relative_to(base)
        try:
            rows = parse_ledger(path.read_bytes())
        except FormatError as e:
            e.add_note(f"in ledger {path}")
            raise
        out.append(LedgerFile("/".join(rel.parts[:-1]), rel.stem, path, rows))
    return out


@dataclass
class Spending:
    """Totals of call rows: how many, and what they cost."""

    calls: int = 0
    cost_usd: float = 0.0


def summarize_spending(
    ledgers: Iterable[LedgerFile], by: Sequence[str] = ("branch", "developer")
) -> dict[tuple[Any, ...], Spending]:
    """Totals of call rows, grouped by any of `branch`, `developer`, and row members such
    as `by`, `model`, `billing`, `label`, or `run`. A missing member groups as None."""
    out: dict[tuple[Any, ...], Spending] = {}
    for ledger in ledgers:
        names = {"branch": ledger.branch, "developer": ledger.developer}
        for row in ledger.rows:
            if row["kind"] != "call":
                continue
            group = tuple(names[k] if k in names else row.get(k) for k in by)
            s = out.setdefault(group, Spending())
            s.calls += 1
            s.cost_usd += row["costUsd"]
    return out
