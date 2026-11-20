# Cross-language format notes

Python and TypeScript packages must compute the same hash for the same key, and read each
other's entries (README requirements X1–X4, K1, S1). This doc lists where the two
languages disagree by default, and how the format avoids those differences. Back to the
[README](../README.md).

This doc was the input to the format spec. The spec itself is
[spec/format.md](../spec/format.md), and where the two differ, the spec wins.

## Python and JavaScript disagree by default

Checked on 2026-11-20 with Python 3.12 (`json.dumps`) and Node 26 (`JSON.stringify`):

| Input | Python default | JavaScript | Why it matters |
|---|---|---|---|
| `1.0` | `1.0` | `1` | Same value, different bytes, different hash |
| `1e16` | `1e+16` | `10000000000000000` | Number formatting differs |
| `1e-7` | `1e-07` | `1e-7` | Exponent formatting differs |
| `-0.0` | `-0.0` | `0` | Negative zero is lost in JavaScript |
| `2**60` | `1152921504606846976` | `1152921504606847000` | JavaScript numbers lose precision above 2^53 |
| `NaN` | `NaN` (not valid JSON) | `null` (silently) | Neither should be allowed in a key |
| `"é"` | `"é"` (`ensure_ascii=True`) | `"é"` | String escaping differs |
| Separators | `", "` and `": "` | `","` and `":"` | Whitespace differs |
| Sorting `""` and `"😀"` | `` first (code points) | `😀` first (UTF-16 code units) | Sort order differs for characters outside the Basic Multilingual Plane |
| Lone surrogate `"\ud800"` | can't be encoded as UTF-8 | `"\ud800"` | Must be rejected |
| UTC timestamp | `2026-11-20T20:50:46.419394+00:00` | `2026-11-20T20:50:46.690Z` | Different precision and suffix |

Python also accepts things that aren't JSON values, or turns them into JSON values
quietly: dict keys that aren't strings (`{1: "a"}` becomes `{"1": "a"}`), tuples (which
become arrays), and, with a `default=` hook, enums, `Decimal`, and datetimes. A key
built from these can't be reproduced in another language unless the conversion is
explicit.

## How the format avoids the differences

- **Canonical JSON is RFC 8785 (JSON Canonicalization Scheme, JCS).** JCS sorts object
  keys by UTF-16 code units, formats numbers the way ECMAScript does (the same as
  `JSON.stringify`), escapes only the characters JSON requires, and adds no whitespace.
  It is built on JavaScript's behavior, so the TypeScript side is mostly `JSON.stringify`
  with sorted keys. That is also what Threshold's `canonicalJson()` does already. The
  Python side has to sort by UTF-16 code units and format numbers like ECMAScript.
  Libraries to evaluate: `rfc8785` on PyPI and `canonicalize` on npm. Their licenses
  and maintenance haven't been checked yet. A small implementation of our own,
  tested against the shared vectors, is also fine.
- **Key values are restricted** to strings, booleans, null, arrays, objects with string
  keys, and numbers. A number whose value is an integer must be within ±(2^53 − 1), and
  that includes floats such as `1e16`, since JavaScript can't tell them apart. `NaN`, `Infinity`, `-0`, and
  lone surrogates are rejected with an error, not converted. Callers convert enums,
  decimals, and model objects to plain JSON themselves (e.g. pydantic's
  `model_dump(mode="json")`). Rejecting these keeps conversions from happening silently.
- **No Unicode normalization.** Hashing is exact to the byte, and NFC and NFD forms are
  different keys. If a caller wants normalized keys, the caller normalizes them.
- **Hash:** SHA-256 over the UTF-8 bytes of the canonical JSON, as 64 lowercase hex
  digits. Python uses `hashlib`. TypeScript uses Web Crypto (`crypto.subtle.digest`), which
  works in both Node and the browser but is async.
- **Absent and null are different.** To support K8 (keys can gain fields), an absent field
  is left out of the canonical form, while `null` is kept. Settled in the spec (§1): a
  member is absent only when it isn't in the dict, and Python `None` is `null`. In
  TypeScript, a member whose value is `undefined` is absent.
