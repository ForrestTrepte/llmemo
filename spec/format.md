# llmemo cache format, version 1

This is the language-neutral specification of llmemo's cache: how a key becomes an entry ID,
what an entry file holds, and how files are laid out on disk (README requirement X1). A
Python or TypeScript package must be implementable from this document and the
[test vectors](vectors/README.md) alone. Requirement IDs (K1, S4, …) refer to the
[README](../README.md).

The words **must**, **must not**, **should**, and **may** are used as in RFC 2119.

Three versions are defined here, and each one changes independently (§9):

| Version | Value | Where it's recorded | Changing it |
|---|---|---|---|
| `keyFormat` | `1` | Inside every key, so it's hashed (K7) | Changes every entry ID |
| `entryFormat` | `1` | Top level of every entry file (E3) | Entries can be rewritten without changing their IDs |
| `layoutFormat` | `1` | `llmemo.json` at the cache root | Changes where files go |

## 1. JSON values

Everything llmemo hashes or stores is a **JSON value**: `null`, `true`, `false`, a string,
a number, an array, or an object with string member names. These rules apply to keys,
replies, errors, and metadata, both when they're written and when they're read.

1. **Numbers** are IEEE 754 doubles. A number must be finite and must not be negative
   zero. A number whose value is an integer must have a magnitude of at most
   2^53 − 1 (9007199254740991). This rule is about the value, not its spelling or its
   type in the host language, so `1e16` is rejected, as is a Python `float` of
   `2.0**60`. Larger integers can't survive a round trip through JavaScript, and an
   identifier that big belongs in a string.
2. **Strings** must be sequences of Unicode scalar values. A lone surrogate (U+D800 to
   U+DFFF not part of a pair) is rejected. Strings aren't normalized: NFC and NFD forms
   are different values.
3. **Object member names** must be unique within an object, and follow the same string
   rules.
4. **Absent is not null.** A member that isn't present is absent. A member whose value is
   `null` is present. The two hash differently. In Python, a member is absent when it
   isn't in the dict, and `None` is `null`. In TypeScript, a member whose value is
   `undefined` is absent (as in `JSON.stringify`), and `undefined` anywhere else (e.g. in
   an array) is rejected.
