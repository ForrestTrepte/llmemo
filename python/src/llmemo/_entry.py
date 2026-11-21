"""Key envelopes (spec §3), entries (§5), and reading them back (§8)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ._errors import FormatError
from ._json import MAX_SAFE_INTEGER, canonical_bytes, json_file_bytes, parse_json
from ._stored import HEX64, LoadBlob, Where, check_refs, from_stored, sha256_hex, to_stored

KEY_FORMAT = 1
ENTRY_FORMAT = 1
LAYOUT_FORMAT = 1

DEFAULT_MAX_ENTRY_BYTES = 1024 * 1024  # §8
DEFAULT_MAX_BLOB_BYTES = 16 * 1024 * 1024  # §8

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
FAILURE_ID = re.compile(r"\d{8}T\d{9}Z-[0-9a-f]{8}")
USAGE_NAMES = (
    "inputTokens",
    "outputTokens",
    "reasoningTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
)
_META_STRINGS = ("recordedBy", "servedModel", "servedBy", "toolVersion", "codeVersion", "billing")
_ERROR_MEMBERS = frozenset({"kind", "message", "httpStatus", "exitCode", "details"})


# §3 Keys ---------------------------------------------------------------------------------


def make_key(
    request: object, *, attempt: object = 0, label: object = None, variant: object = None
) -> dict[str, Any]:
    """Build and check a key envelope (§3.1). `label` and `variant` are absent when None."""
    if type(attempt) is float and attempt.is_integer():
        attempt = int(attempt)  # §1: the value counts, not the type
    if type(attempt) is not int or not 0 <= attempt <= MAX_SAFE_INTEGER:
        raise FormatError(
            "invalid-key", f"attempt must be an integer from 0 to 2^53 - 1, not {attempt!r}"
        )
    env: dict[str, Any] = {"keyFormat": KEY_FORMAT, "attempt": attempt, "request": request}
    for name, part in (("label", label), ("variant", variant)):
        if part is None:
            continue
        if type(part) is not str or part == "":
            raise FormatError("invalid-key", f"{name} must be a non-empty string, not {part!r}")
        env[name] = part
    canonical_bytes(env)  # raises invalid-value for anything §1 forbids
    return env


def entry_id_of(env: dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(env))


def _check_stored_key(env: object) -> None:
    if not isinstance(env, dict):
        raise FormatError("schema", "key isn't an object")
    names = env.keys()
    if (
        not {"keyFormat", "attempt", "request"}
        <= names
        <= {
            "keyFormat",
            "attempt",
            "request",
            "label",
            "variant",
        }
    ):
        raise FormatError("schema", f"key members {sorted(names)}")
    if type(env["keyFormat"]) is not int or env["keyFormat"] != KEY_FORMAT:
        raise FormatError("schema", f"keyFormat {env['keyFormat']!r}")
    if type(env["attempt"]) is not int or env["attempt"] < 0:
        raise FormatError("schema", "key attempt")
    for name in ("label", "variant"):
        if name in env and (not isinstance(env[name], (str, dict)) or env[name] == ""):
            raise FormatError("schema", f"key {name}")  # a dict may be a text reference


# §5 Entries ------------------------------------------------------------------------------


def _nonneg_int(v: object) -> bool:
    return type(v) is int and v >= 0


def check_meta(meta: object) -> None:
    """§5.4. Unknown members are ignored, so metadata can grow without a format change."""
    if not isinstance(meta, dict):
        raise FormatError("schema", "meta isn't an object")
    at = meta.get("recordedAt")
    if not isinstance(at, str) or not TIMESTAMP.fullmatch(at):
        raise FormatError("schema", f"meta.recordedAt {at!r}")
    for name in _META_STRINGS:
        if name in meta and not isinstance(meta[name], str):
            raise FormatError("schema", f"meta.{name}")
    if "latencyMs" in meta and not _nonneg_int(meta["latencyMs"]):
        raise FormatError("schema", "meta.latencyMs must be a non-negative integer")
    if "costUsd" in meta:
        c = meta["costUsd"]
        if type(c) not in (int, float) or c < 0:
            raise FormatError("schema", "meta.costUsd must be a non-negative number")
    if "usage" in meta:
        usage = meta["usage"]
        if not isinstance(usage, dict):
            raise FormatError("schema", "meta.usage")
        for name in USAGE_NAMES:
            if name in usage and not _nonneg_int(usage[name]):
                raise FormatError("schema", f"meta.usage.{name} must be a non-negative integer")
    if "tags" in meta:
        tags = meta["tags"]
        if not isinstance(tags, dict) or not all(isinstance(t, str) for t in tags.values()):
            raise FormatError("schema", "meta.tags must be an object of strings")
    if "extra" in meta and not isinstance(meta["extra"], dict):
        raise FormatError("schema", "meta.extra must be an object")


def check_error(error: object) -> None:
    """The logical error object of §5.3."""
    if not isinstance(error, dict) or not {"kind", "message"} <= error.keys() <= _ERROR_MEMBERS:
        names = sorted(error) if isinstance(error, dict) else type(error).__name__
        raise FormatError("schema", f"error members {names}")
    if not isinstance(error["kind"], str) or not isinstance(error["message"], str):
        raise FormatError("schema", "error.kind and error.message must be strings")
    for name in ("httpStatus", "exitCode"):
        if name in error and type(error[name]) is not int:
            raise FormatError("schema", f"error.{name} must be an integer")


@dataclass
class Built:
    """An entry ready to write: its file bytes, and the text and vector files it needs."""

    entry_id: str
    data: bytes
    blobs: dict[str, bytes]


def build_entry(
    env: dict[str, Any],
    meta: dict[str, Any],
    *,
    reply: object = None,
    error: dict[str, Any] | None = None,
) -> Built:
    """The bytes of an entry file (or failure record) and its blobs. One of reply or error."""
    check_meta(meta)
    blobs: dict[str, bytes] = {}
    entry: dict[str, Any] = {
        "entryFormat": ENTRY_FORMAT,
        "key": to_stored(env, "key", blobs),
        "meta": meta,
    }
    name: Where = "reply"
    if error is not None:
        check_error(error)
        name = "error"
    content = to_stored(error if error is not None else reply, name, blobs)
    entry[name] = content
    entry["contentSha256"] = sha256_hex(canonical_bytes(content))
    return Built(entry_id_of(env), json_file_bytes(entry), blobs)


@dataclass
class Stored:
    """A checked entry, still in stored form (§8 steps 2-5)."""

    key: dict[str, Any]
    content_name: Where  # "reply" or "error"
    content: Any
    meta: dict[str, Any]


def parse_entry(data: bytes) -> Stored:
    """§8 steps 3-5: parse an entry file and check its schema and content hash."""
    entry = parse_json(data)
    if not isinstance(entry, dict):
        raise FormatError("schema", "entry isn't an object")
    if "entryFormat" not in entry:
        raise FormatError("schema", "no entryFormat")
    fmt = entry["entryFormat"]
    if type(fmt) is not int or fmt != ENTRY_FORMAT:
        raise FormatError("unsupported-format", f"entryFormat {fmt!r}")
    has_reply, has_error = "reply" in entry, "error" in entry
    if has_reply == has_error:
        raise FormatError("schema", "an entry needs exactly one of reply and error")
    name: Where = "reply" if has_reply else "error"
    if entry.keys() != {"entryFormat", "key", "contentSha256", "meta", name}:
        raise FormatError("schema", f"entry members {sorted(entry)}")
    digest = entry["contentSha256"]
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise FormatError("schema", "contentSha256")
    check_meta(entry["meta"])
    _check_stored_key(entry["key"])
    check_refs(entry["key"], "key")
    check_refs(entry[name], name)
    if sha256_hex(canonical_bytes(entry[name])) != digest:
        raise FormatError(
            "content-mismatch", "the stored reply or error doesn't match contentSha256"
        )
    return Stored(entry["key"], name, entry[name], entry["meta"])


def check_key_matches(stored: Stored, requested: dict[str, Any], load: LoadBlob) -> None:
    """§8 step 6 (K10). Compares stored forms first, which needs no text files."""
    if canonical_bytes(to_stored(requested, "key", {})) == canonical_bytes(stored.key):
        return
    # A reader must accept other stored forms (e.g. short text in a file), so compare the
    # logical keys too before calling it a mismatch.
    logical = from_stored(stored.key, "key", load)
    if canonical_bytes(logical) != canonical_bytes(requested):
        raise FormatError("key-mismatch", "the stored key isn't the requested key")


def logical_content(stored: Stored, load: LoadBlob) -> Any:
    """§8 step 7: the reply or error, with references replaced by their content."""
    content = from_stored(stored.content, stored.content_name, load)
    if stored.content_name == "error":
        check_error(content)
    return content


def logical_key(stored: Stored, load: LoadBlob) -> dict[str, Any]:
    key = from_stored(stored.key, "key", load)
    assert isinstance(key, dict)
    return key
