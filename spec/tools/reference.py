"""Reference implementation of the llmemo cache format (spec/format.md), version 1.

This module exists to generate and check the test vectors in spec/vectors/. It's written
for clarity, not speed, and uses only the standard library. It is not the llmemo
package: no locking, no backends, no API. The package must not import it.

Section numbers (§n) refer to spec/format.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEY_FORMAT = 1
ENTRY_FORMAT = 1
LAYOUT_FORMAT = 1

MAX_SAFE_INTEGER = 2**53 - 1
TEXT_INLINE_MAX_BYTES = 512  # §4.4
DEFAULT_ENTRY_LIMIT = 1024 * 1024  # §8
DEFAULT_BLOB_LIMIT = 16 * 1024 * 1024  # §8

HEX64 = re.compile(r"[0-9a-f]{64}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
FAILURE_ID = re.compile(r"\d{8}T\d{9}Z-[0-9a-f]{8}")
NEEDS_ESCAPE = re.compile(r"\$+llmemo")  # §4.3, matched at the start of a name
ESCAPED = re.compile(r"\$\$+llmemo")
RESERVED = re.compile(r"\$llmemo")
REF_KINDS = {"$llmemo.text": "txt", "$llmemo.f64": "f64", "$llmemo.f32": "f32"}

GITATTRIBUTES = (
    "# Written by llmemo. Stored text keeps its exact bytes.\n"
    "*.txt -text\n"
    "*.f32 binary\n"
    "*.f64 binary\n"
    "*.json text eol=lf\n"
)
GITIGNORE = "# Written by llmemo.\n*.lock\n*.tmp\n"


class FormatError(Exception):
    """A violation of the format, with one of the codes in §8."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Vector:
    """An array of numbers the caller marked for binary storage (§4.5)."""

    values: tuple[float, ...]
    dtype: str  # "f64" or "f32"


# §1 JSON values -------------------------------------------------------------------


def check_number(x: object, where: str) -> None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise FormatError("invalid-value", f"{where}: not a number")
    if isinstance(x, float):
        if not math.isfinite(x):
            raise FormatError("invalid-value", f"{where}: not finite")
        if x == 0 and math.copysign(1.0, x) < 0:
            raise FormatError("invalid-value", f"{where}: negative zero")
        if x.is_integer() and abs(x) > MAX_SAFE_INTEGER:
            raise FormatError("invalid-value", f"{where}: integer above 2^53 - 1")
    elif abs(x) > MAX_SAFE_INTEGER:
        raise FormatError("invalid-value", f"{where}: integer above 2^53 - 1")


def check_string(s: str, where: str) -> None:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        raise FormatError("invalid-value", f"{where}: lone surrogate")


def check_value(v: object, where: str = "$") -> None:
    """Raise unless v is a JSON value as §1 defines it."""
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        check_number(v, where)
    elif isinstance(v, str):
        check_string(v, where)
    elif isinstance(v, list):
        for i, item in enumerate(v):
            check_value(item, f"{where}[{i}]")
    elif isinstance(v, dict):
        for k, item in v.items():
            if not isinstance(k, str):
                raise FormatError("invalid-value", f"{where}: member name {k!r} isn't a string")
            check_string(k, where)
            check_value(item, f"{where}.{k}")
    else:
        raise FormatError("invalid-value", f"{where}: {type(v).__name__} isn't a JSON value")


# §2 Canonical JSON ----------------------------------------------------------------


def format_number(x: int | float) -> str:
    """ECMAScript Number::toString for a value check_number accepts."""
    if isinstance(x, int):
        return str(x)
    if x == 0:
        return "0"
    r = repr(x)  # shortest round-trip digits, e.g. '1e-07', '0.30000000000000004'
    sign = ""
    if r.startswith("-"):
        sign, r = "-", r[1:]
    mantissa, _, exp_text = r.partition("e")
    exponent = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    leading_zeros = len(all_digits) - len(all_digits.lstrip("0"))
    digits = all_digits.lstrip("0").rstrip("0")
    k = len(digits)
    n = len(int_part) - leading_zeros + exponent  # value = 0.digits * 10^n
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + s


