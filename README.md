# llmemo

A record-and-replay cache for LLM calls, stored as plain files that are committed to git.

**Status (2026-11-20): requirements stage. No code yet.** This README holds the project's
goals and requirements. The first implementation will be a Python package, and its first
user is [ConceptExpansion](https://github.com/ForrestTrepte/ConceptExpansion). A TypeScript
package that reads and writes the same cache format may follow, in this same repository.

## Why

Several of the owner's projects call LLMs from scripts, notebooks, and tests, and each one
built its own cache so it could:

- rerun an experiment or test for free when the calls haven't changed,
- keep a permanent, reviewable record of what each model was asked and what it said, and
- run tests and CI with no API key.

Those caches work, but each has different flaws (see [docs/prior-art.md](docs/prior-art.md)).
One uses Python `repr` and SHA-1 in a flat directory of 100,000 files. Others use pickled
SQLite caches that git can't merge, or a single JSON file that is rewritten on every call.
Only one of them handles parallel recording safely. llmemo will be the one cache they can
all share.

## Goals

1. **Record once, replay forever.** A call whose inputs haven't changed is answered from
   the cache, with no API key, no network, and no cost. This works even after the model
   is retired.
2. **Git-native.** Entries are small text files that are readable in a diff and never
   conflict when branches merge. The cache is meant to be committed.
3. **Works with any backend.** llmemo wraps any `request -> response` callable: an HTTP
   SDK, an OpenAI-compatible endpoint such as OpenRouter, or a subprocess such as
   `claude -p`. It doesn't know about providers.
4. **One format for every language.** The on-disk format is specified independently of
   Python, so a TypeScript package can share caches with the Python one.
5. **Trustworthy.** Every entry is checked when it is read. Secrets are never stored.
   Nothing in the cache is executed or unpickled.
6. **Cost and latency stay visible.** Each entry records what the call cost and how long
   it took, so replayed runs can report what they would have cost without the cache.
7. **Small.** It has a small API, few dependencies, and is easy to adopt.

## Non-goals

- **Semantic or fuzzy matching.** A hit requires exactly the same key. Similar prompts
  don't count.
- **Provider adapters, prompt templating, and output parsing.** These belong to the
  caller. llmemo stores the raw reply, and the caller parses and validates it every time.
- **Making keys deterministic.** If the caller builds a request using unseeded randomness
  or unordered iteration, every run misses. llmemo can help explain a miss (T6), but it
  can't prevent one.
- **Rate limiting, concurrency limits, and retrying failed calls.** Every Python project
  has its own version of these, but they aren't caching. They might become a separate
  module later (see Open questions).

## Users

| Project | Language | Role |
|---|---|---|
| [ConceptExpansion](https://github.com/ForrestTrepte/ConceptExpansion) | Python | **First user.** Simulated playtests via `claude -p`. Its 26 requirements are included below as `[CE n]`. |
| [Threshold](https://github.com/ForrestTrepte/Threshold) | TypeScript | Informs the design. It has the most complete cache (parallel-safe recording, replay-only tests, pruning). A future TypeScript package would fit it. |
| [WordSpace](https://github.com/ForrestTrepte/WordSpace) | Python | Informs the design. It has the largest cache (104k entries), lets old entries keep matching when the key gains fields, and records failed calls. |
| [SoCloverAI](https://github.com/ForrestTrepte/SoCloverAI) | Python | Informs the design. It caches embeddings, tracks cached and uncached cost, and shows the problems with litellm and LangChain caches. |

The three projects that inform the design won't be changed for now. It should still be
possible to move each of them onto llmemo later.

## Requirements

Requirement IDs are stable. Don't renumber them. Withdraw a requirement by striking it
through and saying why. Tags show where each one came from: `[CE n]` is
ConceptExpansion's [cache requirement](https://github.com/ForrestTrepte/ConceptExpansion/blob/main/docs/simulated-playtesting.md#cache-requirements)
*n*, and `[T]`, `[WS]`, and `[SC]` stand for Threshold, WordSpace, and SoCloverAI.
**(later)** marks requirements that the first release doesn't need.

### Keys

- **K1. Stable hash.** The entry ID is the SHA-256 of the key's canonical JSON, as
  lowercase hex. Python and TypeScript must produce identical bytes, so canonicalization
  follows a written spec (RFC 8785 is the leading candidate; see
  [docs/cross-language.md](docs/cross-language.md)). It never uses Python's `hash()`,
  `repr`/`str` of objects, or pickle. `[CE 1] [T]`
- **K2. The caller defines the key.** The caller passes the key as plain JSON data:
  backend, exact model ID (not an alias), system prompt, messages, tools, every setting,
  and the output schema. llmemo doesn't guess at fields. `[CE 2] [T]`
- **K3. Replicate number** in the key, for independent samples of the same request
  (WordSpace and SoCloverAI call it a "trial", Threshold a "variant"). `[CE 3] [WS] [SC] [T]`
- **K4. Attempt number** in the key, so a run that retried after invalid output replays
  the same sequence of failures and successes. `[CE 4] [T]`
- **K5. Optional label** (e.g. a prompt ID) in the key. It isn't needed for uniqueness,
  but it groups entries and makes them easy to identify in logs, reports, and pruning. `[T]`
- **K6. Fields outside the key are explicit.** Values such as timeouts, CLI version, and
  tags are passed separately from the key and stored only as metadata. API keys are
  never passed in at all. `[CE 5]`
- **K7. Versioned key format.** The format version is part of what gets hashed. `[CE 6]`
- **K8. Keys can gain fields.** Adding a new key field must not invalidate existing
  entries as long as calls that don't use the field leave it absent. This must not
  depend on code defaults: WordSpace left out fields that equaled their default, so
  changing a default would silently reuse entries recorded with the old value
  ([docs/prior-art.md](docs/prior-art.md#wordspace)). `[WS] [T]`
- **K9. Attachments are hashed by content.** Attached files and images are included in
  the key by their content hash. `[CE 2]`
- **K10. Every read checks the key.** Each entry stores its full key. On read, the
  stored key is compared with the requested one, which catches hash collisions and
  edited files. `[T]`

### Entries

- **E1. The raw reply is stored unchanged.** Parsing is the caller's job, and the caller
  validates the reply on every use. So a change to validation code takes effect without
  re-recording. `[CE 7] [T]`
- **E2. Metadata**, kept for information only: when the entry was recorded, latency,
  token usage (input, output, reasoning, and tokens read from or written to the
  provider's prompt cache), cost, the model and host that actually answered, the tool or
  CLI version, the code version (`<commit>[-dirty]`), who recorded it (e.g. `claude`,
  `developer`, or `ci`), and caller-defined tags (e.g. run, game, role). Usage and cost
  are optional, since not every backend reports them. `[CE 8] [T] [WS] [SC]`
- **E3. Versioned entry format**, separate from the key format version, so entries can
  be migrated without changing their keys. `[T] [WS]`
- **E4. No secrets.** Entries and metadata never contain API keys or credentials. A check
  for known secret values is provided (see T5). `[CE 9] [T]`
- **E5. Failed calls.** A reply that arrived but is invalid (refused, cut off, not JSON,
  or wrong schema) is cached like any other. When no reply arrived (a network or HTTP
  error, or a timeout), nothing is cached by default. As an option, the failure can be
  saved for diagnosis or replayed on purpose, as WordSpace does with `.exception` and
  `.inject` files. `[CE 10] [T] [WS]`
- **E6. (later) Streaming.** A complete reply (text plus any tool call) can be replayed
  as a stream, so a cached chat feels the same as a live one. `[T]`

### Storage

- **S1. One file per entry**, written as pretty-printed JSON with a stable key order,
  UTF-8, LF line endings, and a trailing newline. When Python and TypeScript write the
  same entry, the bytes should be identical. `[CE 11] [T]`
- **S2. Hash-prefix folders** (e.g. `ab/ab12….json`). GitHub's web UI lists only 1,000
  files per directory, and WordSpace's flat directory already holds 104k. `[CE 11] [T] [WS]`
- **S3. Never SQLite, pickle, or one big file.** A SQLite database committed with its
  write-ahead-log files can't be merged, and pickle runs code when it is loaded. A single
  JSON file has to be rewritten on every call and conflicts on every merge. `[CE 11] [SC]`
- **S4. Large repeated inputs are stored once.** System prompts and reference docs are
  stored by hash and referenced from entries, and pruning removes ones no entry uses.
  `[CE 12]`
- **S5. Atomic writes.** Each entry is written to a temporary file and then linked into
  place, which fails if the entry already exists. So a reader never sees a partly written
  entry, and if two writers record the same key, the first one wins. `[CE 13] [T]`
- **S6. No duplicate paid calls.** Parallel threads, coroutines, and processes that need
  the same missing entry wait for one recorder instead of each paying for the call.
  Across processes this uses lock files. A lock that hasn't been refreshed for a set time
  is treated as abandoned and taken over, and stale locks and temporary files are cleaned
  up. `[CE 13] [T]`
- **S7. No silent overwrites.** Replacing an entry requires an explicit refresh (M3). `[CE 14]`
- **S8. Entries are untrusted.** Every read enforces a size limit and checks the schema
  and the key (K10). Content from the cache is never executed. `[T]`
- **S9. Scales to hundreds of thousands of entries.** A lookup touches only the entry's
  own file. Nothing loads an index of the whole cache at startup. An optional check fails
  when the cache grows past a size limit. `[WS] [SC] [T]`
- **S10. Pluggable stores.** Backends include a file store and an in-memory store (for
  tests and short sessions, like Threshold's per-game cache in the browser). A file store
  can have an in-memory layer for speed. `[T] [WS]`
- **S11. Any location, several caches.** The cache directory is configurable, and one
  project can use several caches, e.g. a committed cache for completions and a gitignored
  one for bulky embeddings. `[SC]`

### Modes

- **M1. Read-write:** a miss calls the backend and records the result. `[CE 15] [T]`
- **M2. Cache-only (replay):** a miss raises an error that names the label, replicate,
  attempt, and hash, and says how to record the entry. CI and tests use this mode. `[CE 16] [T]`
- **M3. Refresh** selected entries, chosen by label, tag, or hash, by calling again and
  replacing them. `[CE 17] [T] [WS]`
- **M4. Dry run:** count misses and estimate their cost without calling. `[CE 18]`
- **M5. Configurable from the environment**, so the mode can be switched without code
  changes (e.g. `LLMEMO_MODE=record`, like Threshold's `LLM_CACHE=record`). `[T]`
- **M6. (later) Spending limit:** stop calling the backend once a run has spent a set
  amount, as protection against a runaway retry loop. `[T]`

### API

- **A1. Wraps any callable.** llmemo wraps any function from request to response, such as
  an HTTP SDK or a subprocess call (ConceptExpansion needs the subprocess case). The
  callable returns the raw reply plus optional usage, cost, and served-model metadata.
  `[CE 19]`
- **A2. Small API**, e.g. `cache.call(key, fn, replicate=0, attempt=0, tags={...})`.
  `[CE 20]`
- **A3. Sync and async**, both safe for parallel calls. The sync API must work in Jupyter
  notebooks, where an event loop is already running (WordSpace needed `CompatTaskGroup`
  for this). `[CE 20] [WS]`
- **A4. Every result says whether it was cached or recorded**, along with its metadata,
  so callers can add up hits, misses, cached cost, and uncached cost. All three Python
  projects build these totals by hand. `[WS] [SC] [T]`
- **A5. Hooks:** before and after each recorded call (e.g. a spending ledger or limit),
  and whenever an entry is used (e.g. for run manifests or cost reports). `[T]`
- **A6. Few dependencies, Python 3.10 or later, MIT-compatible license**, with full type
  hints that pass a strict type check. `[CE 21] [WS] [SC]`
- **A7. Quiet logging by default**, with an option for one line per hit and per miss.
  `[WS]`
- **A8. (later) Batches.** Batched requests (e.g. embeddings) can be cached item by item,
  so a growing vocabulary doesn't miss whenever the batch changes. `[SC]`

### Tools for analysis and maintenance

- **T1. Search entries** by label, tag, or metadata without calling a model. `[CE 22]`
- **T2. Run manifests** list the entry hashes a run used. `[CE 23] [T]`
- **T3. Prune** entries and stored inputs that no recent manifest uses. Prune nothing if
  the run that produced the manifest failed, as Threshold does with `cache:prune`.
  `[CE 24] [T]`
- **T4. Cost and latency report:** what a run (or a set of entries) would have cost, and
  how long it would have taken, without the cache. `[T] [SC] [WS]`
- **T5. Verify:** every entry parses and matches its hash and key, there are no leftover
  locks or temporary files, and no known secret value appears anywhere. `[T]`
- **T6. (later) Explain a miss** by showing which field differs from the nearest entry
  with the same label. `[CE 25]`
- **T7. (later) Command-line tool** for stats, showing entries, verifying, pruning, and
  migrating. `[CE 26] [T]`
- **T8. (later) Migrations:** rewrite entries to a new entry format, and import legacy
  caches (WordSpace `.txt`, Threshold `.json`) when a project moves onto llmemo. `[WS] [T]`
- **T9. (later) Maintain failure records:** delete saved failures once the call has
  succeeded, and mark failures that keep happening for replay (WordSpace's
  `maintain_exception_cache`). `[WS]`

### Cross-language

- **X1. A written format spec** covers key canonicalization, hashing, entry schema,
  directory layout, and versioning, independently of any language. Both packages
  implement it. `[user]`
- **X2. Shared test vectors.** These are key/hash pairs and entry files kept in the repo,
  and both test suites must pass them. `[user]`
- **X3. No language-specific encodings** (e.g. Python `Decimal`, enum `repr`, or
  datetimes with local offsets) in keys or entries. Timestamps are UTC ISO 8601 with
  milliseconds and `Z`. `[user]`
- **X4. A repository layout with room for both packages** and the spec they share. `[user]`

## Repository layout (planned)

```
docs/           Human- and agent-facing design docs
spec/           Language-neutral format spec and test vectors (X1, X2)
python/         The Python package (or the repo root; see Open questions)
typescript/     Future TypeScript package
```

## Documentation

- [docs/prior-art.md](docs/prior-art.md): how each existing project caches LLM calls,
  what worked, what didn't, and how each could move onto llmemo.
- [docs/cross-language.md](docs/cross-language.md): where Python and TypeScript disagree
  on JSON, hashing, and files, and how the format avoids those differences.
- [CLAUDE.md](CLAUDE.md): instructions for AI agents working in this repo.

## Decisions

Newest at the bottom. Include the date and a one-line reason.

| Date | Decision | Why |
|---|---|---|
| 2026-11-20 | Python package first. ConceptExpansion is the first user | Its simulated-playtesting harness is waiting for a cache |
| 2026-11-20 | SoCloverAI, Threshold, and WordSpace inform the requirements but aren't changed now. Each should be able to adopt llmemo later | Learn from all their caches without taking on migrations yet |
| 2026-11-20 | The format is language-neutral, so a TypeScript package in this repo can share it | Threshold is TypeScript, and the same caching approach applies there |

## Open questions

1. **Default mode.** ConceptExpansion wants read-write by default (CE 15). Threshold
   defaults to replay-only, so that a plain test run never spends money. Should a default
   exist at all, or should the caller always choose (with M5 for overrides)?
2. **Replicate vs. variant.** Is an integer replicate enough (CE 3), or is Threshold's
   free-form `variant` string also needed (e.g. a branch ID when rerolling a turn)?
3. **Readable prompts.** JSON turns newlines into `\n`, so long prompts are hard to read
   in diffs. WordSpace's text format kept real newlines. Would storing large inputs as
   plain-text files (S4) solve this?
4. **Embeddings.** Are they in scope for the first release? SoCloverAI's embedding cache
   holds 60k entries in 2.1 GB, so it may need a compact encoding or a cache that isn't
   committed (S11).
5. **Python package location:** the repo root or `python/`? The devcontainer currently
   runs `pip install -e .` from the root.
6. **Distribution:** publish to PyPI (the `llmemo` name is unchecked), or install from
   this repo's git URL?
7. **Folder depth:** one level of 2-hex-digit folders (256 folders, about 400 entries
   each at WordSpace's size) or two levels?
8. **License:** MIT, like the other projects?
9. **A separate module later** for the rate limiting, concurrency limits, and retry code
   that each project currently writes for itself?
10. **Build vs. adopt.** ConceptExpansion says the owner will "find or build" the package.
    The caches already tried (LangChain's, litellm's, and diskcache) fail S3. Before
    building, should any other existing library be checked against these requirements?

## Next steps

1. **The owner reviews the requirements and open questions.** Record answers in Decisions.
2. **Write the format spec** in `spec/` (X1): key structure, canonicalization, hashing,
   entry schema, layout, stored inputs, and versions. Add test vectors (X2).
3. **Set up the Python package:** license, layout (open question 5), and tooling. The
   owner's other Python projects use uv, ruff, mypy (strict), and pytest.
4. **Build the core:** hashing, file and memory stores, `call()` (sync and async), the
   read-write and replay modes, atomic writes, and locking. Tests use fake callables and
   never make paid calls.
5. **Hand off to ConceptExpansion** for its harness phase 1, then add the requirements
   that phase needs next (dry run, manifests, prune).
