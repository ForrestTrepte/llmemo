"""The stored form of keys, replies, and errors: text and vector files (spec §4)."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Callable, Iterable
from typing import Any, Literal

from ._errors import FormatError
from ._json import check_number, check_string, check_value

TEXT_INLINE_MAX_BYTES = 512  # §4.4, fixed for entryFormat 1

HEX64 = re.compile(r"[0-9a-f]{64}")
_NEEDS_ESCAPE = re.compile(r"\$+llmemo")  # §4.3, matched at the start of a name
_ESCAPED = re.compile(r"\$\$+llmemo")
_RESERVED = re.compile(r"\$llmemo")
_REF_EXT = {"$llmemo.text": "txt", "$llmemo.f64": "f64", "$llmemo.f32": "f32"}

Where = Literal["key", "reply", "error"]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Vector:
    """Marks an array of numbers in a reply to be stored as a binary file (S12, spec §4.5).

    `dtype` is `"f64"` (exact, the default) or `"f32"` (half the size; each value is rounded
    to float32). It reads back as a plain list of floats.
    """

    __slots__ = ("dtype", "values")

    def __init__(self, values: Iterable[float], dtype: Literal["f64", "f32"] = "f64") -> None:
        if dtype not in ("f64", "f32"):
            raise ValueError(f"dtype must be 'f64' or 'f32', not {dtype!r}")
        out: list[float] = []
        for x in values:
            # float() accepts numpy scalars. Marking the array is the explicit conversion.
            if isinstance(x, bool) or (
                not isinstance(x, (int, float)) and not hasattr(x, "__float__")
            ):
                raise FormatError("invalid-value", f"vector: {type(x).__name__} isn't a number")
            out.append(float(x))
        if not out:
            raise FormatError("invalid-value", "vector: empty")
        self.values: tuple[float, ...] = tuple(out)
        self.dtype: Literal["f64", "f32"] = dtype

    def __repr__(self) -> str:
        return f"Vector(<{len(self.values)} values>, dtype={self.dtype!r})"

    def to_bytes(self) -> bytes:
        for x in self.values:
            check_number(x, "vector")  # finite, not -0 (§1 applies to what the caller passes)
        n = len(self.values)
        if self.dtype == "f64":
            return struct.pack(f"<{n}d", *self.values)
        try:
            return struct.pack(f"<{n}f", *self.values)
        except OverflowError:
            raise FormatError("invalid-value", "vector: value outside float32 range") from None


def stores_as_text(s: str) -> bool:
    """§4.4: a string with a line feed, or over 512 UTF-8 bytes, goes in a .txt file."""
    return "\n" in s or (len(s) > TEXT_INLINE_MAX_BYTES // 4 and len(s.encode()) > 512)


def to_stored(v: object, where: Where, blobs: dict[str, bytes]) -> Any:
    """Logical value -> stored value (§4.6). Adds files to write to `blobs` (name -> bytes).

    Raises FormatError("invalid-value") if v isn't a JSON value (with Vectors in a reply).
    """
    if isinstance(v, Vector):
        if where != "reply":
            raise FormatError(
                "invalid-value", f"vectors are allowed only in the reply, not {where}"
            )
        data = v.to_bytes()
        h = sha256_hex(data)
        blobs[f"{h}.{v.dtype}"] = data
        return {f"$llmemo.{v.dtype}": h}
    if type(v) is str:
        check_string(v, where)
        if stores_as_text(v):
            data = v.encode("utf-8")
            h = sha256_hex(data)
            blobs[f"{h}.txt"] = data
            return {"$llmemo.text": h}
        return v
    if isinstance(v, list):
        return [to_stored(item, where, blobs) for item in v]
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, item in v.items():
            if type(k) is not str:
                raise FormatError("invalid-value", f"{where}: member name {k!r} isn't a string")
            check_string(k, f"{where} member name")
            out["$" + k if _NEEDS_ESCAPE.match(k) else k] = to_stored(item, where, blobs)
        return out
    check_value(v, where)
    return v


def check_refs(v: object, where: Where) -> None:
    """Check the reference and escaping syntax of a stored value (§4.2, §4.3)."""
    if isinstance(v, list):
        for item in v:
            check_refs(item, where)
    elif isinstance(v, dict) and _ref(v, where) is None:
        for item in v.values():
            check_refs(item, where)


def _ref(v: dict[str, Any], where: Where) -> tuple[str, str] | None:
    """(hash, extension) if v is a reference, None if it's an ordinary object."""
    for name in v:
        if _RESERVED.match(name):
            break
    else:
        return None
    ext = _REF_EXT.get(name)
    if len(v) != 1 or ext is None:
        raise FormatError("schema", f"{where}: malformed reference {sorted(v)}")
    if ext != "txt" and where != "reply":
        raise FormatError("schema", f"{where}: vector reference outside the reply")
    h = v[name]
    if not isinstance(h, str) or not HEX64.fullmatch(h):
        raise FormatError("schema", f"{where}: reference {h!r} isn't 64 lowercase hex digits")
    return h, ext


LoadBlob = Callable[[str], bytes]
"""Returns a text or vector file's bytes by name (`<sha256>.<ext>`), already checked
against its hash and size limit, or raises FormatError (blob-missing, too-large, ...)."""


def decode_blob(data: bytes, ext: str) -> str | list[float]:
    if ext == "txt":
        try:
            text = data.decode("utf-8")  # strict, and keeps a leading BOM (§4.4)
        except UnicodeDecodeError:
            raise FormatError("blob-invalid", "text file isn't valid UTF-8") from None
        return text  # Python's strict decoder already rejects encoded surrogates
    size = 8 if ext == "f64" else 4
    if not data or len(data) % size:
        raise FormatError("blob-invalid", f"vector file length {len(data)}")
    values = list(struct.unpack(f"<{len(data) // size}{'d' if size == 8 else 'f'}", data))
    if not all(map(math.isfinite, values)):
        raise FormatError("blob-invalid", "vector value isn't finite")
    return values


def from_stored(v: object, where: Where, load: LoadBlob) -> Any:
    """Stored value -> logical value (§4.6). The syntax must already be checked."""
    if isinstance(v, list):
        return [from_stored(item, where, load) for item in v]
    if not isinstance(v, dict):
        return v
    ref = _ref(v, where)
    if ref is not None:
        h, ext = ref
        return decode_blob(load(f"{h}.{ext}"), ext)
    return {(k[1:] if _ESCAPED.match(k) else k): from_stored(x, where, load) for k, x in v.items()}
