"""Where entries live: a file store (spec §7) and an in-memory store (S10).

A store holds bytes and knows nothing about the entry format, except the file store's
layout. Everything a reader checks happens in the Cache, so both stores get the same
checks (S8).
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Protocol

from ._entry import FAILURE_ID, LAYOUT_FORMAT
from ._errors import FormatError, LlmemoError
from ._json import json_file_bytes, parse_json

log = logging.getLogger("llmemo")

GITATTRIBUTES = (
    b"# Written by llmemo. Stored text keeps its exact bytes.\n"
    b"*.txt -text\n"
    b"*.f32 binary\n"
    b"*.f64 binary\n"
    b"*.json text eol=lf\n"
)
GITIGNORE = b"# Written by llmemo.\n*.lock\n*.tmp\n"


class Store(Protocol):
    """The storage interface the Cache uses. Names are `<64 hex>.<ext>` (spec §7.1).

    Implementations must be safe to call from several threads.
    """

    def open(self, create: bool) -> None:
        """Check that this is a cache (`not-a-cache`, `unsupported-format`), or create one."""

    def read(self, name: str, limit: int) -> bytes | None:
        """A file's bytes, or None if it doesn't exist. Raises FormatError("too-large")."""

    def exists(self, name: str) -> bool: ...

    def write_blob(self, name: str, data: bytes) -> None:
        """Publish a text or vector file. If it exists already, that's success."""

    def publish(self, name: str, data: bytes) -> bool:
        """Publish an entry without overwriting. False if it already exists (S5)."""

    def replace(self, name: str, data: bytes) -> None:
        """Replace an entry atomically. Only for refresh (M3, S7), with the lock held."""

    def write_failure(self, entry_id: str, failure_id: str, data: bytes) -> None:
        """Publish a failure record (spec §5.3) as `<id>.<failure id>.failure.json`."""

    def try_lock(self, entry_id: str) -> bool:
        """Take the entry's lock without waiting. Takes over an abandoned lock (S6)."""

    def unlock(self, entry_id: str) -> None: ...


