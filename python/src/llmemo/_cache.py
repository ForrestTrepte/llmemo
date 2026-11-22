"""The Cache: look up a key, or call the backend and record its reply."""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from ._entry import (
    DEFAULT_MAX_BLOB_BYTES,
    DEFAULT_MAX_ENTRY_BYTES,
    Built,
    build_entry,
    check_key_matches,
    entry_id_of,
    logical_content,
    make_key,
    parse_entry,
)
from ._errors import BackendError, CacheMiss, FormatError, ReplayedError
from ._ledger import Ledger
from ._stats import SpendLimit, Stats
from ._store import FileStore, Store, log
from ._stored import sha256_hex

Mode = Literal["record", "replay"]
Failures = Literal["discard", "save", "replay"]
MODES: tuple[Mode, ...] = ("record", "replay")


@dataclass(frozen=True)
class Usage:
    """Token counts a backend reports (E2). Leave a count None when it isn't reported."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None  # the provider's prompt cache, not llmemo's
    cache_write_tokens: int | None = None

    def to_json(self) -> dict[str, int]:
        names = {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
        }
        return {k: v for k, v in names.items() if v is not None}


@dataclass(frozen=True)
class Reply:
    """What a backend returns: the raw reply (E1), and what it reported about the call.

    A backend may also return a bare JSON value, which is the same as `Reply(value)`.
    Mark embedding arrays in `value` with `Vector` to store them as binary files.
    """

    value: Any
    usage: Usage | None = None
    cost_usd: float | None = None
    served_model: str | None = None
    served_by: str | None = None
    tool_version: str | None = None
    billing: str | None = None
    """`"api"` (money spent) or `"subscription"` (`cost_usd` is an API-equivalent figure)."""


@dataclass(frozen=True)
class CallInfo:
    """Identifies a call, for refresh selection (M3) and hooks (A5)."""

    entry_id: str
    request: Any
    label: str | None
    variant: str | None
    attempt: int
    tags: Mapping[str, str]
    extra: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Result:
    """The answer to a call (A4). `reply` is the logical reply, exactly as a later replay
    returns it (e.g. a float `1.0` comes back as `1`). `cached` is False when this call
    paid for the backend. `meta` is the entry's metadata (spec §5.4)."""

    reply: Any
    cached: bool
    entry_id: str
    meta: dict[str, Any]
    info: CallInfo = field(repr=False)
    error: dict[str, Any] | None = field(default=None, repr=False)
    """The stored error, only for a hit on an error entry (seen by on_entry_used)."""


def default_recorded_by() -> str:
    """E2: `LLMEMO_RECORDED_BY` if set, else `ci`, `claude` (in Claude Code), or `developer`."""
    if os.environ.get("LLMEMO_RECORDED_BY"):
        return os.environ["LLMEMO_RECORDED_BY"]
    if os.environ.get("CI"):
        return "ci"
    if os.environ.get("CLAUDECODE") == "1":
        return "claude"
    return "developer"