- **Timestamps** are UTC ISO 8601 with exactly 3 fractional digits and `Z`, as
  `toISOString()` produces. Timestamps appear only in metadata, never in keys.
- **Durations and costs** are stored as plain numbers: latency in integer milliseconds,
  cost as a float in USD. They're never stored as `Decimal` strings, which WordSpace
  currently writes.

## Entry files

- An entry file doesn't have to be canonical, because readers parse it and compare keys
  (K10) rather than comparing bytes. Even so, both languages should write identical bytes
  for the same entry (S1), so that a migration run from either language causes no churn
  in git. The proposed format: JCS key order and number formatting, 2-space indentation,
  `\n` line endings, and a trailing newline. Python's `json.dumps(indent=2)` and
  `JSON.stringify(x, null, 2)` already agree on indentation and empty containers. They
  differ on numbers and escaping, so the Python writer needs its own serializer. The
  TypeScript writer needs one too: JavaScript objects iterate integer-like member names
  (`"2"`, `"10"`) first, whatever order they were added in, so `JSON.stringify` of a
  sorted copy isn't sorted.
- `JSON.parse` keeps the last of two duplicate member names without an error, so a
  TypeScript reader needs its own small parser to reject them (spec §6.2).
- Strings inside JSON escape newlines as `\n`, so converting line endings (Windows
  `autocrlf`) changes only the whitespace between tokens. Parsing is unaffected, and hashes
  don't depend on the file's bytes. The repo should still set `* text=auto eol=lf` in
  `.gitattributes`, as the owner's other repos do, so files don't change in diffs.
- File names use lowercase hex only, so they're safe on case-insensitive file systems
  (macOS and Windows). A path such as `ab/<64 hex>.json` stays well within Windows'
  path-length limit.

## Binary vectors

Embeddings are stored as binary (README S12), so the byte order is fixed as
little-endian IEEE 754. Python reads them with `struct` or
`numpy.frombuffer(..., "<f8")`, and TypeScript with a `DataView` (not a raw
`Float64Array`, which uses the platform's byte order). Like text files, a vector file is
named by its SHA-256 and checked when read.

## Atomic writes and locks

| Need | Python | Node |
|---|---|---|
| Create only if absent (a lock) | `os.open(path, O_CREAT \| O_EXCL \| O_WRONLY)` | `writeFile(path, data, { flag: "wx" })` |
| Publish without overwriting (S5) | write a temp file, `os.link(temp, final)`, then remove the temp | write a temp file, `link(temp, final)`, then remove the temp |
| Refresh a lock | `os.utime(path)` | `utimes(path, now, now)` |

- Don't use `os.replace` or `os.rename` to publish an entry. On POSIX they overwrite an
  existing file silently, which breaks S7. On Windows `os.rename` fails if the target
  exists, so behavior would differ between platforms.
- Hard links work on ext4, APFS, and NTFS. Some file systems don't support them (FAT,
  some network mounts). The spec requires them and defines no fallback (§7.2).
- Within one process, waiting on an in-memory "in flight" map stops coroutines and
  threads that need the same entry from paying twice. Across processes, lock files do
  the same (S6). Threshold implements both.

## Shared test vectors (X2)

Now in [spec/vectors/](../spec/vectors/README.md), as planned here:

- **Keys:** pairs of a key and its canonical JSON plus hash. They include awkward cases:
  floats (`1.0`, `1e16`, `1e-7`, `0.1 + 0.2`), non-ASCII text, characters outside the
  BMP that affect key order, control characters, nested empty containers, and an absent
  field next to a `null` one.
- **Invalid keys** that must be rejected: `NaN`, `-0`, integers above 2^53, lone
  surrogates, and non-string object keys.
- **Entries:** complete entry files, each with the exact bytes a writer must produce
  and the parsed result a reader must return, including entries that must be refused
  (too large, wrong schema, or a key that doesn't match its hash).
