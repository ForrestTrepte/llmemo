"""Every case in spec/vectors/ (X2). See spec/vectors/README.md for what each test checks.

These don't import spec/tools/reference.py: the vectors are the oracle, and the package
must agree with them on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llmemo import Cache, CacheMiss, FileStore, FormatError, ReplayedError, Vector
from llmemo._entry import build_entry, logical_content, make_key, parse_entry
from llmemo._json import canonical_bytes

VECTORS = Path(__file__).resolve().parents[2] / "spec" / "vectors"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def case_dirs(group: str) -> list[Path]:
    return sorted(p for p in (VECTORS / group).iterdir() if p.is_dir())


def same(a: object, b: object) -> bool:
    """Equal as JSON values (vectors README: compare by canonical form)."""
    return canonical_bytes(a) == canonical_bytes(b)


def key_args(key: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": key.get("attempt", 0),
        "label": key.get("label"),
        "variant": key.get("variant"),
    }


def unmark(v: Any) -> Any:
    """Turn the vectors' `{"$test.vector": ...}` notation into llmemo's Vector."""
    if isinstance(v, list):
        return [unmark(x) for x in v]
    if isinstance(v, dict):
        if v.keys() == {"$test.vector"}:
            spec = v["$test.vector"]
            return Vector(spec["values"], dtype=spec["dtype"])
        return {k: unmark(x) for k, x in v.items()}
    return v


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def no_call(request: Any) -> Any:
    raise AssertionError("replay must not call the backend")


# keys.json and invalid-keys.json ---------------------------------------------------------

KEYS = load(VECTORS / "keys.json")
INVALID_KEYS = load(VECTORS / "invalid-keys.json")


@pytest.mark.parametrize("case", KEYS, ids=[c["name"] for c in KEYS])
def test_key(case: dict[str, Any]) -> None:
    inp = case["input"]
    env = make_key(inp["request"], **key_args(inp))
    assert canonical_bytes(env) == case["canonical"].encode("utf-8")
    cache = Cache(FileStore("unused"), mode="replay")
    assert cache.entry_id(inp["request"], **key_args(inp)) == case["sha256"]


@pytest.mark.parametrize("case", INVALID_KEYS, ids=[c["name"] for c in INVALID_KEYS])
def test_invalid_key(case: dict[str, Any]) -> None:
    if "input" in case:
        inp = case["input"]
        request, args = inp["request"], key_args(inp)
    else:
        request, args = json.loads(case["requestJson"]), {}
    with pytest.raises(FormatError) as e:
        make_key(request, **args)
    assert e.value.code == case["error"]


# entries/ --------------------------------------------------------------------------------

ENTRIES = case_dirs("entries")


@pytest.mark.parametrize("folder", ENTRIES, ids=[p.name for p in ENTRIES])
def test_entry_write(folder: Path, tmp_path: Path) -> None:
    """Writing from nothing produces the expected cache tree, byte for byte (S1)."""
    case = load(folder / "case.json")
    store = FileStore(tmp_path / "cache")
    store.open(create=True)
    env = make_key(case["key"]["request"], **key_args(case["key"]))
    if "error" in case:
        built = build_entry(env, case["meta"], error=case["error"])
    else:
        built = build_entry(env, case["meta"], reply=unmark(case["reply"]))
    assert built.entry_id == case["id"]
    for name, data in built.blobs.items():
        store.write_blob(name, data)
    if "failureId" in case:
        store.write_failure(built.entry_id, case["failureId"], built.data)
    else:
        assert store.publish(f"{built.entry_id}.json", built.data)
    assert tree(tmp_path / "cache") == tree(folder / "cache")


@pytest.mark.parametrize("folder", ENTRIES, ids=[p.name for p in ENTRIES])
def test_entry_read(folder: Path) -> None:
    case = load(folder / "case.json")
    cache = Cache(folder / "cache", mode="replay")
    request, args = case["key"]["request"], key_args(case["key"])
    if "failureId" in case:
        # A failure record doesn't affect lookups (spec §5.3).
        with pytest.raises(CacheMiss):
            cache.call(request, no_call, **args)
        path = folder / "cache" / case["id"][:3] / f"{case['id']}.{case['failureId']}.failure.json"
        stored = parse_entry(path.read_bytes())
        assert same(logical_content(stored, cache._load_blob), case["expectedError"])
    elif "expectedError" in case:
        with pytest.raises(ReplayedError) as e:
            cache.call(request, no_call, **args)
        assert same(e.value.error, case["expectedError"])
        assert e.value.kind == case["expectedError"]["kind"]
    else:
        result = cache.call(request, no_call, **args)
        assert result.cached
        assert result.entry_id == case["id"]
        assert same(result.reply, case["expectedReply"])
        assert same(result.meta, case["meta"])


# read-ok/ and read-errors/ ---------------------------------------------------------------

READ_OK = case_dirs("read-ok")
READ_ERRORS = case_dirs("read-errors")


@pytest.mark.parametrize("folder", READ_OK, ids=[p.name for p in READ_OK])
def test_read_ok(folder: Path) -> None:
    case = load(folder / "case.json")
    cache = Cache(folder / "cache", mode="replay")
    result = cache.call(case["key"]["request"], no_call, **key_args(case["key"]))
    assert result.entry_id == case["id"]
    assert same(result.reply, case["expectedReply"])


@pytest.mark.parametrize("folder", READ_ERRORS, ids=[p.name for p in READ_ERRORS])
def test_read_error(folder: Path) -> None:
    case = load(folder / "case.json")
    limits = case.get("limits", {})
    sizes = {}
    if "entryBytes" in limits:
        sizes["max_entry_bytes"] = limits["entryBytes"]
    if "blobBytes" in limits:
        sizes["max_blob_bytes"] = limits["blobBytes"]
    cache = Cache(folder / "cache", mode="replay", **sizes)
    with pytest.raises(FormatError) as e:
        cache.call(case["key"]["request"], no_call, **key_args(case["key"]))
    assert e.value.code == case["expectedErrorCode"], str(e.value)