_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def format_string(s: str) -> str:
    out = ['"']
    for c in s:
        if c in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[c])
        elif ord(c) < 0x20:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def sort_names(obj: dict[str, Any]) -> list[str]:
    return sorted(obj, key=lambda name: name.encode("utf-16-be"))


def canonical(v: object) -> str:
    """RFC 8785 canonical JSON of a checked value."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return format_number(v)
    if isinstance(v, str):
        return format_string(v)
    if isinstance(v, list):
        return "[" + ",".join(canonical(item) for item in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(format_string(k) + ":" + canonical(v[k]) for k in sort_names(v)) + "}"
    raise FormatError("invalid-value", f"{type(v).__name__} isn't a JSON value")


def canonical_bytes(v: object) -> bytes:
    check_value(v)
    return canonical(v).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# §3 Keys --------------------------------------------------------------------------


def make_key(
    request: object, attempt: object = 0, label: object = None, variant: object = None
) -> dict[str, Any]:
    """Build a key envelope (§3.1). label and variant are absent when None."""
    if isinstance(attempt, bool) or not isinstance(attempt, (int, float)):
        raise FormatError("invalid-key", "attempt must be an integer")
    if isinstance(attempt, float):
        if not attempt.is_integer():
            raise FormatError("invalid-key", "attempt must be an integer")
        attempt = int(attempt)
    if not 0 <= attempt <= MAX_SAFE_INTEGER:
        raise FormatError("invalid-key", "attempt must be from 0 to 2^53 - 1")
    env: dict[str, Any] = {"keyFormat": KEY_FORMAT, "attempt": attempt, "request": request}
    for name, part in (("label", label), ("variant", variant)):
        if part is None:
            continue
        if not isinstance(part, str) or part == "":
            raise FormatError("invalid-key", f"{name} must be a non-empty string")
        env[name] = part
    check_value(env)
    return env


def check_key(env: object) -> None:
    """Raise unless env is a well-formed key envelope (used when reading)."""
    if not isinstance(env, dict):
        raise FormatError("schema", "key isn't an object")
    allowed = {"keyFormat", "attempt", "label", "variant", "request"}
    if not {"keyFormat", "attempt", "request"} <= env.keys() <= allowed:
        raise FormatError("schema", f"key members {sorted(env)}")
    if env["keyFormat"] != KEY_FORMAT or isinstance(env["keyFormat"], bool):
        raise FormatError("schema", "keyFormat")


def entry_id(env: dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(env))


# §4 Stored form -------------------------------------------------------------------


def stores_as_text(s: str) -> bool:
    return "\n" in s or len(s.encode("utf-8")) > TEXT_INLINE_MAX_BYTES


def vector_bytes(vec: Vector) -> bytes:
    if not vec.values:
        raise FormatError("invalid-value", "empty vector")
    for x in vec.values:
        check_number(x, "vector")
    if vec.dtype == "f64":
        return struct.pack(f"<{len(vec.values)}d", *vec.values)
    if vec.dtype == "f32":
        try:
            return struct.pack(f"<{len(vec.values)}f", *vec.values)
        except OverflowError as e:
            raise FormatError("invalid-value", "vector value outside float32 range") from e
    raise FormatError("invalid-value", f"unknown vector dtype {vec.dtype!r}")


def to_stored(v: object, blobs: dict[str, bytes], allow_vectors: bool) -> object:
    """Logical value -> stored value (§4.6). Adds the files to write to blobs."""
    if isinstance(v, Vector):
        if not allow_vectors:
            raise FormatError("invalid-value", "vectors are allowed only in the reply")
        data = vector_bytes(v)
        h = sha256_hex(data)
        blobs[f"{h}.{v.dtype}"] = data
        return {f"$llmemo.{v.dtype}": h}
    if isinstance(v, str):
        check_string(v, "string")
        if stores_as_text(v):
            data = v.encode("utf-8")
            h = sha256_hex(data)
            blobs[f"{h}.txt"] = data
            return {"$llmemo.text": h}
        return v
    if isinstance(v, list):
        return [to_stored(item, blobs, allow_vectors) for item in v]
    if isinstance(v, dict):
        out: dict[str, object] = {}
        for k, item in v.items():
            if not isinstance(k, str):
                raise FormatError("invalid-value", f"member name {k!r} isn't a string")
            check_string(k, "member name")
            out["$" + k if NEEDS_ESCAPE.match(k) else k] = to_stored(item, blobs, allow_vectors)
        return out
    check_value(v)
    return v


@dataclass
class Limits:
    entry_bytes: int = DEFAULT_ENTRY_LIMIT
    blob_bytes: int = DEFAULT_BLOB_LIMIT


def read_blob(root: Path, h: str, ext: str, limits: Limits) -> bytes:
    path = root / h[:3] / f"{h}.{ext}"
    if not path.is_file():
        raise FormatError("blob-missing", str(path.relative_to(root)))
    if path.stat().st_size > limits.blob_bytes:
        raise FormatError("too-large", str(path.relative_to(root)))
    data = path.read_bytes()
    if sha256_hex(data) != h:
        raise FormatError("blob-mismatch", str(path.relative_to(root)))
    return data


def decode_blob(data: bytes, ext: str) -> object:
    if ext == "txt":
        try:
            text = data.decode("utf-8")  # strict, and keeps a leading BOM
        except UnicodeDecodeError as e:
            raise FormatError("blob-invalid", "text isn't UTF-8") from e
        return text
    size = 8 if ext == "f64" else 4
    if len(data) == 0 or len(data) % size:
        raise FormatError("blob-invalid", f"vector length {len(data)}")
    values = list(struct.unpack(f"<{len(data) // size}{'d' if ext == 'f64' else 'f'}", data))
    if not all(math.isfinite(x) for x in values):
        raise FormatError("blob-invalid", "vector value isn't finite")
    return values


def from_stored(v: object, where: str, root: Path | None, limits: Limits) -> object:
    """Stored value -> logical value (§4.6). With root=None, only checks the syntax."""
    if isinstance(v, list):
        return [from_stored(item, where, root, limits) for item in v]
    if not isinstance(v, dict):
        return v
    reserved = [k for k in v if RESERVED.match(k)]
    if reserved:
        name = reserved[0]
        if len(v) != 1 or name not in REF_KINDS:
            raise FormatError("schema", f"malformed reference {sorted(v)}")
        ext = REF_KINDS[name]
        if ext != "txt" and where != "reply":
            raise FormatError("schema", f"vector reference in {where}")
        h = v[name]
        if not isinstance(h, str) or not HEX64.fullmatch(h):
            raise FormatError("schema", f"bad reference {h!r}")
        if root is None:
            return v
        return decode_blob(read_blob(root, h, ext, limits), ext)
    return {(k[1:] if ESCAPED.match(k) else k): from_stored(item, where, root, limits) for k, item in v.items()}


# §6 Writing files -----------------------------------------------------------------


def pretty(v: object, indent: str = "") -> str:
    """The JSON file layout of §6.1, without the final newline."""
    inner = indent + "  "
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[\n" + ",\n".join(inner + pretty(item, inner) for item in v) + "\n" + indent + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        members = (inner + format_string(k) + ": " + pretty(v[k], inner) for k in sort_names(v))
        return "{\n" + ",\n".join(members) + "\n" + indent + "}"
    return canonical(v)


def json_file_bytes(v: object) -> bytes:
    check_value(v)
    return (pretty(v) + "\n").encode("utf-8")


def init_cache(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "llmemo.json").write_bytes(json_file_bytes({"layoutFormat": LAYOUT_FORMAT}))
    (root / ".gitattributes").write_bytes(GITATTRIBUTES.encode())
    (root / ".gitignore").write_bytes(GITIGNORE.encode())


def build_entry(
    env: dict[str, Any], meta: dict[str, Any], reply: object = None, error: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The stored entry object and the files it refers to."""
    blobs: dict[str, bytes] = {}
    entry: dict[str, Any] = {"entryFormat": ENTRY_FORMAT, "key": to_stored(env, blobs, False), "meta": meta}
    if error is not None:
        check_error(error)
        entry["error"] = to_stored(error, blobs, False)
        content = entry["error"]
    else:
        entry["reply"] = to_stored(reply, blobs, True)
        content = entry["reply"]
    entry["contentSha256"] = sha256_hex(canonical_bytes(content))
    check_meta(meta)
    return entry, blobs


