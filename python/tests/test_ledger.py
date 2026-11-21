"""The spending ledger (M7, spec §10): the shared vectors in spec/vectors/ledger.json, and
how the Cache writes rows."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from llmemo import (
    Cache,
    FileStore,
    FormatError,
    Ledger,
    MemoryStore,
    Reply,
    read_ledgers,
    summarize_spending,
)
from llmemo._json import canonical_bytes
from llmemo._ledger import GITATTRIBUTES, check_row, ledger_relpath, parse_ledger

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "vectors" / "ledger.json").read_text("utf-8")
)


def ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in cases]


# Vectors ---------------------------------------------------------------------------------


def test_gitattributes() -> None:
    assert VECTORS["gitattributes"].encode() == GITATTRIBUTES


@pytest.mark.parametrize("case", VECTORS["paths"], ids=ids(VECTORS["paths"]))
def test_path(case: dict[str, Any]) -> None:
    assert ledger_relpath(case["branch"], case["developer"]) == case["path"]


@pytest.mark.parametrize("case", VECTORS["rows"], ids=ids(VECTORS["rows"]))
def test_row_line(case: dict[str, Any]) -> None:
    assert check_row(case["row"])
    assert canonical_bytes(case["row"]) + b"\n" == case["line"].encode()


def test_append_writes_the_vector_bytes(tmp_path: Path) -> None:
    row = next(c for c in VECTORS["rows"] if c["name"] == "call-full")
    ledger = Ledger(
        tmp_path / "spend",
        branch="feature/x",
        developer="dev",
        session=row["row"]["session"],
        run=row["row"]["run"],
    )
    ledger.append({k: v for k, v in row["row"].items() if k != "ledgerFormat"})
    assert ledger.path == tmp_path / "spend" / "feature" / "x" / "dev.jsonl"
    assert ledger.path.read_bytes() == row["line"].encode()
    assert (tmp_path / "spend" / ".gitattributes").read_bytes() == GITATTRIBUTES


@pytest.mark.parametrize("case", VECTORS["read"], ids=ids(VECTORS["read"]))
def test_read(case: dict[str, Any]) -> None:
    rows = parse_ledger(case["text"].encode())
    assert canonical_bytes(rows) == canonical_bytes(case["expectedRows"])


@pytest.mark.parametrize("case", VECTORS["readErrors"], ids=ids(VECTORS["readErrors"]))
def test_read_error(case: dict[str, Any]) -> None:
    with pytest.raises(FormatError) as e:
        parse_ledger(case["text"].encode())
    assert e.value.code == case["expectedErrorCode"]


# Writing ---------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_defaults_come_from_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_USER", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    git(tmp_path, "init", "-q", "-b", "feature/ledger")
    git(tmp_path, "config", "user.name", "Ada Lovelace")
    monkeypatch.chdir(tmp_path)
    ledger = Ledger()
    assert ledger.path == tmp_path / "llm-spend" / "feature" / "ledger" / "Ada-Lovelace.jsonl"
    assert ledger.session is None
    monkeypatch.setenv("GITHUB_USER", "ada")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-1")
    ledger = Ledger()
    assert ledger.path.name == "ada.jsonl" and ledger.session == "s-1"


def test_parallel_appends_dont_interleave(tmp_path: Path) -> None:
    def write(n: int) -> None:
        ledger = Ledger(tmp_path, branch="main", developer="dev", run=f"run-{n}")
        for i in range(50):
            ledger.record_call(
                entry_id="0" * 64, attempt=i, cost_usd=0.001, at="2026-11-21T00:00:00.000Z", by="ci"
            )

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    [ledger] = read_ledgers(tmp_path)
    assert len(ledger.rows) == 400
    assert (ledger.branch, ledger.developer) == ("main", "dev")


def test_unknown_kind_isnt_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Ledger(tmp_path, branch="b", developer="d").append({"kind": "refund"})


# The Cache writes rows (M7) --------------------------------------------------------------


def priced(cost: float | None, **extra: Any) -> Any:
    return lambda request: Reply({"ok": request}, cost_usd=cost, served_model="m-1", **extra)


def rows(ledger: Ledger) -> list[dict[str, Any]]:
    return parse_ledger(ledger.path.read_bytes()) if ledger.path.exists() else []


def test_cache_writes_a_row_per_paid_call(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "spend", branch="main", developer="dev", session="s")
    cache = Cache(MemoryStore(), mode="record", ledger=ledger, code_version="abc123")
    first = cache.call({"n": 1}, priced(0.5, billing="subscription"), label="a", variant="v")
    cache.call({"n": 1}, priced(0.5), label="a", variant="v")  # a hit: nothing paid
    cache.call({"n": 2}, priced(None))  # no cost reported: nothing to count
    [row] = rows(ledger)
    assert row == {
        "ledgerFormat": 1,
        "kind": "call",
        "at": first.meta["recordedAt"],
        "by": first.meta["recordedBy"],
        "session": "s",
        "run": ledger.run,
        "entryId": first.entry_id,
        "attempt": 0,
        "costUsd": 0.5,
        "label": "a",
        "variant": "v",
        "model": "m-1",
        "billing": "subscription",
        "codeVersion": "abc123",
    }
    assert first.meta["billing"] == "subscription"


def test_row_written_even_when_the_reply_is_invalid(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, branch="main", developer="dev")
    cache = Cache(MemoryStore(), mode="record", ledger=ledger)
    with pytest.raises(FormatError):
        cache.call({}, lambda r: Reply(float("nan"), cost_usd=0.25))
    assert [r["costUsd"] for r in rows(ledger)] == [0.25]


def test_lost_race_is_marked_duplicate(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "spend", branch="main", developer="dev")
    root = tmp_path / "c"

    def backend(request: Any) -> Reply:
        winner = Cache(FileStore(root), mode="record")
        info, key = winner._prepare(request, None, None, 0, None, None)
        winner._finish(info, key, "winner", 0.0, replace=False)
        return Reply("loser", cost_usd=0.1)

    Cache(FileStore(root), mode="record", ledger=ledger).call({}, backend)
    assert [r.get("duplicate") for r in rows(ledger)] == [True]


def test_summarize_spending(tmp_path: Path) -> None:
    for branch, developer, costs in (("main", "a", [1, 2]), ("main", "b", [4]), ("x", "a", [8])):
        ledger = Ledger(tmp_path, branch=branch, developer=developer)
        for c in costs:
            ledger.record_call(
                entry_id="1" * 64, attempt=0, cost_usd=c, at="2026-11-21T00:00:00.000Z", by="ci"
            )
    ledger.record_key_total("openrouter", 99.0, limit_usd=100, source="test")
    ledgers = read_ledgers(tmp_path)
    by_branch = summarize_spending(ledgers, by=("branch",))
    assert {k: (v.calls, v.cost_usd) for k, v in by_branch.items()} == {
        ("main",): (3, 7),
        ("x",): (1, 8),
    }
    assert summarize_spending(ledgers, by=("by", "billing"))[("ci", None)].cost_usd == 15
    assert any(r["kind"] == "key-total" for f in ledgers for r in f.rows)
