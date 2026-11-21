"""Behavior of the Cache, with fake backends.

The backends here are fakes on purpose: they produce what a real call can't on demand
(exact replies to compare, failures, slow calls, races). Live calls are in test_live.py.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import enum
import logging
import multiprocessing
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from llmemo import (
    BackendError,
    Cache,
    CacheMiss,
    CallInfo,
    FileStore,
    FormatError,
    MemoryStore,
    ReplayedError,
    Reply,
    Result,
    SpendLimit,
    SpendLimitExceeded,
    Stats,
    Usage,
    Vector,
)
from llmemo._entry import make_key


@pytest.fixture(autouse=True)
def _no_env_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLMEMO_MODE", raising=False)


class Counter:
    """A fake backend that counts its calls and echoes the request."""

    def __init__(self, delay: float = 0.0, cost: float | None = 0.01) -> None:
        self.calls = 0
        self.delay = delay
        self.cost = cost
        self._mutex = threading.Lock()

    def __call__(self, request: Any) -> Reply:
        with self._mutex:
            self.calls += 1
        time.sleep(self.delay)
        return Reply(
            {"echo": request},
            usage=Usage(input_tokens=10, output_tokens=2),
            cost_usd=self.cost,
            served_model="fake-1",
        )


REQUEST = {"model": "fake-1", "prompt": "Say hi."}


# Values that aren't JSON (spec §1, the Python list in spec/vectors/README.md) ------------


class Color(enum.Enum):
    RED = "red"


class Shade(enum.StrEnum):
    DARK = "dark"


@pytest.mark.parametrize(
    "bad",
    [
        (1, 2),
        {1, 2},
        b"bytes",
        decimal.Decimal("1.5"),
        datetime.datetime(2026, 11, 20, tzinfo=datetime.UTC),
        Color.RED,
        Shade.DARK,
        {1: "non-string member name"},
        float("nan"),
        float("inf"),
        -0.0,
        2**53,
        1e16,
    ],
    ids=repr,
)
def test_python_values_rejected(bad: object) -> None:
    with pytest.raises(FormatError) as e:
        make_key({"x": [bad]})
    assert e.value.code == "invalid-value"


def test_bool_attempt_rejected() -> None:
    with pytest.raises(FormatError) as e:
        make_key({}, attempt=True)
    assert e.value.code == "invalid-key"


def test_integral_float_attempt_is_an_integer() -> None:
    assert make_key({}, attempt=2.0) == make_key({}, attempt=2)


# Modes (M1, M2, M5) ----------------------------------------------------------------------


def test_record_then_replay(tmp_path: Path) -> None:
    backend = Counter()
    rec = Cache(tmp_path / "c", mode="record")
    first = rec.call(REQUEST, backend, label="greet", tags={"run": "a"}, extra={"timeoutS": 5})
    assert not first.cached
    assert first.reply == {"echo": REQUEST}
    assert first.meta["usage"] == {"inputTokens": 10, "outputTokens": 2}
    assert first.meta["tags"] == {"run": "a"}
    assert first.meta["extra"] == {"timeoutS": 5}
    assert first.meta["servedModel"] == "fake-1"
    second = rec.call(REQUEST, backend, label="greet")
    assert second.cached and second.reply == first.reply
    assert second.meta == first.meta
    rep = Cache(tmp_path / "c", mode="replay").call(REQUEST, backend, label="greet")
    assert rep.cached and rep.reply == first.reply
    assert backend.calls == 1
    assert (tmp_path / "c" / "llmemo.json").is_file()


def test_record_returns_what_replay_returns() -> None:
    """A recorded reply is returned as it reads back, so record and replay runs agree."""
    store = MemoryStore()
    rec = Cache(store, mode="record").call({}, lambda r: {"x": 1.0, "v": Vector([0.1], "f32")})
    rep = Cache(store, mode="replay").call({}, lambda r: None)
    assert rec.reply == rep.reply == {"x": 1, "v": [0.10000000149011612]}
    assert type(rec.reply["x"]) is int


def test_miss_in_replay_names_the_key(tmp_path: Path) -> None:
    Cache(tmp_path / "c", mode="record")  # opening lazily: nothing created yet
    store = MemoryStore()
    with pytest.raises(CacheMiss) as e:
        Cache(store, mode="replay").call(
            REQUEST, Counter(), label="greet", variant="t-1", attempt=2
        )
    msg = str(e.value)
    for part in (
        "label='greet'",
        "variant='t-1'",
        "attempt=2",
        e.value.entry_id,
        "LLMEMO_MODE=record",
    ):
        assert part in msg


def test_replay_needs_an_existing_cache(tmp_path: Path) -> None:
    with pytest.raises(FormatError) as e:
        Cache(tmp_path / "nothing", mode="replay").call(REQUEST, Counter())
    assert e.value.code == "not-a-cache"


def test_record_wont_take_over_a_folder_that_isnt_a_cache(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(FormatError) as e:
        Cache(tmp_path, mode="record").call(REQUEST, Counter())
    assert e.value.code == "not-a-cache"


def test_mode_is_required_and_checked() -> None:
    with pytest.raises(TypeError):
        Cache(MemoryStore())  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Cache(MemoryStore(), mode="read-write")  # type: ignore[arg-type]


def test_env_overrides_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLMEMO_MODE", "replay")
    assert Cache(MemoryStore(), mode="record").mode == "replay"
    monkeypatch.setenv("LLMEMO_MODE", "sometimes")
    with pytest.raises(ValueError):
        Cache(MemoryStore(), mode="record")


def test_key_parts_separate_entries() -> None:
    backend = Counter()
    cache = Cache(MemoryStore(), mode="record")
    ids = {
        cache.call(REQUEST, backend).entry_id,
        cache.call(REQUEST, backend, variant="t-2").entry_id,
        cache.call(REQUEST, backend, attempt=1).entry_id,
        cache.call(REQUEST, backend, label="x").entry_id,
        cache.call({**REQUEST, "temperature": None}, backend).entry_id,  # null isn't absent
    }
    assert len(ids) == backend.calls == 5
    # Metadata isn't part of the key (K6).
    assert cache.call(REQUEST, backend, tags={"t": "1"}, extra={"timeoutS": 1}).cached


def test_contains_changes_nothing() -> None:
    cache = Cache(MemoryStore(), mode="record")
    assert not cache.contains(REQUEST, label="greet")
    cache.call(REQUEST, Counter(), label="greet")
    assert cache.contains(REQUEST, label="greet")
    assert not cache.contains(REQUEST)
    assert cache.stats.total.calls == 1


def test_bare_values_and_large_text(tmp_path: Path) -> None:
    long = "line\n" * 300
    cache = Cache(tmp_path / "c", mode="record")
    result = cache.call({"system": long, "prompt": "hi"}, lambda r: long + "reply")
    assert result.reply == long + "reply"
    texts = list((tmp_path / "c").rglob("*.txt"))
    assert len(texts) == 2  # the system prompt and the reply, each stored once (S4)
    again = Cache(tmp_path / "c", mode="replay").call({"system": long, "prompt": "hi"}, no_call)
    assert again.reply == long + "reply"


def no_call(request: Any) -> Any:
    raise AssertionError("must not call the backend")


def test_invalid_reply_isnt_recorded() -> None:
    cache = Cache(MemoryStore(), mode="record")
    with pytest.raises(FormatError) as e:
        cache.call({}, lambda r: {"when": datetime.date(2026, 1, 1)})
    assert e.value.code == "invalid-value"
    assert not cache.contains({})


def test_damaged_entry_is_an_error_not_a_miss(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "c", mode="record")
    eid = cache.call(REQUEST, lambda r: "fixed").entry_id
    path = tmp_path / "c" / eid[:3] / f"{eid}.json"
    path.write_text(path.read_text().replace('"Say hi."', '"Say bye."'))
    with pytest.raises(FormatError) as e:
        Cache(tmp_path / "c", mode="record").call(REQUEST, no_call)
    assert e.value.code == "key-mismatch"


# Refresh (M3, S7) ------------------------------------------------------------------------


def test_refresh_replaces_selected_entries_once(tmp_path: Path) -> None:
    replies = iter(["old", "new", "unused"])
    first = Cache(tmp_path / "c", mode="record")
    first.call(REQUEST, lambda r: next(replies), label="a")
    first.call(REQUEST, lambda r: "b-old", label="b")
    seen: list[CallInfo] = []

    def select(info: CallInfo) -> bool:
        seen.append(info)
        return info.label == "a"

    cache = Cache(tmp_path / "c", mode="record", refresh=select)
    refreshed = cache.call(REQUEST, lambda r: next(replies), label="a")
    assert refreshed.reply == "new" and not refreshed.cached
    again = cache.call(REQUEST, lambda r: next(replies), label="a")
    assert again.reply == "new" and again.cached  # once per Cache
    assert cache.call(REQUEST, no_call, label="b").reply == "b-old"
    assert Cache(tmp_path / "c", mode="replay").call(REQUEST, no_call, label="a").reply == "new"
    assert {i.label for i in seen} == {"a", "b"}
    assert not list((tmp_path / "c").rglob("*.tmp"))


# Failures (E5) ---------------------------------------------------------------------------


def failing(request: Any) -> Any:
    raise BackendError("http", "503 Service Unavailable\nupstream", http_status=503)


def test_failure_discarded_by_default() -> None:
    store = MemoryStore()
    with pytest.raises(BackendError):
        Cache(store, mode="record").call(REQUEST, failing)
    assert store.files() == {}


def test_failure_saved_for_diagnosis(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "c", mode="record", failures="save")
    for _ in range(2):
        with pytest.raises(BackendError):
            cache.call(REQUEST, failing)
    records = list((tmp_path / "c").rglob("*.failure.json"))
    assert len(records) == 2
    assert not cache.contains(REQUEST)  # failure records don't affect lookups
    assert cache.call(REQUEST, lambda r: "ok").reply == "ok"


def test_failure_replayed_on_purpose() -> None:
    store = MemoryStore()
    with pytest.raises(BackendError):
        Cache(store, mode="record", failures="replay").call(REQUEST, failing)
    used: list[Result] = []
    with pytest.raises(ReplayedError) as e:
        Cache(store, mode="replay", on_entry_used=used.append).call(REQUEST, no_call)
    assert e.value.http_status == 503 and e.value.kind == "http"
    assert e.value.message == "503 Service Unavailable\nupstream"
    assert len(used) == 1 and used[0].error is not None


def test_other_exceptions_arent_saved() -> None:
    store = MemoryStore()

    def buggy(request: Any) -> Any:
        raise KeyError("bug")

    with pytest.raises(KeyError):
        Cache(store, mode="record", failures="save").call(REQUEST, buggy)
    assert store.files() == {}


# Stats, hooks, and the spending limit (A4, A5, M6) ---------------------------------------


def test_stats() -> None:
    store = MemoryStore()
    cache = Cache(store, mode="record")
    cache.call(REQUEST, Counter(cost=0.25), label="a")
    cache.call(REQUEST, Counter(cost=0.25), label="a")
    cache.call(REQUEST, Counter(cost=0.5), label="b")
    t = cache.stats.total
    assert (t.hits, t.misses) == (1, 2)
    assert t.uncached_cost_usd == 0.75 and t.cached_cost_usd == 0.25
    assert t.cost_without_cache_usd == 1.0
    assert t.uncached_tokens == {"inputTokens": 20, "outputTokens": 4}
    by_label = cache.stats.by_label()
    assert by_label["a"].hits == 1 and by_label["b"].misses == 1
    assert cache.stats.by_model()["fake-1"].calls == 3
    combined = Stats.from_json(cache.stats.to_json()) + cache.stats
    assert combined.total.calls == 6
    cache.stats.reset()
    assert cache.stats.total.calls == 0


def test_hooks_order() -> None:
    events: list[str] = []
    cache = Cache(
        MemoryStore(),
        mode="record",
        before_record=lambda info: events.append(f"before {info.label}"),
        after_record=lambda r: events.append(f"after {r.cached}"),
        on_entry_used=lambda r: events.append(f"used {r.cached}"),
    )
    cache.call(REQUEST, Counter(), label="a")
    cache.call(REQUEST, Counter(), label="a")
    assert events == ["before a", "after False", "used False", "used True"]


def test_spend_limit_stops_calls() -> None:
    limit = SpendLimit(0.02)
    a = Cache(MemoryStore(), mode="record", spend_limit=limit)
    b = Cache(MemoryStore(), mode="record", spend_limit=limit)
    a.call({"n": 1}, Counter(cost=0.01))
    b.call({"n": 2}, Counter(cost=0.01))
    with pytest.raises(SpendLimitExceeded):
        a.call({"n": 3}, Counter(cost=0.01))
    assert a.call({"n": 1}, no_call).cached  # hits are free
    assert limit.spent_usd == pytest.approx(0.02)


def test_unknown_cost_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    cache = Cache(MemoryStore(), mode="record", spend_limit=1.0)
    with caplog.at_level(logging.WARNING, "llmemo"):
        cache.call({"n": 1}, lambda r: "free?")
        cache.call({"n": 2}, lambda r: "free?")
    assert sum("reported no cost" in m for m in caplog.messages) == 1


def test_log_lines() -> None:
    lines: list[str] = []
    cache = Cache(MemoryStore(), mode="record", log_calls=lines.append)
    cache.call(REQUEST, Counter(), label="greet")
    cache.call(REQUEST, Counter(), label="greet")
    assert [line.split()[1] for line in lines] == ["recorded", "hit"]
    assert all("label=greet" in line for line in lines)


# Parallel recording (S5, S6) -------------------------------------------------------------


def test_threads_pay_once(tmp_path: Path) -> None:
    backend = Counter(delay=0.2)
    cache = Cache(tmp_path / "c", mode="record")
    results: list[Result] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.call(REQUEST, backend)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert backend.calls == 1
    assert sum(not r.cached for r in results) == 1
    assert not list((tmp_path / "c").rglob("*.lock"))


def test_coroutines_pay_once() -> None:
    calls = 0

    async def backend(request: Any) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return "hi"

    async def main() -> list[Result]:
        cache = Cache(MemoryStore(), mode="record")
        return await asyncio.gather(*(cache.acall(REQUEST, backend) for _ in range(8)))

    results = asyncio.run(main())
    assert calls == 1 and [r.reply for r in results] == ["hi"] * 8


def test_sync_call_inside_a_running_loop() -> None:
    """A3: the sync API works where an event loop is already running, as in Jupyter."""

    async def main() -> Result:
        return Cache(MemoryStore(), mode="record").call(REQUEST, Counter())

    assert asyncio.run(main()).reply == {"echo": REQUEST}


def _record_in_process(root: str, log_path: str) -> None:
    def backend(request: Any) -> str:
        with open(log_path, "a") as f:
            f.write("called\n")
        time.sleep(0.5)
        return "hi"

    store = FileStore(root, poll_s=0.05)
    assert Cache(store, mode="record").call(REQUEST, backend).reply == "hi"


def test_processes_pay_once(tmp_path: Path) -> None:
    root, log_path = str(tmp_path / "c"), str(tmp_path / "calls.log")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_record_in_process, args=(root, log_path)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    assert [p.exitcode for p in procs] == [0] * 4
    assert Path(log_path).read_text() == "called\n"
    assert not list(Path(root).rglob("*.lock")) and not list(Path(root).rglob("*.tmp"))


def test_lost_race_warns_and_returns_the_winner(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "c"

    def backend(request: Any) -> str:
        # Another writer that ignores the lock publishes the same entry meanwhile.
        winner = Cache(FileStore(root), mode="record")
        info, key = winner._prepare(REQUEST, None, None, 0, None, None)
        winner._finish(info, key, "winner", time.monotonic(), replace=False)
        return "loser"

    with caplog.at_level(logging.WARNING, "llmemo"):
        result = Cache(FileStore(root), mode="record").call(REQUEST, backend)
    assert result.reply == "winner" and not result.cached
    assert any("paid twice" in m for m in caplog.messages)


# Locks (spec §7.3) -----------------------------------------------------------------------


def test_lock_is_refreshed_during_a_long_call(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "c", lock_refresh_s=0.1, lock_stale_s=0.5)
    cache = Cache(store, mode="record")
    eid = cache.entry_id(REQUEST)
    lock = store.path(f"{eid}.lock")
    mtimes: list[float] = []

    def slow(request: Any) -> str:
        for _ in range(4):
            mtimes.append(lock.stat().st_mtime)
            time.sleep(0.15)
        return "done"

    cache.call(REQUEST, slow)
    assert mtimes[-1] > mtimes[0]
    assert not lock.exists()


def test_abandoned_lock_is_taken_over(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "c", lock_refresh_s=0.1, lock_stale_s=0.5, poll_s=0.05)
    cache = Cache(store, mode="record")
    cache._open()
    eid = cache.entry_id(REQUEST)
    lock = store.path(f"{eid}.lock")
    lock.parent.mkdir(exist_ok=True)
    lock.write_text("{}")  # a crashed process's lock
    old = time.time() - 10
    os.utime(lock, (old, old))
    assert cache.call(REQUEST, lambda r: "ok").reply == "ok"
    assert not lock.exists()


def test_waits_for_a_live_lock(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "c", poll_s=0.05)
    holder = Cache(store, mode="record")
    eid = holder.entry_id(REQUEST)
    holder._open()
    assert store.try_lock(eid)  # as if another process were recording it

    def finish() -> None:
        time.sleep(0.3)
        info, key = holder._prepare(REQUEST, None, None, 0, None, None)
        holder._finish(info, key, "from the holder", time.monotonic(), replace=False)
        store.unlock(eid)

    t = threading.Thread(target=finish)
    t.start()
    waiter = Cache(FileStore(tmp_path / "c", poll_s=0.05), mode="record")
    result = waiter.call(REQUEST, no_call)
    t.join()
    assert result.reply == "from the holder" and result.cached


def test_cleanup_removes_old_temp_files(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "c")
    Cache(store, mode="record").call(REQUEST, Counter())
    folder = next(p for p in (tmp_path / "c").iterdir() if p.is_dir())
    old_tmp, new_tmp = folder / "a.0000000000000000.tmp", folder / "b.0000000000000000.tmp"
    old_tmp.write_text("")
    new_tmp.write_text("")
    old = time.time() - 7200
    os.utime(old_tmp, (old, old))
    assert store.cleanup() == 1
    assert new_tmp.exists() and not old_tmp.exists()


def test_memory_layer(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "c", memory=True)
    cache = Cache(store, mode="record")
    cache.call(REQUEST, Counter())
    for p in (tmp_path / "c").rglob("*.json"):
        if p.name != "llmemo.json":
            p.unlink()
    assert cache.call(REQUEST, no_call).cached


def test_stale_must_exceed_refresh() -> None:
    with pytest.raises(ValueError):
        FileStore("x", lock_refresh_s=10, lock_stale_s=15)