def git_code_version(cwd: str | os.PathLike[str] | None = None) -> str | None:
    """E2: the current commit's short hash, plus `-dirty` if there are uncommitted changes.

    None if `cwd` isn't in a git work tree. Pass the result to `Cache(code_version=...)`.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no") != ""
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit + ("-dirty" if dirty else "")


def _now() -> str:
    t = datetime.now(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"  # X3


def _failure_id() -> str:
    t = datetime.now(UTC)
    return t.strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z-{secrets.token_hex(4)}"


class Cache:
    """A record-and-replay cache for LLM calls.

    `store` is a folder path (a FileStore) or a Store such as MemoryStore. `mode` must be
    given: `"record"` calls the backend on a miss and records the reply (M1), and
    `"replay"` raises CacheMiss instead (M2). The environment variable `LLMEMO_MODE`
    overrides it when set (M5).

    Options:
    - `refresh`: a predicate that selects entries to call again and replace, at most once
      each per Cache (M3, record mode only).
    - `failures`: what to do when the backend raises BackendError (no reply arrived, E5).
      `"discard"` saves nothing; `"save"` writes a failure record for diagnosis;
      `"replay"` writes an error entry, so later lookups raise the same error.
    - `spend_limit`: a number of US dollars, or a SpendLimit shared between caches (M6).
    - `tags`: added to the tags of every call this cache records.
    - `before_record`, `after_record`, `on_entry_used`: hooks (A5). `on_entry_used` sees
      every entry a call returns, hit or recorded, so it can collect hashes for pruning.
    - `ledger`: a Ledger that gets one row per backend call that reports a cost (M7).
    - `log_calls`: True prints one line per hit and miss to stderr; a callable gets the
      lines instead (A7). Otherwise llmemo is quiet, apart from warnings.
    """

    def __init__(
        self,
        store: str | os.PathLike[str] | Store,
        *,
        mode: Mode,
        refresh: Callable[[CallInfo], bool] | None = None,
        failures: Failures = "discard",
        spend_limit: float | SpendLimit | None = None,
        tags: Mapping[str, str] | None = None,
        recorded_by: str | None = None,
        code_version: str | None = None,
        before_record: Callable[[CallInfo], None] | None = None,
        after_record: Callable[[Result], None] | None = None,
        on_entry_used: Callable[[Result], None] | None = None,
        ledger: Ledger | None = None,
        log_calls: bool | Callable[[str], None] = False,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> None:
        env_mode = os.environ.get("LLMEMO_MODE")
        if env_mode:
            if env_mode not in MODES:
                raise ValueError(f"LLMEMO_MODE must be 'record' or 'replay', not {env_mode!r}")
            mode = env_mode
        if mode not in MODES:
            raise ValueError(f"mode must be 'record' or 'replay', not {mode!r}")
        if failures not in ("discard", "save", "replay"):
            raise ValueError(f"failures must be 'discard', 'save', or 'replay', not {failures!r}")
        self.store: Store = store if not isinstance(store, (str, os.PathLike)) else FileStore(store)
        self.mode: Mode = mode
        self.refresh = refresh
        self.failures: Failures = failures
        self.spend_limit = (
            SpendLimit(spend_limit) if isinstance(spend_limit, (int, float)) else spend_limit
        )
        self.tags = dict(tags or {})
        self.recorded_by = recorded_by if recorded_by is not None else default_recorded_by()
        self.code_version = code_version
        self.before_record = before_record
        self.after_record = after_record
        self.on_entry_used = on_entry_used
        self.ledger = ledger
        self.max_entry_bytes = max_entry_bytes
        self.max_blob_bytes = max_blob_bytes
        self.stats = Stats()
        if log_calls is True:
            self._log: Callable[[str], None] | None = lambda line: print(line, file=sys.stderr)
        else:
            self._log = log_calls or None
        self._mutex = threading.Lock()
        self._opened = False
        self._inflight: dict[str, Future[None]] = {}
        self._refreshed: set[str] = set()
        if refresh is not None and mode == "replay":
            log.warning("llmemo: refresh is ignored in replay mode")

    def __repr__(self) -> str:
        return f"Cache({self.store!r}, mode={self.mode!r})"

    # Public API -------------------------------------------------------------------------

    def call(
        self,
        request: object,
        fn: Callable[[Any], object],
        *,
        label: str | None = None,
        variant: str | None = None,
        attempt: int = 0,
        tags: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Result:
        """Return the cached reply for this key, or call `fn(request)` and record it (A2).

        The key is `request` (any JSON value: every input that affects the reply, K2),
        with `label`, `variant`, and `attempt` (K3-K5). `tags` and `extra` are stored as
        metadata only (K6). `fn` returns a Reply or a bare JSON value; to have a failure
        saved, it raises BackendError.
        """
        info, key = self._prepare(request, label, variant, attempt, tags, extra)
        while True:
            refresh = self._wants_refresh(info)
            if not refresh and (hit := self._hit(info, key)) is not None:
                return hit
            waiting = self._claim(info.entry_id)
            if waiting is not None:
                waiting.result()  # another thread is recording it; look again when it's done
                continue
            try:
                while not self.store.try_lock(info.entry_id):
                    if not refresh and (hit := self._hit(info, key)) is not None:
                        return hit
                    time.sleep(self._poll_s())
                try:
                    refresh = self._wants_refresh(info)
                    if not refresh and (hit := self._hit(info, key)) is not None:
                        return hit
                    started = self._before_call(info)
                    try:
                        out = fn(info.request)
                    except BackendError as e:
                        self._save_failure(info, key, e, started, replace=refresh)
                        raise
                    return self._finish(info, key, out, started, replace=refresh)
                finally:
                    self.store.unlock(info.entry_id)
            finally:
                self._release(info.entry_id)

    async def acall(
        self,
        request: object,
        fn: Callable[[Any], Awaitable[object]],
        *,
        label: str | None = None,
        variant: str | None = None,
        attempt: int = 0,
        tags: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Result:
        """The async form of `call`, for an async backend. Coroutines and threads that need
        the same missing entry wait for one recorder (S6)."""
        info, key = self._prepare(request, label, variant, attempt, tags, extra)
        while True:
            refresh = self._wants_refresh(info)
            if not refresh and (hit := self._hit(info, key)) is not None:
                return hit
            waiting = self._claim(info.entry_id)
            if waiting is not None:
                await asyncio.wrap_future(waiting)
                continue
            try:
                while not self.store.try_lock(info.entry_id):
                    if not refresh and (hit := self._hit(info, key)) is not None:
                        return hit
                    await asyncio.sleep(self._poll_s())
                try:
                    refresh = self._wants_refresh(info)
                    if not refresh and (hit := self._hit(info, key)) is not None:
                        return hit
                    started = self._before_call(info)
                    try:
                        out = await fn(info.request)
                    except BackendError as e:
                        self._save_failure(info, key, e, started, replace=refresh)
                        raise
                    return self._finish(info, key, out, started, replace=refresh)
                finally:
                    self.store.unlock(info.entry_id)
            finally:
                self._release(info.entry_id)

    def contains(
        self,
        request: object,
        *,
        label: str | None = None,
        variant: str | None = None,
        attempt: int = 0,
    ) -> bool:
        """Whether an entry exists for this key, without reading it, calling the backend,
        or changing the stats (A9)."""
        env = make_key(request, attempt=attempt, label=label, variant=variant)
        self._open()
        return self.store.exists(f"{entry_id_of(env)}.json")

    def entry_id(
        self,
        request: object,
        *,
        label: str | None = None,
        variant: str | None = None,
        attempt: int = 0,
    ) -> str:
        """The entry ID (SHA-256 hex) for a key (K1)."""
        return entry_id_of(make_key(request, attempt=attempt, label=label, variant=variant))

    # Steps ------------------------------------------------------------------------------

    def _prepare(
        self,
        request: object,
        label: str | None,
        variant: str | None,
        attempt: int,
        tags: Mapping[str, str] | None,
        extra: Mapping[str, Any] | None,
    ) -> tuple[CallInfo, dict[str, Any]]:
        key = make_key(request, attempt=attempt, label=label, variant=variant)
        merged = {**self.tags, **(tags or {})}
        for name, value in merged.items():
            if type(name) is not str or type(value) is not str:
                raise TypeError(f"tags must map strings to strings, not {name!r}: {value!r}")
        self._open()
        info = CallInfo(entry_id_of(key), request, label, variant, key["attempt"], merged, extra)
        return info, key

    def _open(self) -> None:
        if self._opened:
            return
        with self._mutex:
            if not self._opened:
                self.store.open(create=self.mode == "record")
                self._opened = True

    def _poll_s(self) -> float:
        return getattr(self.store, "poll_s", 0.05)

    def _wants_refresh(self, info: CallInfo) -> bool:
        if self.refresh is None or self.mode != "record":
            return False
        with self._mutex:
            if info.entry_id in self._refreshed:
                return False
        return self.refresh(info)

    def _claim(self, entry_id: str) -> Future[None] | None:
        """Claim an entry within this process. Returns another recorder's Future to wait
        on, or None if this caller now records it."""
        with self._mutex:
            if entry_id in self._inflight:
                return self._inflight[entry_id]
            self._inflight[entry_id] = Future()
            return None

    def _release(self, entry_id: str) -> None:
        with self._mutex:
            self._inflight.pop(entry_id).set_result(None)

    def _load_blob(self, name: str) -> bytes:
        data = self.store.read(name, self.max_blob_bytes)
        if data is None:
            raise FormatError("blob-missing", f"{name} doesn't exist")
        if sha256_hex(data) != name.split(".", 1)[0]:
            raise FormatError("blob-mismatch", f"{name} doesn't match its hash")
        return data

    def _hit(self, info: CallInfo, key: dict[str, Any]) -> Result | None:
        """Look the key up (spec §8). None on a miss; raises CacheMiss in replay mode."""
        data = self.store.read(f"{info.entry_id}.json", self.max_entry_bytes)
        if data is None:
            if self.mode == "replay":
                missing = None
                if isinstance(self.store, FileStore) and not self.store.root.exists():
                    missing = str(self.store.root)  # spec §7.1: nothing recorded yet
                raise CacheMiss(
                    info.entry_id, info.label, info.variant, info.attempt, missing_folder=missing
                )
            return None
        try:
            stored = parse_entry(data)
            check_key_matches(stored, key, self._load_blob)
            content = logical_content(stored, self._load_blob)
        except FormatError as e:
            e.add_note(f"llmemo entry {info.entry_id} in {self.store!r}")
            raise
        if stored.content_name == "error":
            result = Result(None, True, info.entry_id, stored.meta, info, content)
        else:
            result = Result(content, True, info.entry_id, stored.meta, info)
        self.stats.add(info.label, stored.meta, cached=True)
        self._log_line("hit", result)
        if self.on_entry_used:
            self.on_entry_used(result)
        if result.error is not None:
            raise ReplayedError(info.entry_id, result.error)
        return result

    def _before_call(self, info: CallInfo) -> float:
        if self.spend_limit is not None:
            self.spend_limit.check()
        if self.before_record:
            self.before_record(info)
        return time.monotonic()

    def _meta(self, info: CallInfo, started: float, reply: Reply | None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "recordedAt": _now(),
            "recordedBy": self.recorded_by,
            "latencyMs": round((time.monotonic() - started) * 1000),
        }
        if reply is not None:
            usage = reply.usage.to_json() if reply.usage else {}
            optional = {
                "usage": usage or None,
                "costUsd": reply.cost_usd,
                "servedModel": reply.served_model,
                "servedBy": reply.served_by,
                "toolVersion": reply.tool_version,
                "billing": reply.billing,
            }
            meta.update({k: v for k, v in optional.items() if v is not None})
        if self.code_version is not None:
            meta["codeVersion"] = self.code_version
        if info.tags:
            meta["tags"] = dict(info.tags)
        if info.extra:
            meta["extra"] = dict(info.extra)
        return meta

    def _write(self, built: Built, replace: bool) -> bool:
        """Write the blobs, then the entry (spec §7.2). False if another writer won."""
        for name, data in built.blobs.items():
            self.store.write_blob(name, data)
        name = f"{built.entry_id}.json"
        if replace:
            self.store.replace(name, built.data)
            return True
        return self.store.publish(name, built.data)

    def _finish(
        self, info: CallInfo, key: dict[str, Any], out: object, started: float, replace: bool
    ) -> Result:
        reply = out if isinstance(out, Reply) else Reply(out)
        meta = self._meta(info, started, reply)
        limit = self.spend_limit
        if limit is not None and not limit.charge(reply.cost_usd) and not limit.warned_unknown_cost:
            limit.warned_unknown_cost = True
            log.warning("llmemo: a backend reported no cost, so the spending limit can't count it")
        duplicate = False
        try:
            try:
                built = build_entry(key, meta, reply=reply.value)
            except FormatError as e:
                e.add_note("The backend's reply or metadata isn't valid, so it wasn't recorded.")
                raise
            published = self._write(built, replace)
            duplicate = not published  # another writer recorded it first (S5)
        finally:
            # M7: the call was paid for, whether or not its reply could be recorded.
            self._ledger_row(info, meta, duplicate=duplicate)
        if not published:
            log.warning(
                "llmemo: entry %s was recorded by another writer first; this call's reply was"
                " discarded and the backend was paid twice (S5)",
                built.entry_id,
            )
            stored = parse_entry(
                self.store.read(built.entry_id + ".json", self.max_entry_bytes) or b""
            )
            check_key_matches(stored, key, self._load_blob)
            load: Callable[[str], bytes] = self._load_blob
        else:
            stored = parse_entry(built.data)  # what a replay will return, e.g. 1.0 -> 1
            load = built.blobs.__getitem__
            if replace:
                with self._mutex:
                    self._refreshed.add(info.entry_id)
        content = logical_content(stored, load)
        if stored.content_name == "error":
            raise ReplayedError(info.entry_id, content)
        result = Result(content, False, info.entry_id, stored.meta, info)
        self.stats.add(info.label, meta, cached=False)
        self._log_line("recorded", result)
        if self.after_record:
            self.after_record(result)
        if self.on_entry_used:
            self.on_entry_used(result)
        return result

    def _ledger_row(self, info: CallInfo, meta: dict[str, Any], duplicate: bool) -> None:
        cost = meta.get("costUsd")
        if self.ledger is None or not isinstance(cost, (int, float)):
            return
        self.ledger.record_call(
            entry_id=info.entry_id,
            attempt=info.attempt,
            cost_usd=cost,
            at=meta["recordedAt"],
            by=meta["recordedBy"],
            label=info.label,
            variant=info.variant,
            model=meta.get("servedModel"),
            billing=meta.get("billing"),
            code_version=meta.get("codeVersion"),
            duplicate=duplicate,
        )

    def _save_failure(
        self,
        info: CallInfo,
        key: dict[str, Any],
        error: BackendError,
        started: float,
        replace: bool,
    ) -> None:
        """E5: no reply arrived. Save it only if asked to."""
        self._log_line_info("failed", info, error.kind)
        if self.failures == "discard" or error.kind == "usage-limit":
            return  # a usage limit is about the account, not the request (spec §5.3)
        built = build_entry(key, self._meta(info, started, None), error=error.to_json())
        if self.failures == "save":
            for name, data in built.blobs.items():
                self.store.write_blob(name, data)
            self.store.write_failure(info.entry_id, _failure_id(), built.data)
        elif not self._write(built, replace):
            log.warning(
                "llmemo: entry %s already exists, so the error entry wasn't written", built.entry_id
            )

    def _log_line(self, what: str, result: Result) -> None:
        if self._log:
            cost = result.meta.get("costUsd")
            self._log_line_info(what, result.info, f"${cost:.6f}" if cost is not None else "")

    def _log_line_info(self, what: str, info: CallInfo, detail: str) -> None:
        if self._log:
            parts = [f"llmemo {what}", info.entry_id[:12]]
            parts += [
                f"{n}={v}" for n, v in (("label", info.label), ("variant", info.variant)) if v
            ]
            if info.attempt:
                parts.append(f"attempt={info.attempt}")
            if detail:
                parts.append(detail)
            self._log(" ".join(parts))