5. Nothing else is a JSON value. Implementations must reject, not convert: Python tuples,
   sets, `bytes`, `Decimal`, enums, datetimes, non-string dict keys, and `bool` used as a
   number; JavaScript `BigInt`, `Date`, `Map`, and class instances. Callers convert these
   to JSON values themselves (e.g. pydantic's `model_dump(mode="json")`).
6. A parser that reads an entry must enforce these rules on what it reads, reject
   duplicate member names, and may reject nesting deeper than 128 levels.

A violation is reported with the error code `invalid-value` (§8).

## 2. Canonical JSON

The canonical form of a JSON value is its RFC 8785 (JSON Canonicalization Scheme)
serialization, as UTF-8 bytes. For the values §1 allows, that means:

- No whitespace anywhere.
- **Object members** are sorted by their names compared as sequences of UTF-16 code
  units (not code points, and not UTF-8 bytes). This differs from code-point order only
  for names containing characters above U+FFFF: `"😀"` (UTF-16 `D83D DE00`) sorts before
  `"ﬀ"` (U+FB00). JavaScript's default `Array.prototype.sort` on strings already uses this
  order. Python can sort by `name.encode("utf-16-be")`.
- **Numbers** are formatted as ECMAScript's `Number.prototype.toString` does, which is
  what `JSON.stringify` does for a finite number. Because of §1, the exponent form appears
  only for small magnitudes (`1e-7`, `5e-324`), never with `e+`. Examples: `1.0` → `1`,
  `0.1 + 0.2` → `0.30000000000000004`, `1e-6` → `0.000001`, `1e-7` → `1e-7`,
  `-1.5` → `-1.5`. A Python implementation can take the digits from `repr(float)` (which
  is the shortest round-trip form) and lay them out by the ECMAScript rules; see the
  reference implementation in [tools/reference.py](tools/reference.py).
- **Strings** are written between `"` with only these escapes: `\"`, `\\`, `\b`, `\f`,
  `\n`, `\r`, `\t`, and `\u00xx` (lowercase hex) for the other characters below U+0020.
  Every other character, including `/`, U+007F, U+2028, and non-ASCII text, is written as
  itself in UTF-8.
- `true`, `false`, and `null` are written as such.

## 3. Keys and entry IDs

### 3.1 The key envelope

The caller supplies a request and a few named parts (K2–K5). llmemo combines them into a
**key envelope**, an object with these members:

| Member | Required | Value | Requirement |
|---|---|---|---|
| `keyFormat` | yes | `1` | K7 |
| `attempt` | yes | An integer from 0 to 2^53 − 1 | K4 |
| `label` | no | A non-empty string, e.g. a prompt ID | K5 |
| `variant` | no | A non-empty string, e.g. `trial-3` | K3 |
| `request` | yes | Any JSON value the caller chooses | K2 |

`label` and `variant` are absent unless the caller gives them. An empty string is
rejected rather than treated as absent, so that "absent" has only one spelling. An API
may give `attempt` a default of 0, since it's always present in the envelope.

No other members are allowed. Everything else that should affect the hash goes in
`request`. Values that must not affect the hash, such as timeouts, the CLI version, and
tags, go in the entry's metadata (§5.4, K6). API keys go in neither.

### 3.2 The entry ID

The **entry ID** is the SHA-256 of the canonical JSON (§2) of the key envelope, written as
64 lowercase hex digits (K1).

Example: the envelope `{"keyFormat": 1, "attempt": 0, "request": {"model":
"anthropic/claude-haiku-4.5", "prompt": "Say hi."}}` has the canonical form

```
{"attempt":0,"keyFormat":1,"request":{"model":"anthropic/claude-haiku-4.5","prompt":"Say hi."}}
```

and its ID is given in [vectors/keys.json](vectors/keys.json) (case `minimal`).

The ID covers the full text of every string. It doesn't depend on how the entry is
stored (§4), so the rules for storing text and vectors can change without changing IDs.

### 3.3 Designing a request (guidance)

These aren't format rules, but they keep keys stable. Built-in backends follow them.

- **Put every input that affects the reply in `request`:** the backend name, the exact
  model ID (not an alias), the system prompt, messages, tools, every setting, and the
  output schema.
- **Adding a field (K8).** Leave a new field absent in calls that don't use it. Existing
  entries keep matching. Never leave a field out because it equals a default in your
  code: if the default later changes, old entries would silently match calls with the new
  value. That is the bug WordSpace has (see [prior-art.md](../docs/prior-art.md#wordspace)).
- **Removing a field.** If a field turns out not to matter, there are two choices,
  following Inspect AI. *Drop* it (stop sending it), which invalidates the entries that
  had it. Or *neutralize* it (keep sending the value old entries used, whatever the real
  setting is), which keeps them.
- **Attachments (K9)** go in the request by content hash, not by path or bytes. The
  recommended form is an object with `sha256` (64 lowercase hex digits of the file's
  bytes) and `mediaType`, e.g. `{"sha256": "…", "mediaType": "image/png"}`. llmemo
  doesn't store the attachment itself.
- **No cache policy in the key.** Expiry times, cache modes, and similar settings must
  not go in the key: changing them would invalidate every entry (Inspect AI puts its
  expiry in the key, and suffers for it).
- **No secrets.** API keys and tokens must never appear in a request (E4).

## 4. Stored form: text, vectors, and references

An entry file doesn't hold large strings or embedding vectors directly. They're stored
once, in files named by their content's hash, and the entry refers to them (S4, S12).
The **logical** value is what callers see. The **stored** value is what's in the entry
file. This section says how to convert between them.

### 4.1 Where it applies

The conversion applies to three values in an entry: `key`, `reply`, and `error` (§5).
It doesn't apply to `meta`, which is always stored as is.

### 4.2 References

A **reference** is an object with exactly one member, whose name is one of these:

| Name | Value | Refers to | Logical value |
|---|---|---|---|
| `$llmemo.text` | 64 lowercase hex digits | `<id>.txt` | A string |
| `$llmemo.f64` | 64 lowercase hex digits | `<id>.f64` | An array of numbers |
| `$llmemo.f32` | 64 lowercase hex digits | `<id>.f32` | An array of numbers |

The hex digits are the SHA-256 of the referenced file's bytes. A reader must check that
the value is exactly 64 characters from `0-9a-f` before using it to build a path; anything
else is a `schema` error. This keeps a hostile entry from naming a file outside the cache
(S8). Vector references may appear only in `reply`. Text references may appear in `key`,
`reply`, and `error`.

### 4.3 Escaping member names

Caller data could contain an object that looks like a reference. To keep the two apart,
every member name in caller data (in `key`, `reply`, or `error`) that starts with one or
more `$` followed by `llmemo` gets one more `$` in the stored form. So `$llmemo.text`
is stored as `$$llmemo.text`, and `$$llmemo` as `$$$llmemo`. Other names, including
`$schema`, `$ref`, and `$defs`, are stored unchanged.

When reading, an object with a member whose name is `$` followed by `llmemo` (exactly one
`$`) must be a reference as defined in §4.2, or it's a `schema` error. A name with two or
more `$` before `llmemo` loses one `$`.

### 4.4 Stored text

A string value is stored as text if, and only if, it contains a line feed (U+000A) or its
UTF-8 encoding is longer than **512 bytes**. This rule is fixed for `entryFormat` 1 so
that every writer produces the same bytes (S1). Readers must accept any string either
inline or as a reference, whatever its length. Member names are never stored as text.

The text file holds the string's UTF-8 bytes exactly: no byte order mark is added or
removed, line endings aren't converted, and no newline is added at the end. Its name is
the SHA-256 of those bytes plus `.txt`. Readers must read the file as bytes, check its
hash, and decode it as strict UTF-8 without stripping a leading BOM (e.g. Python
`open(path, "rb")` and `bytes.decode("utf-8")`, not text mode or `utf-8-sig`; JavaScript
`new TextDecoder("utf-8", { fatal: true, ignoreBOM: true })`). The decoded string must
also follow §1.

The same text is stored once, however many entries use it.

### 4.5 Stored vectors

Embedding vectors are stored as binary files (S12). The writer decides which arrays are
vectors: the caller or backend marks them explicitly (e.g. by wrapping the values in a
vector type), and nothing is converted automatically. A marked vector must be a
non-empty array of numbers.

- `.f64`: each number as an IEEE 754 binary64 value, little-endian, with no header or
  padding. The file's length is 8 times the array's length. This is exact, and the
  default.
- `.f32`: each number rounded to the nearest IEEE 754 binary32 value (ties to even), as
  little-endian binary32. Used when the caller opts in, or when the provider already
  returns float32. Rounding a finite number to a value outside binary32's range is an
  error.

The file is named by the SHA-256 of its bytes plus `.f64` or `.f32`. When reading, the
length must be a positive multiple of 8 (or 4), and every value must be finite;
otherwise it's a `blob-invalid` error. Negative zero is allowed inside vector files (§1
doesn't apply to their contents). Each value is decoded as a double, so an `.f32` value
reads back widened, e.g. `0.1` reads back as `0.10000000149011612`.

Readers must decode with explicit little-endian access (Python `struct.unpack("<…d")` or
`numpy.frombuffer(data, "<f8")`; JavaScript `DataView.getFloat64(offset, true)`), not a
typed array in the platform's byte order.

### 4.6 Converting

**Logical to stored** (writing), applied recursively:

- A string that §4.4 selects becomes `{"$llmemo.text": <id>}`, and the text file is
  written.
- A marked vector becomes `{"$llmemo.f64": <id>}` or `{"$llmemo.f32": <id>}`, and the
  vector file is written.
- An object's member names are escaped (§4.3), and its values converted.
- An array's items are converted. Other values are unchanged.

**Stored to logical** (reading) is the reverse: references are replaced by their content
(after checking it), and escaped names lose one `$`.

## 5. Entries

### 5.1 The entry object

An entry file holds one object with these members, and no others:

| Member | Required | Value |
|---|---|---|
| `entryFormat` | yes | `1` |
| `key` | yes | The stored form (§4) of the key envelope (§3.1) |
| `reply` | one of `reply` or `error` | The stored form of the backend's raw reply, any JSON value (E1) |
| `error` | one of `reply` or `error` | The stored form of an error object (§5.3) |
| `contentSha256` | yes | SHA-256 of the canonical JSON of the stored `reply` (or `error`), lowercase hex |
| `meta` | yes | Metadata (§5.4) |

The **reply** is whatever the backend returned, unchanged: a string for a plain text
completion, or an object such as `claude -p`'s JSON output or an HTTP response body. A
reply that arrived but is invalid (refused, cut off, not JSON, the wrong schema) is an
ordinary reply (E5). The caller parses and validates it on every use.

`contentSha256` lets a reader detect a reply edited by hand or damaged, the way the file
name detects a changed key. It's computed over the stored form, so it doesn't require
reading the text and vector files, which are checked by their own names.

### 5.2 Example

```json
{
  "contentSha256": "…",
  "entryFormat": 1,
  "key": {
    "attempt": 0,
    "keyFormat": 1,
    "label": "clue-giver",
    "request": {
      "backend": "claude-cli",
      "model": "claude-haiku-4-5-20251001",
      "prompt": "Prompt: Lighthouse",
      "system": {
        "$llmemo.text": "3f1c…"
      }
    },
    "variant": "trial-1"
  },
  "meta": {
    "costUsd": 0.0021,
    "latencyMs": 4120,
    "recordedAt": "2026-11-20T22:15:00.123Z",
    "recordedBy": "developer",
    "usage": {
      "inputTokens": 1830,
      "outputTokens": 64
    }
  },
  "reply": "{\"placements\": [\"A1\", \"C4\"]}"
}
```

The hashes are shortened here. Complete files are in
[vectors/entries/](vectors/entries/).

### 5.3 Error entries and failure records

When no reply arrived (a network or HTTP error, a timeout, a subprocess that crashed),
nothing is cached by default (E5). Two options exist:

- A **failure record** saves the failure for diagnosis. It doesn't affect lookups. It's an
  entry object with `error` in place of `reply`, stored at
  `<prefix>/<entry id>.<failure id>.failure.json`. The failure ID is the UTC time in the
  compact form `YYYYMMDDTHHMMSSmmmZ` (e.g. `20261120T221500123Z`), a hyphen, and 8 random
  lowercase hex digits, so records from parallel writers don't collide. There may be any
  number of failure records for one entry ID.
- An **error entry** replays a failure on purpose (WordSpace's `.inject`). It's the same
  object stored at the entry's normal path, `<prefix>/<entry id>.json`. A hit on an error
  entry raises the stored error instead of calling the backend.

The error object:

| Member | Required | Value |
|---|---|---|
| `kind` | yes | A string. Backends should use one of `timeout`, `network`, `http`, `process`, `other`. Readers must accept any string |
| `message` | yes | A string |
| `httpStatus` | no | An integer HTTP status code |
| `exitCode` | no | An integer process exit code |
| `details` | no | Any JSON value |

Readers must reject other members of the error object (`schema`). An error must not
contain secrets; backends must leave request headers and credentials out of `message` and
`details`.

### 5.4 Metadata

`meta` is for information only and never affects a lookup (E2). All members except
`recordedAt` are optional, and are left out when unknown (never written as `null`).

| Member | Value |
|---|---|
| `recordedAt` | When the reply arrived, as UTC ISO 8601 with exactly 3 fractional digits and `Z`: `2026-11-20T22:15:00.123Z` (X3) |
| `recordedBy` | Who recorded it, e.g. `developer`, `claude`, or `ci` |
| `latencyMs` | Integer milliseconds from sending the request to receiving the whole reply |
| `usage` | An object with any of `inputTokens`, `outputTokens`, `reasoningTokens`, `cacheReadTokens`, `cacheWriteTokens`, each a non-negative integer. `cacheReadTokens` and `cacheWriteTokens` are the provider's prompt cache, not llmemo's |
| `costUsd` | Non-negative number, the cost in US dollars as the backend reported it. For `claude -p` on a subscription, this is the API-equivalent cost (M6) |
| `servedModel` | The model that actually answered, as the provider reported it |
| `servedBy` | The provider or host that answered, e.g. an OpenRouter provider name |
| `toolVersion` | The backend's tool or CLI version, e.g. `claude 2.1.3` |
| `codeVersion` | The caller's code version, `<commit>` or `<commit>-dirty` |
| `tags` | An object whose values are all strings, defined by the caller (e.g. `run`, `game`, `role`) |
| `extra` | Any JSON object: values the caller passed outside the key (K6), such as a timeout |

Readers must ignore members of `meta` and `meta.usage` that they don't know, so new
metadata can be added without a format change. Values in `meta` follow §1, and are never
converted to references (§4).

## 6. Writing files

### 6.1 JSON files

Every JSON file llmemo writes (entries, failure records, and `llmemo.json`) uses this
exact form, so that the Python and TypeScript packages write identical bytes (S1):

- UTF-8, no byte order mark, LF line endings, and one LF at the end of the file.
- Object members in canonical order (§2), at every level.
- Scalars (numbers, strings, `true`, `false`, `null`) exactly as in canonical JSON (§2).
- Two spaces of indentation per level. A non-empty array or object puts each item on its
  own line, followed by `,` except for the last, and its closing bracket on its own line
  at the parent's indentation. An object member is written as name, `: `, value.
- An empty array is `[]`, and an empty object is `{}`.

This is the layout of `JSON.stringify(value, null, 2)`, but a TypeScript writer can't use
that function directly: JavaScript objects put integer-like member names (`"2"`, `"10"`)
before all others regardless of insertion order, which breaks canonical order. Python's
`json.dumps(..., indent=2)` has the right layout but the wrong number and string
formatting. Both packages need a small serializer of their own.

### 6.2 Readers are lenient about layout

Entry files are parsed, not compared byte for byte. A reader must accept any valid JSON
text in UTF-8, with any whitespace, any member order, and CRLF line endings (e.g. after a
checkout with `core.autocrlf`). It must reject a byte order mark, invalid UTF-8, and
everything §1 and §5 reject.

Canonical JSON doesn't keep the difference between `1` and `1.0`, so a reply recorded as
the Python float `1.0` reads back as the int `1`. They're the same JSON value, and
callers that care should parse the reply with a schema.

A reader needs a JSON parser that reports duplicate member names. Python's `json.loads`
can do it with `object_pairs_hook`, and its `parse_int`, `parse_float`, and
`parse_constant` hooks can enforce §1 (note that `json.loads("-0")` returns the int `0`,
so `-0` must be caught in `parse_int`). JavaScript's `JSON.parse` silently keeps the last
duplicate, so a TypeScript reader needs a small parser of its own; it should also add
members with `Object.defineProperty`, so a member named `__proto__` stays an ordinary
member. [tools/check_vectors.mjs](tools/check_vectors.mjs) has one.

## 7. Directory layout

### 7.1 Files

```
<cache root>/
  llmemo.json                              {"layoutFormat": 1}
  .gitattributes
  .gitignore
  3fa/
    3fa…(64 hex).json                      entry
    3fa…(64 hex).20261120T221500123Z-1a2b3c4d.failure.json   failure record
    3fa…(64 hex).txt                       stored text
    3fa…(64 hex).f64                       stored vector
    3fa…(64 hex).lock                      lock (ignored by git)
    3fa…(64 hex).9c1e5b7a20d4f386.tmp      temporary file (ignored by git)
```

- Every file lives in the **prefix folder** named by the first 3 hex digits of its ID
  (S2). There are at most 4,096 prefix folders, and no other folders.
- Entries, text files, and vector files share the prefix folders. Their IDs come from
  different hashes, so they're told apart by extension.
- File and folder names use only lowercase hex, `.`, `-`, and the letters in the fixed
  suffixes, so they behave the same on case-insensitive file systems.
- `llmemo.json` marks the folder as a cache. A reader that finds no `llmemo.json` reports
  `not-a-cache` instead of treating the folder as an empty cache. A `layoutFormat` it
  doesn't know is `unsupported-format`.

When a writer creates a new cache, it writes `llmemo.json`, plus these two files exactly
(they're also in the write vectors):

`.gitattributes`:

```
# Written by llmemo. Stored text keeps its exact bytes.
*.txt -text
*.f32 binary
*.f64 binary
*.json text eol=lf
```

`.gitignore`:

```
# Written by llmemo.
*.lock
*.tmp
```

A cache may be committed (completions) or gitignored as a whole (large embedding caches,
S11). One project may have several caches in different folders.

### 7.2 Publishing a file

Every file is published atomically, so a reader never sees a partly written file (S5):

1. Write the content to a temporary file in the same prefix folder, named
   `<id>.<16 random hex digits>.tmp`, and flush it to disk (`fsync`).
2. Create the final name as a **hard link** to the temporary file. Creating a link fails if
   the name already exists, so a file is never overwritten.
3. Remove the temporary file, whether or not the link succeeded.

Hard links are required. If the file system doesn't support them (some network mounts,
FAT), the file store reports an error instead of falling back to a write that a reader
could see half done. `rename` and `os.replace` must not be used to publish, because on
POSIX they silently replace an existing file (S7).

The order of writes is: text and vector files first, then the entry. If a text or vector
file already exists, that's success, since it's named by its content. If an entry already
exists when the link is attempted, another writer recorded the same key first. The first
entry wins, the loser's reply is discarded, the loser returns the winner's entry, and a
warning names the entry ID, because someone paid for a duplicate call (S5).

**Refresh** (M3, S7) is the only way to replace an entry. While holding the entry's lock
(§7.3), the writer writes a temporary file as above and then renames it over the entry,
which replaces it atomically.

### 7.3 Locks

Locks keep parallel processes from paying for the same missing entry (S6). Within one
process, an implementation should also keep a map of entries being recorded, so threads
and coroutines wait on each other without touching the file system.

- A lock is the file `<prefix>/<id>.lock`, created with create-if-absent (`O_CREAT |
  O_EXCL`, or Node's `wx` flag). Its content is informational, e.g. a JSON object with
  the process ID, host name, and acquisition time. Readers must not depend on it.
- **Recording a miss:** take the lock. Check again whether the entry exists, since
  another writer may have published it in the meantime; if it does, release the lock and
  use it. Otherwise call the backend, publish, and delete the lock.
- **Waiting:** a process that finds the lock taken polls until the lock is gone (an
  interval of 100–500 ms is suggested), then reads the entry. If there's still no entry
  (the holder failed), it tries to take the lock itself.
- **Refreshing:** while it holds a lock, the holder updates the lock's modification time
  at least every **10 seconds**, from a timer that runs while the backend call is in
  progress (not only between calls).
- **Stale locks:** a lock whose modification time is more than **60 seconds** old is
  abandoned. To take it over, rename it to a temporary name (`<id>.<16 random hex
  digits>.tmp`). Only one process's rename can succeed. The winner deletes the renamed
  file and tries to create the lock again. Both durations should be configurable, and
  the stale limit must be several times the refresh interval.
- **Cleanup:** temporary files older than one hour may be deleted by any process. Verify
  (T5) reports locks and temporary files it finds.

## 8. Reading an entry

Given a requested key envelope, a reader:

1. Computes the entry ID (§3.2) and the path `<prefix>/<id>.json`. If there's no file,
   it's a miss.
2. Checks the file's size before reading it. The default limit is **1 MiB**, and it
   should be configurable (`too-large`).
3. Parses it (§6.2, `invalid-json`), and checks the values (§1, `invalid-value`).
4. Checks `entryFormat` (`unsupported-format` if it isn't 1) and the schema of §5
   (`schema`), including the reference syntax (§4.2, §4.3).
5. Checks `contentSha256` against the stored `reply` or `error` (`content-mismatch`).
6. Converts the stored key to its logical form and compares its canonical JSON with that
   of the requested key (`key-mismatch`). This catches edited keys, copied or renamed
   files, and hash collisions (K10). An implementation may skip loading a text file here
   by comparing the requested string's SHA-256 with the reference instead.
7. Converts the stored reply or error to its logical form. Each text or vector file is
   checked for existence (`blob-missing`), size (`too-large`, default limit **16 MiB**),
   hash (`blob-mismatch`), and content (`blob-invalid`: invalid UTF-8, a lone surrogate,
   or a bad vector length or value).
8. Returns the logical reply and the metadata, or, for an error entry, raises the stored
   error.

Errors that mean the cache itself is damaged are reported with one of these codes, so
both packages and their tests agree:

| Code | Meaning |
|---|---|
| `not-a-cache` | The folder has no `llmemo.json` |
| `unsupported-format` | An `entryFormat` or `layoutFormat` this reader doesn't know |
| `too-large` | A file is over its size limit |
| `invalid-json` | Not valid UTF-8 JSON, a byte order mark, or a duplicate member name |
| `invalid-value` | A value that §1 forbids |
| `schema` | Missing or extra members, wrong types, or a malformed reference |
| `content-mismatch` | `contentSha256` doesn't match the stored reply or error |
| `key-mismatch` | The stored key isn't the requested key |
| `blob-missing` | A referenced text or vector file doesn't exist |
| `blob-mismatch` | A text or vector file's content doesn't match its name |
| `blob-invalid` | A text or vector file has the right hash but invalid content |

Two more codes apply when building a key rather than reading: `invalid-key` when a part
of the envelope breaks §3.1 (a negative or fractional `attempt`, an empty `label`), and
`invalid-value` when a value breaks §1. JSON text that isn't JSON at all, such as `NaN`
in a file, is `invalid-json`; a well-formed number that §1 forbids, such as `1e400` or
`-0`, is `invalid-value`.

A damaged entry is an error, not a miss: replacing it silently would hide the damage, and
in cache-only mode (M2) a miss would point at the wrong fix. Refresh (M3) can replace it.
**Verify** (T5) runs the same checks on every file in the cache, and also checks that
each entry's stored key hashes to its file name.

## 9. Versions

- **`keyFormat`** changes when the envelope or canonicalization changes in a way that
  gives an existing key a different ID. Since it's inside the key, a new `keyFormat`
  never matches old entries. Old entries stay readable but unreachable until re-recorded
  or rehashed by a one-off script.
- **`entryFormat`** changes when an old reader couldn't read a new entry correctly: a new
  required member, a new kind of reference, or a change to the stored-text rule in §4.4
  (since that changes the bytes writers produce). Adding an optional `meta` member
  doesn't need a change. A reader must refuse an `entryFormat` it doesn't know.
- **`layoutFormat`** changes when file names or folders change.

Any of these changes must update this spec and the vectors in the same commit (see
[CLAUDE.md](../CLAUDE.md)).