class MemoryStore:
    """A cache held in memory, for tests and short sessions (S10). Nothing is saved."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._locks: set[str] = set()
        self._mutex = threading.Lock()

    def open(self, create: bool) -> None:
        pass

    def read(self, name: str, limit: int) -> bytes | None:
        data = self._files.get(name)
        if data is not None and len(data) > limit:
            raise FormatError("too-large", f"{name} is over {limit} bytes")
        return data

    def exists(self, name: str) -> bool:
        return name in self._files

    def write_blob(self, name: str, data: bytes) -> None:
        with self._mutex:
            self._files.setdefault(name, data)

    def publish(self, name: str, data: bytes) -> bool:
        with self._mutex:
            if name in self._files:
                return False
            self._files[name] = data
            return True

    def replace(self, name: str, data: bytes) -> None:
        with self._mutex:
            self._files[name] = data

    def try_lock(self, entry_id: str) -> bool:
        with self._mutex:
            if entry_id in self._locks:
                return False
            self._locks.add(entry_id)
            return True

    def unlock(self, entry_id: str) -> None:
        with self._mutex:
            self._locks.discard(entry_id)

    def write_failure(self, entry_id: str, failure_id: str, data: bytes) -> None:
        with self._mutex:
            self._files[f"{entry_id}.{failure_id}.failure.json"] = data

    def files(self) -> dict[str, bytes]:
        """A copy of everything stored, by name."""
        with self._mutex:
            return dict(self._files)


class FileStore:
    """A cache in a folder, laid out as in spec §7: `<root>/<3 hex>/<id>.<ext>`.

    `lock_refresh_s` and `lock_stale_s` are §7.3's 10 and 60 seconds. `poll_s` is how
    often a process waiting for another's lock checks again. With `memory=True`, files
    read or written are also kept in memory, which is faster for a cache read many times
    in one process but holds every file used.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lock_refresh_s: float = 10.0,
        lock_stale_s: float = 60.0,
        poll_s: float = 0.2,
        memory: bool = False,
    ) -> None:
        if lock_stale_s < 3 * lock_refresh_s:
            raise ValueError("lock_stale_s must be several times lock_refresh_s (spec §7.3)")
        self.root = Path(root)
        self.lock_refresh_s = lock_refresh_s
        self.lock_stale_s = lock_stale_s
        self.poll_s = poll_s
        self._memory: dict[str, bytes] | None = {} if memory else None
        self._held: dict[str, str] = {}  # entry ID -> the token in our lock file
        self._mutex = threading.Lock()
        self._wake = threading.Condition(self._mutex)
        self._refresher: threading.Thread | None = None

    def __repr__(self) -> str:
        return f"FileStore({str(self.root)!r})"

    def path(self, name: str) -> Path:
        return self.root / name[:3] / name

    # Opening ----------------------------------------------------------------------------

    def open(self, create: bool) -> None:
        marker = self.root / "llmemo.json"
        if not marker.exists() and create:
            if self.root.exists() and any(self.root.iterdir()):
                raise FormatError(
                    "not-a-cache",
                    f"{self.root} isn't empty and has no llmemo.json, so llmemo won't write there",
                )
            self.root.mkdir(parents=True, exist_ok=True)
            for name, data in ((".gitattributes", GITATTRIBUTES), (".gitignore", GITIGNORE)):
                with contextlib.suppress(FileExistsError):
                    _write_new(self.root / name, data)
            # The marker goes last, so a cache with a marker is complete.
            self._link_new(
                self.root, "llmemo.json", json_file_bytes({"layoutFormat": LAYOUT_FORMAT})
            )
        if not marker.is_file():
            raise FormatError("not-a-cache", f"{self.root} has no llmemo.json")
        info = parse_json(_read_limited(marker, 64 * 1024))
        if not isinstance(info, dict) or type(info.get("layoutFormat")) is not int:
            raise FormatError("schema", f"{marker} has no layoutFormat")
        if info["layoutFormat"] != LAYOUT_FORMAT:
            raise FormatError("unsupported-format", f"layoutFormat {info['layoutFormat']}")

    # Reading and writing ----------------------------------------------------------------

    def read(self, name: str, limit: int) -> bytes | None:
        if self._memory is not None and name in self._memory:
            data = self._memory[name]
            if len(data) > limit:
                raise FormatError("too-large", f"{name} is over {limit} bytes")
            return data
        try:
            data = _read_limited(self.path(name), limit)
        except FileNotFoundError:
            return None
        if self._memory is not None:
            self._memory[name] = data
        return data

    def exists(self, name: str) -> bool:
        return (self._memory is not None and name in self._memory) or self.path(name).is_file()

    def write_blob(self, name: str, data: bytes) -> None:
        path = self.path(name)
        if not path.exists():
            self._link_new(path.parent, name, data)
        self._remember(name, data)

    def publish(self, name: str, data: bytes) -> bool:
        published = self._link_new(self.path(name).parent, name, data)
        if published:
            self._remember(name, data)
        return published

    def replace(self, name: str, data: bytes) -> None:
        path = self.path(name)
        tmp = self._write_tmp(path.parent, name, data)
        os.replace(tmp, path)  # §7.2: refresh is the one place a file is replaced
        self._remember(name, data)

    def write_failure(self, entry_id: str, failure_id: str, data: bytes) -> None:
        assert FAILURE_ID.fullmatch(failure_id), failure_id
        name = f"{entry_id}.{failure_id}.failure.json"
        self._link_new(self.path(name).parent, name, data)

    def _remember(self, name: str, data: bytes) -> None:
        if self._memory is not None:
            self._memory[name] = data

    def _write_tmp(self, folder: Path, name: str, data: bytes) -> Path:
        folder.mkdir(exist_ok=True)
        stem = name.split(".", 1)[0]
        tmp = folder / f"{stem}.{secrets.token_hex(8)}.tmp"
        _write_new(tmp, data, sync=True)
        return tmp

    def _link_new(self, folder: Path, name: str, data: bytes) -> bool:
        """§7.2: write a temp file, hard-link it into place, remove the temp file."""
        tmp = self._write_tmp(folder, name, data)
        try:
            os.link(tmp, folder / name)
            return True
        except FileExistsError:
            return False
        except OSError as e:
            if e.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
                raise LlmemoError(
                    f"llmemo needs hard links to publish files safely, and {folder} doesn't support"
                    f" them ({e.strerror}). Put the cache on a local disk (spec §7.2)."
                ) from e
            raise
        finally:
            tmp.unlink(missing_ok=True)

    # Locks (spec §7.3) ------------------------------------------------------------------

    def try_lock(self, entry_id: str) -> bool:
        path = self.path(f"{entry_id}.lock")
        token = secrets.token_hex(8)
        content = {
            "acquiredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "token": token,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):  # a second try after taking over an abandoned lock
            try:
                _write_new(path, json_file_bytes(content))
            except FileExistsError:
                if self._take_over(path):
                    continue
                return False
            with self._mutex:
                self._held[entry_id] = token
                self._start_refresher()
            return True
        return False

    def _take_over(self, path: Path) -> bool:
        """Remove an abandoned lock. True if this process removed it."""
        try:
            if time.time() - path.stat().st_mtime <= self.lock_stale_s:
                return False
        except FileNotFoundError:
            return True  # released meanwhile, so try again
        tmp = path.with_name(f"{path.name.split('.')[0]}.{secrets.token_hex(8)}.tmp")
        try:
            os.rename(path, tmp)  # only one process's rename succeeds
        except FileNotFoundError:
            return True
        try:
            if time.time() - tmp.stat().st_mtime <= self.lock_stale_s:
                # Another process took the lock over between our check and our rename.
                # Put its fresh lock back; if that fails, it was replaced again anyway.
                with contextlib.suppress(OSError):
                    os.link(tmp, path)
                return False
            log.warning("llmemo: took over an abandoned lock %s", path)
            return True
        finally:
            tmp.unlink(missing_ok=True)

    def unlock(self, entry_id: str) -> None:
        with self._mutex:
            token = self._held.pop(entry_id, None)
        if token is None:
            return
        path = self.path(f"{entry_id}.lock")
        try:
            ours = token in path.read_text()
        except FileNotFoundError:
            ours = False
        if ours:
            path.unlink(missing_ok=True)
        else:
            log.warning("llmemo: lost the lock %s while holding it; was it taken over?", path)

    def _start_refresher(self) -> None:
        if self._refresher is None or not self._refresher.is_alive():
            self._refresher = threading.Thread(
                target=self._refresh_locks, name="llmemo-lock-refresh", daemon=True
            )
            self._refresher.start()

    def _refresh_locks(self) -> None:
        """Touch every held lock every lock_refresh_s, while backend calls run (§7.3)."""
        with self._mutex:
            while self._held:
                self._wake.wait(self.lock_refresh_s)
                for entry_id in self._held:
                    with contextlib.suppress(FileNotFoundError):
                        os.utime(self.path(f"{entry_id}.lock"))
            self._refresher = None

    # Maintenance ------------------------------------------------------------------------

    def cleanup(self, older_than_s: float = 3600.0) -> int:
        """Delete temp files older than an hour, and abandoned locks (§7.3). Returns a count.

        This lists every prefix folder, so it isn't run automatically (S9).
        """
        removed = 0
        now = time.time()
        for path in self.root.glob("[0-9a-f][0-9a-f][0-9a-f]/*"):
            if path.suffix not in (".tmp", ".lock"):
                continue
            limit = older_than_s if path.suffix == ".tmp" else self.lock_stale_s
            with contextlib.suppress(FileNotFoundError):
                if now - path.stat().st_mtime > limit:
                    path.unlink()
                    removed += 1
        return removed


def _read_limited(path: Path, limit: int) -> bytes:
    """Read a file, refusing more than `limit` bytes without reading them all (S8)."""
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise FormatError("too-large", f"{path.name} is over {limit} bytes")
    return data


def _write_new(path: Path, data: bytes, sync: bool = False) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        if sync:
            f.flush()
            os.fsync(f.fileno())