def publish(path: Path, data: bytes) -> None:
    """Simplified §7.2 for a single writer: never overwrite."""
    path.parent.mkdir(exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def write_entry(
    root: Path,
    env: dict[str, Any],
    meta: dict[str, Any],
    reply: object = None,
    error: dict[str, Any] | None = None,
    failure_id: str | None = None,
) -> Path:
    entry, blobs = build_entry(env, meta, reply, error)
    for name, data in sorted(blobs.items()):
        path = root / name[:3] / name
        if not path.exists():
            publish(path, data)
    eid = entry_id(env)
    if failure_id is not None:
        if error is None or not FAILURE_ID.fullmatch(failure_id):
            raise ValueError("a failure record needs an error and a valid failure ID")
        name = f"{eid}.{failure_id}.failure.json"
    else:
        name = f"{eid}.json"
    path = root / eid[:3] / name
    publish(path, json_file_bytes(entry))
    return path


# §5 and §8 Reading ----------------------------------------------------------------


def _reject_constant(name: str) -> object:
    raise FormatError("invalid-json", f"{name} isn't JSON")


def _parse_int(text: str) -> int:
    if text == "-0":
        raise FormatError("invalid-value", "negative zero")
    n = int(text)
    check_number(n, "number")
    return n


def _parse_float(text: str) -> float:
    x = float(text)
    check_number(x, "number")
    return x


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise FormatError("invalid-json", f"duplicate member {k!r}")
        out[k] = v
    return out


def parse_json_file(data: bytes) -> object:
    if data.startswith(b"\xef\xbb\xbf"):
        raise FormatError("invalid-json", "byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FormatError("invalid-json", "not UTF-8") from e
    try:
        v = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as e:
        raise FormatError("invalid-json", str(e)) from e
    check_value(v)
    return v


def _check_nonneg_int(v: object, where: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise FormatError("schema", f"{where} must be a non-negative integer")


def check_meta(meta: object) -> None:
    if not isinstance(meta, dict):
        raise FormatError("schema", "meta isn't an object")
    if not isinstance(meta.get("recordedAt"), str) or not TIMESTAMP.fullmatch(meta["recordedAt"]):
        raise FormatError("schema", "meta.recordedAt")
    for name in ("recordedBy", "servedModel", "servedBy", "toolVersion", "codeVersion"):
        if name in meta and not isinstance(meta[name], str):
            raise FormatError("schema", f"meta.{name}")
    if "latencyMs" in meta:
        _check_nonneg_int(meta["latencyMs"], "meta.latencyMs")
    if "costUsd" in meta:
        c = meta["costUsd"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or c < 0:
            raise FormatError("schema", "meta.costUsd")
    if "usage" in meta:
        if not isinstance(meta["usage"], dict):
            raise FormatError("schema", "meta.usage")
        for name in ("inputTokens", "outputTokens", "reasoningTokens", "cacheReadTokens", "cacheWriteTokens"):
            if name in meta["usage"]:
                _check_nonneg_int(meta["usage"][name], f"meta.usage.{name}")
    if "tags" in meta:
        tags = meta["tags"]
        if not isinstance(tags, dict) or not all(isinstance(t, str) for t in tags.values()):
            raise FormatError("schema", "meta.tags")
    if "extra" in meta and not isinstance(meta["extra"], dict):
        raise FormatError("schema", "meta.extra")


def check_error(error: object) -> None:
    """Check a logical error object (§5.3)."""
    if not isinstance(error, dict):
        raise FormatError("schema", "error isn't an object")
    if not {"kind", "message"} <= error.keys() <= {"kind", "message", "httpStatus", "exitCode", "details"}:
        raise FormatError("schema", f"error members {sorted(error)}")
    if not isinstance(error["kind"], str) or not isinstance(error["message"], str):
        raise FormatError("schema", "error.kind and error.message must be strings")
    for name in ("httpStatus", "exitCode"):
        if name in error and (isinstance(error[name], bool) or not isinstance(error[name], int)):
            raise FormatError("schema", f"error.{name}")


def check_layout(root: Path) -> None:
    marker = root / "llmemo.json"
    if not marker.is_file():
        raise FormatError("not-a-cache", str(root))
    info = parse_json_file(marker.read_bytes())
    if not isinstance(info, dict) or info.get("layoutFormat") != LAYOUT_FORMAT:
        raise FormatError("unsupported-format", f"layoutFormat {info!r}")


@dataclass
class ReadResult:
    reply: object
    error: dict[str, Any] | None
    meta: dict[str, Any]


def read_entry_file(root: Path, path: Path, requested: dict[str, Any] | None, limits: Limits) -> ReadResult:
    """§8 steps 2-8 for one file. With requested=None, the stored key is checked
    against the file name instead (as verify does)."""
    if path.stat().st_size > limits.entry_bytes:
        raise FormatError("too-large", path.name)
    entry = parse_json_file(path.read_bytes())
    if not isinstance(entry, dict):
        raise FormatError("schema", "entry isn't an object")
    if "entryFormat" not in entry:
        raise FormatError("schema", "no entryFormat")
    if entry["entryFormat"] != ENTRY_FORMAT or isinstance(entry["entryFormat"], bool):
        raise FormatError("unsupported-format", f"entryFormat {entry['entryFormat']!r}")
    has_reply, has_error = "reply" in entry, "error" in entry
    content_name = "reply" if has_reply else "error"
    expected = {"entryFormat", "key", "contentSha256", "meta", content_name}
    if has_reply == has_error or set(entry) != expected:
        raise FormatError("schema", f"entry members {sorted(entry)}")
    if not isinstance(entry["contentSha256"], str) or not HEX64.fullmatch(entry["contentSha256"]):
        raise FormatError("schema", "contentSha256")
    check_meta(entry["meta"])
    stored_key = entry["key"]
    from_stored(stored_key, "key", None, limits)
    from_stored(entry[content_name], content_name, None, limits)
    if sha256_hex(canonical_bytes(entry[content_name])) != entry["contentSha256"]:
        raise FormatError("content-mismatch", path.name)
    key = from_stored(stored_key, "key", root, limits)
    check_key(key)
    assert isinstance(key, dict)
    if requested is not None:
        if canonical_bytes(key) != canonical_bytes(requested):
            raise FormatError("key-mismatch", path.name)
    elif not path.name.startswith(entry_id(key)):
        raise FormatError("key-mismatch", path.name)
    content = from_stored(entry[content_name], content_name, root, limits)
    if has_error:
        check_error(content)
        assert isinstance(content, dict)
        return ReadResult(None, content, entry["meta"])
    return ReadResult(content, None, entry["meta"])


def read_entry(root: Path, requested: dict[str, Any], limits: Limits | None = None) -> ReadResult | None:
    """Look up a key. Returns None on a miss, and raises FormatError on damage."""
    limits = limits or Limits()
    check_layout(root)
    eid = entry_id(requested)
    path = root / eid[:3] / f"{eid}.json"
    if not path.is_file():
        return None
    return read_entry_file(root, path, requested, limits)
