"""JSON values (spec §1), canonical JSON (§2), the file layout (§6.1), and parsing (§6.2).

Python's json module is used only to parse, with hooks that enforce §1. Everything llmemo
writes or hashes goes through the serializers here, never `json.dumps`, whose number and
string formatting differ from JavaScript's (docs/cross-language.md).
"""

from __future__ import annotations

import json
import math
from typing import Any

from ._errors import FormatError

MAX_SAFE_INTEGER = 2**53 - 1


# §1 JSON values -----------------------------------------------------------------------


def check_number(x: object, where: str) -> None:
    # Exact types: bool, IntEnum, and numpy scalars are subclasses, and §1 rejects them.
    if type(x) is int:
        if abs(x) > MAX_SAFE_INTEGER:
            raise FormatError("invalid-value", f"{where}: integer above 2^53 - 1")
    elif type(x) is float:
        if not math.isfinite(x):
            raise FormatError("invalid-value", f"{where}: number isn't finite")
        if x == 0 and math.copysign(1.0, x) < 0:
            raise FormatError("invalid-value", f"{where}: negative zero")
        if x.is_integer() and abs(x) > MAX_SAFE_INTEGER:
            raise FormatError("invalid-value", f"{where}: integer value above 2^53 - 1")
    else:
        raise FormatError("invalid-value", f"{where}: {type(x).__name__} isn't a number")


def check_string(s: str, where: str) -> None:
    # Fast path: a string that encodes as UTF-8 has no lone surrogates.
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise FormatError("invalid-value", f"{where}: lone surrogate in string") from None


def check_value(v: object, where: str = "$") -> None:
    """Raise FormatError("invalid-value") unless v is a JSON value as §1 defines it.

    Strings must be exactly `str` (a `StrEnum` member is rejected). Plain `dict` and `list`
    subclasses are accepted, since their JSON meaning is unambiguous.
    """
    t = type(v)
    if v is None or t is bool:
        return
    if t is str:
        check_string(v, where)  # type: ignore[arg-type]
    elif t is int or t is float:
        check_number(v, where)
    elif isinstance(v, list):
        for i, item in enumerate(v):
            check_value(item, f"{where}[{i}]")
    elif isinstance(v, dict):
        for k, item in v.items():
            if type(k) is not str:
                raise FormatError("invalid-value", f"{where}: member name {k!r} isn't a string")
            check_string(k, f"{where} member name")
            check_value(item, f"{where}.{k}")
    else:
        raise FormatError("invalid-value", f"{where}: {t.__name__} isn't a JSON value")


# §2 Canonical JSON ----------------------------------------------------------------------


def format_number(x: int | float) -> str:
    """ECMAScript Number::toString, for a number check_number accepts."""
    if type(x) is int:
        return str(x)
    assert isinstance(x, float)
    if x.is_integer():
        return str(int(x))  # within 2^53 by §1, so never an exponent
    r = repr(x)  # the shortest digits that round-trip, e.g. '1e-07' or '0.30000000000000004'
    sign = "-" if r[0] == "-" else ""
    r = r.lstrip("-")
    mantissa, _, exp_text = r.partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = (whole + frac).lstrip("0")
    # The value is 0.<digits> * 10^n.
    n = len(whole) + (int(exp_text) if exp_text else 0) - (len(whole + frac) - len(digits))
    digits = digits.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * -n + digits
    else:
        e = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + ("e+" if e >= 0 else "e-")
        s += str(abs(e))
    return sign + s


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r"}
_ESCAPES["\t"] = "\\t"
for _c in range(0x20):
    _ESCAPES.setdefault(chr(_c), f"\\u{_c:04x}")
_ESCAPE_TABLE = str.maketrans(_ESCAPES)


def format_string(s: str) -> str:
    return '"' + s.translate(_ESCAPE_TABLE) + '"'


def sorted_names(obj: dict[str, Any]) -> list[str]:
    """Member names in UTF-16 code unit order (RFC 8785)."""
    return sorted(obj, key=lambda name: name.encode("utf-16-be"))


def _canonical(v: object, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, str):
        out.append(format_string(v))
    elif isinstance(v, (int, float)):
        out.append(format_number(v))
    elif isinstance(v, list):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _canonical(item, out)
        out.append("]")
    else:
        assert isinstance(v, dict)
        out.append("{")
        for i, name in enumerate(sorted_names(v)):
            if i:
                out.append(",")
            out.append(format_string(name))
            out.append(":")
            _canonical(v[name], out)
        out.append("}")


def canonical_bytes(v: object) -> bytes:
    """The RFC 8785 canonical JSON of v, as UTF-8. Raises FormatError if v isn't a JSON value."""
    check_value(v)
    out: list[str] = []
    _canonical(v, out)
    return "".join(out).encode("utf-8")


# §6.1 JSON files -----------------------------------------------------------------------


def _pretty(v: object, indent: str, out: list[str]) -> None:
    if isinstance(v, list) and v:
        inner = indent + "  "
        out.append("[\n")
        for i, item in enumerate(v):
            out.append(",\n" + inner if i else inner)
            _pretty(item, inner, out)
        out.append("\n" + indent + "]")
    elif isinstance(v, dict) and v:
        inner = indent + "  "
        out.append("{\n")
        for i, name in enumerate(sorted_names(v)):
            out.append(",\n" + inner if i else inner)
            out.append(format_string(name) + ": ")
            _pretty(v[name], inner, out)
        out.append("\n" + indent + "}")
    elif isinstance(v, list):
        out.append("[]")
    elif isinstance(v, dict):
        out.append("{}")
    else:
        _canonical(v, out)


def json_file_bytes(v: object) -> bytes:
    """The exact bytes of a JSON file llmemo writes (§6.1)."""
    check_value(v)
    out: list[str] = []
    _pretty(v, "", out)
    out.append("\n")
    return "".join(out).encode("utf-8")


# §6.2 Parsing ---------------------------------------------------------------------------


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


def _reject_constant(name: str) -> object:
    raise FormatError("invalid-json", f"{name} isn't JSON")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        seen: set[str] = set()
        for k, _ in pairs:
            if k in seen:
                raise FormatError("invalid-json", f"duplicate member name {k!r}")
            seen.add(k)
    return out


def parse_json(data: bytes) -> Any:
    """Parse a JSON file strictly: UTF-8 without a BOM, no duplicate names, values per §1."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise FormatError("invalid-json", "byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FormatError("invalid-json", "not valid UTF-8") from None
    try:
        v = json.loads(
            text,
            object_pairs_hook=_object,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as e:
        raise FormatError("invalid-json", str(e)) from None
    except RecursionError:
        raise FormatError("invalid-json", "nested too deeply") from None
    check_value(v)  # strings: lone surrogates written as \ud800 escapes
    return v
