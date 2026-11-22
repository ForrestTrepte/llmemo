# llmemo

A record-and-replay cache for LLM calls, stored as plain files that are committed to git.

**Status (2026-11-21): version 0.1.0 of the Python package is built** to the
[format spec](spec/README.md): keys and hashing, the stored form, file and memory stores,
`call()`/`acall()`, record and replay modes, refresh, failures, stats, hooks, locking, and
the spending limit and spending ledger, and the built-in Claude CLI and OpenRouter
backends (A10). It passes every shared test vector. The maintenance tools (T3–T5) aren't
written yet. This README holds the project's goals and requirements. The first implementation will be a Python package, and its first
user is [ConceptExpansion](https://github.com/ForrestTrepte/ConceptExpansion). A TypeScript
package that reads and writes the same cache format may follow, in this same repository.

## Using llmemo

Install the Python package from this repository. There's no PyPI release.

```
uv add "llmemo @ git+https://github.com/ForrestTrepte/llmemo@v0.1.0#subdirectory=python"
```

Your `uv.lock` pins the exact commit. Change the tag in that command to upgrade. The package's
[readme](python/README.md) and [examples/quickstart.py](python/examples/quickstart.py)
show the API.

```python
from llmemo import Cache, ClaudeCli

cache = Cache("sim/cache", mode="replay", spend_limit=1.00)  # commit sim/cache/
claude = ClaudeCli()
request = ClaudeCli.request(
    "claude-sonnet-5-5",  # a full model ID; aliases like "sonnet" are rejected
    [rules_doc, "\n\nNow play as player 2."],  # parts, each stored once
    system=[role_prompt, setting_doc],  # required; parts are concatenated
    json_schema=MOVE_SCHEMA,  # optional structured output
)
result = cache.call(request, claude, label="player-move", variant="trial-3", attempt=0)
move = parse_and_validate(result.reply["structured_output"])  # your code, on every use (E1)
```

Things to know:

- **Choose a mode.** There is no default. Use `record` while developing and `replay` in CI
  and tests, where a miss raises `CacheMiss` instead of calling the model. Before the first
  recording the cache folder doesn't exist, and replay treats it as empty.
  `LLMEMO_MODE=record` or `LLMEMO_MODE=replay` overrides the code. Commit the cache folder.
  It creates its own `.gitattributes` and `.gitignore`.
- **Set a spending limit** (`spend_limit=`, or a shared `SpendLimit`). It stops a runaway
  retry loop. For `claude -p` on a subscription, the CLI's reported API-equivalent cost
  counts toward it (M6). To keep a committed record of spending, pass `ledger=Ledger()`
  (M7). Its file is named after git's `user.name`, or `LLMEMO_DEVELOPER` if that's set.
- **Claude CLI requests** ([spec/backends.md](spec/backends.md) §1) need a full model ID
  and a `system` prompt, which replaces Claude Code's own. Each call runs in an empty
  temporary folder with no tools, MCP servers, settings, or project files, so the reply
  depends only on the request. Build the request with `ClaudeCli.request`.
- **Split large shared text into parts.** `prompt` and `system` may be lists of strings.
  Each long part, such as a reference document, is stored once as a `.txt` file however
  many entries use it (S4).
- **The Claude CLI reply is not the CLI's whole output.** The entry keeps a fixed list of
  members of the CLI's JSON (`result`, `structured_output`, `stop_reason`, `usage`,
  `modelUsage`, and a few more; see E1). The rest is session IDs, timings, and the login's
  credential expiry, which don't belong in committed files.
- **`variant` and `attempt` are part of the key.** Use `variant` for independent samples or
  rerolls, and `attempt` for retries after invalid output, so a replay repeats the same
  sequence of failures and successes (K3, K4).
- **Stop cleanly at a usage limit.** When the subscription's limit refuses a call,
  `ClaudeCli` raises `UsageLimitReached` (a `BackendError`) with `resets_at`, a UTC
  `datetime`, or `None` if the CLI didn't say. It's never saved as a failure. Stop the run,
  and rerun after `resets_at`: everything recorded so far replays from the cache.
- **Parse and validate the reply every time.** llmemo stores the raw reply. A reply that
  arrived but is invalid is cached like any other (E5).

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
   SDK, an OpenAI-compatible endpoint, or a subprocess. It ships two ready-made backends,
   the Claude CLI (`claude -p`) and OpenRouter (A10). The cache itself doesn't depend on
   either.
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
- **Prompt templating, output parsing, and adapters for every provider.** llmemo stores
  the raw reply, and the caller parses and validates it every time. Beyond the two
  built-in backends (A10), callers write their own callable.
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
ConceptExpansion's [cache requirement](https://github.com/ForrestTrepte/ConceptExpansion/blob/06ca8d250907a459d544c3de515b86e8eff7efda/docs/simulated-playtesting.md#cache-requirements)
*n* (that list was removed from ConceptExpansion once llmemo met it, so the link is to the
last commit that has it), and `[T]`, `[WS]`, and `[SC]` stand for Threshold, WordSpace, and SoCloverAI.
**(later)** marks requirements that the first release doesn't need.

### Keys

- **K1. Stable hash.** The entry ID is the SHA-256 of the key's canonical JSON, as
  lowercase hex. Python and TypeScript must produce identical bytes, so canonicalization
  follows RFC 8785 ([spec/format.md](spec/format.md) §2). It never uses Python's `hash()`,
  `repr`/`str` of objects, or pickle. `[CE 1] [T]`
- **K2. The caller defines the key.** The caller passes the key as plain JSON data:
  backend, exact model ID (not an alias), system prompt, messages, tools, every setting,
  and the output schema. llmemo doesn't guess at fields. `[CE 2] [T]`
- **K3. Variant** in the key: an optional free-form string that separates otherwise
  identical calls, e.g. `trial-3` for independent samples, or a branch ID when rerolling.
  WordSpace and SoCloverAI call this a "trial", and Threshold calls it a `variant`.
  `[CE 3] [WS] [SC] [T]`
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
  re-recording. A built-in backend may keep only a fixed list of the reply's members
  when the rest describes the session rather than the reply. The Claude CLI backend
  does this: it keeps the members listed in [spec/backends.md](spec/backends.md) §1.3
  unchanged and drops session IDs, timings, and the login's credential expiry (E4).
  `[CE 7] [T]`
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
  `.inject` files. A usage limit (e.g. a subscription's 5-hour limit) has its own error,
  with the reset time when the backend reports it, and is never saved, since it's about the
  account rather than the request. `[CE 10] [T] [WS] [user]`
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
- **S4. Large text is stored once, with real newlines.** Any string in a key or reply
  that contains a newline, or is longer than 512 UTF-8 bytes, is stored as a
  plain `.txt` file named by its SHA-256, in the same 3-hex-digit folders as entries. The
  entry refers to the file instead of holding the text. The same text is stored only
  once, wherever it appears, and it's readable in diffs. Details:
  - The entry hash covers the full text, not the reference. So the rule for which
    strings move to files can change without invalidating any entries.
  - Each text file is checked against its hash when read.
  - Git must not convert the files' line endings, so they are marked `-text` in
    `.gitattributes`.
  - Text files are written before their entry. They're named by content, so writing one
    twice is harmless.
  - Entries aren't self-contained, which is accepted. Pruning (T3) also removes text
    files that no entry uses.
  - There is no automatic sharing of common prefixes. To share a long prefix, callers
    pass the request as a list of parts (e.g. separate messages), and each part is stored
    once.
  `[CE 12] [user]`
- **S5. Atomic writes.** Each entry is written to a temporary file and then linked into
  place, which fails if the entry already exists. So a reader never sees a partly written
  entry, and if two writers record the same key, the first one wins. When that happens, a
  warning names the entry, since someone paid for a duplicate call. `[CE 13] [T] [user]`
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
- **S12. Embeddings.** Embedding responses (arrays of floats) are supported from the
  first release, at SoCloverAI's scale: 60k vectors, which take 2.1 GB as JSON text.
  Each vector is stored as a little-endian binary file named by its SHA-256, in the same
  folders as text files (S4), and the entry refers to it. The default is float64
  (`.f64`, exact, about 2.8× smaller than JSON). float32 (`.f32`, about 5.6× smaller) is
  used when the caller opts in, or when the provider already returns float32. Large
  embedding caches are expected to be gitignored (S11). `[SC] [user]`

### Modes

There is no default mode. The caller must choose one. `[user]`

- **M1. Read-write:** a miss calls the backend and records the result. `[CE 15] [T]`
- **M2. Cache-only (replay):** a miss raises an error that names the label, variant,
  attempt, and hash, and says how to record the entry. CI and tests use this mode. `[CE 16] [T]`
- **M3. Refresh** selected entries, chosen by label, tag, or hash, by calling again and
  replacing them. `[CE 17] [T] [WS]`
- ~~**M4. Dry run:** count misses and estimate their cost without calling.~~ `[CE 18]`
  Withdrawn on 2026-11-20. No project has one. In a multi-step run, every call after the
  first miss depends on the missing reply, so a dry run can't count them. A
  `contains(key)` check (A9) covers the simple case.
- **M5. Configurable from the environment**, so the mode can be switched without code
  changes (e.g. `LLMEMO_MODE=record`, like Threshold's `LLM_CACHE=record`). This is an
  override only. It doesn't create a default. `[T]`
- **M6. Spending limit:** stop calling the backend once a run has spent a set amount, as
  protection against a runaway retry loop. Each project sets its own limit. llmemo's own
  tests are capped at $0.10 per run. A Claude subscription run through `claude -p`
  reports what the call would have cost through the API, not money actually spent, but
  that figure counts toward the limit, since it's the best available measure of usage.
  `[T] [user]`
- **M7. Spending ledger:** a committed, append-only record of every backend call that
  reported a cost, in `llm-spend/<branch>/<developer>.jsonl`, so spending over time is
  visible by branch, developer, who ran it (`claude`, `developer`, `ci`), model, and
  billing (API money or a subscription's API-equivalent cost). Each file has one writer
  on one branch, so ledgers never conflict when branches merge. Snapshots of an API key's
  total, as the provider reports it, show spending the call rows don't see. llmemo's own
  development spending is kept this way too. Specified in
  [spec/format.md](spec/format.md) §10. `[T] [user]`

### API

- **A1. Wraps any callable.** llmemo wraps any function from request to response, such as
  an HTTP SDK or a subprocess call (ConceptExpansion needs the subprocess case). The
  callable returns the raw reply plus optional usage, cost, and served-model metadata.
  `[CE 19]`
- **A2. Small API**, e.g. `cache.call(key, fn, variant=None, attempt=0, tags={...})`.
  `[CE 20]`
- **A3. Sync and async**, both safe for parallel calls. The sync API must work in Jupyter
  notebooks, where an event loop is already running (WordSpace needed `CompatTaskGroup`
  for this). `[CE 20] [WS]`
- **A4. Stats.** Every result says whether it was cached or recorded, along with its
  metadata. The package keeps running totals of hits, misses, cached cost (what replayed
  calls originally cost), uncached cost (what this run spent), and tokens. The totals can
  be broken down by label and model, reset, and added together across runs. All three
  Python projects currently build these totals by hand. `[WS] [SC] [T] [user]`
- **A5. Hooks:** before and after each recorded call (e.g. a spending ledger or limit),
  and whenever an entry is used (e.g. for run manifests or cost reports). `[T]`
- **A6. Few dependencies, Python 3.12 or later, and an MIT license**, with full type
  hints that pass a strict type check. All three Python users already run 3.12. CE 21
  asked for 3.10 or later. `[CE 21] [WS] [SC] [user]`
- **A7. Quiet logging by default**, with an option for one line per hit and per miss.
  `[WS]`
- **A8. (later) Batches.** Batched requests (e.g. embeddings) can be cached item by item,
  so a growing vocabulary doesn't miss whenever the batch changes. `[SC]`
- **A9. `contains(key)`** checks whether an entry exists without calling the backend or
  changing the stats. `[CE 18]`
- **A10. Two built-in backends**, optional, alongside any callable (A1):
  - **Claude CLI:** `claude -p` run headless as a subprocess, on the owner's Claude
    subscription. Recorded with full model IDs. The usage and cost it reports are
    stored, and the CLI version is stored as metadata outside the key (K6).
    ConceptExpansion's playtests need this backend.
  - **OpenRouter:** its chat-completions HTTP API, with the key in an environment
    variable. This works with any model OpenRouter offers.
  `[CE 19] [T] [user]`

### Tools for analysis and maintenance

- ~~**T1. Search entries** by label, tag, or metadata without calling a model.~~ `[CE 22]`
  Withdrawn on 2026-11-20. No project searched its cache this way. Iterating over entries,
  which pruning and verification need anyway, is enough.
- ~~**T2. Run manifests** list the entry hashes a run used.~~ `[CE 23] [T]` Withdrawn on
  2026-11-20. The `onEntryUsed` hook (A5) already gives callers the hashes a run used.
  Threshold writes them to a file this way, so it has no separate manifest format.
- **T3. Prune** entries and stored text that aren't in a given set of used hashes. The
  set is collected through the hook (A5) during a full, passing run, as Threshold does
  with `cache:prune`. `[CE 24] [T]`
- **T4. Cost and latency report:** what a run (or a set of entries) would have cost, and
  how long it would have taken, without the cache. `[T] [SC] [WS]`
- **T5. Verify:** every entry parses and matches its hash and key, there are no leftover
  locks or temporary files, and no known secret value appears anywhere. `[T]`
- **T6. (later) Explain a miss** by showing which field differs from the nearest entry
  with the same label. `[CE 25]`
- **T7. (later) Command-line tool** for stats, showing entries, verifying, pruning, and
  migrating. `[CE 26] [T]`
- ~~**T8. Migrations:** rewrite entries to a new entry format, and import legacy caches
  when a project moves onto llmemo.~~ `[WS] [T]` Withdrawn on 2026-11-20. A project that
  adopts llmemo writes its own one-off conversion, or re-records. Entries stay versioned
  (E3), so a future format change can still be handled.
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

## Repository layout

```
docs/           Human- and agent-facing design docs
spec/           Language-neutral format spec and test vectors (X1, X2)
llm-spend/      This repo's own spending ledger (M7), written when live tests record
python/         The Python package
typescript/     Future TypeScript package (not created yet)
.github/        CI workflow
```

## Development

The Python side uses [uv](https://docs.astral.sh/uv/) for everything. The codespace
installs it and runs `uv sync` in `python/`, which creates `python/.venv` with the dev
tools. `python/.python-version` pins the interpreter, and `uv.lock` pins every package.

From `python/`:

```
uv run pytest
uv run mypy                # strict; also checks spec/tools
uv run ruff check ..       # the repo root, so spec/tools is included
uv run ruff format ..
```

To regenerate and check the test vectors, see [spec/README.md](spec/README.md).

`tests/test_live.py` makes real calls through the built-in backends and records them in the
committed cache `python/tests/cache/`: OpenRouter calls when `OPENROUTER_API_KEY` is set,
and Claude CLI calls when `claude` is on the PATH outside CI (one small Haiku call on the
subscription, plus one with an unknown model, which uses nothing). Otherwise it replays
that cache, which is what CI does. `tests/test_examples.py` runs each script in
`python/examples/` in replay mode against its committed cache in `examples/cache/`. After
changing an example, record its new entries with `LLMEMO_MODE=record` and commit them. Every live call in a run shares one $0.10
`SpendLimit` (`tests/conftest.py`), and each call they pay for adds a row to this repo's
ledger in `llm-spend/`. Commit the ledger along with the entries it paid for. To re-record an entry, delete its files and rerun in
the codespace.

**CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs on GitHub Actions for
every push to `main`, every pull request, and on demand from the Actions tab. It runs the
commands above, then regenerates the vectors and fails if any file changes, then runs the
JavaScript vector check. CI has no API keys and makes no LLM calls: tests that need a
live call run in cache-only mode (M2) against committed entries, or are skipped. Live
calls run only in the codespace. Results show on each pull request and in the Actions
tab, or with `gh run list` and `gh run view --log-failed`.

**Releasing.** In one commit, set `version` in `python/pyproject.toml`, run `uv lock`, and
change the tag in both install commands (this README and `python/README.md`). Write the docs
as they should read once the release exists. After that commit is on `main`, tag it
`v<version>` and push the tag.

## Documentation

- [spec/format.md](spec/format.md): the cache format, with its
  [test vectors](spec/vectors/README.md). This is the contract between the Python and
  TypeScript packages.
- [spec/backends.md](spec/backends.md): the requests the built-in backends take and what
  they store, so both packages make the same keys for them.
- [docs/prior-art.md](docs/prior-art.md): how each of the owner's projects caches LLM
  calls, what worked, what didn't, and how each could move onto llmemo. It also reviews
  existing libraries and tools (vcrpy, litellm, DSPy, Inspect AI, and others) and what
  would go wrong if these projects used them.
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
| 2026-11-20 | No default mode; the caller must choose read-write or cache-only | Neither default is safe for every caller: one spends money unexpectedly, the other fails unexpectedly |
| 2026-11-20 | The key holds a free-form `variant` string, not an integer replicate | It covers trials and rerolls/branches alike |
| 2026-11-20 | Long text must be stored with real newlines | Prompts and replies have to be readable in diffs |
| 2026-11-20 | Embeddings are in scope for the first release | SoCloverAI needs them |
| 2026-11-20 | The Python package lives in `python/` | Leaves room for `typescript/` and a shared `spec/` |
| 2026-11-20 | Install from this repo's git URL for now; no PyPI yet | Simplest way to start |
| 2026-11-20 | Entries go in one level of 3-hex-digit folders (4,096 folders) | About 25 entries per folder at WordSpace's 104k, and under GitHub's 1,000-file listing limit until about 4M entries |
| 2026-11-20 | MIT license | Matches the owner's other projects |
| 2026-11-20 | Rate limiting, concurrency limits, and retries are out of scope | They belong in a separate module, not in this project |
| 2026-11-20 | Research existing solutions before building | Avoid building what already exists |
| 2026-11-20 | The package's tests make real LLM calls on a small budget, with keys from a Codespaces secret. Mock only what real calls can't produce | Real calls catch problems that mocks hide |
| 2026-11-20 | Withdrew dry run (M4), search (T1), run manifests (T2), and migrations (T8) | None was used in an existing project, or simpler features cover them (A5, A9). ConceptExpansion's list updated to match ([ConceptExpansion#1](https://github.com/ForrestTrepte/ConceptExpansion/pull/1)) |
| 2026-11-20 | Python 3.12 or later | Every current Python user runs 3.12 |
| 2026-11-20 | Strings that contain a newline or are long are stored once by hash as `.txt` files, and entries refer to them. Entries need not be self-contained | One mechanism gives real newlines in diffs and stores repeated text once |
| 2026-11-20 | No automatic prefix sharing for now. Callers split requests into parts | Automatic sharing makes the stored files depend on recording order, or breaks text into fragments that are hard to read |
| 2026-11-20 | Built-in backends for the Claude CLI and OpenRouter | ConceptExpansion uses the Claude subscription. OpenRouter covers every other model |
| 2026-11-20 | llmemo's own tests spend at most $0.10 per run. Other projects set their own limit | Keeps real-call testing cheap |
| 2026-11-20 | Embedding vectors are stored as binary files, float64 by default and float32 on request. Large embedding caches are gitignored | Measured on SoCloverAI's 1,536-dimension vectors: 34 KB each as JSON, 12 KB as float64 (exact), 6 KB as float32 (rounds every value by about 1 part in 10⁸). gzip saves only about 15% on floats |
| 2026-11-20 | The OpenRouter key is in `OPENROUTER_API_KEY`, a Codespaces secret declared in the devcontainer | Keeps the key out of the repo |
| 2026-11-20 | ConceptExpansion's requirements 3, 15, and 21 updated to match llmemo ([ConceptExpansion#2](https://github.com/ForrestTrepte/ConceptExpansion/pull/2)) | Its checklist should agree with the package |
| 2026-11-20 | Build llmemo rather than adopt an existing tool | No library or tool researched meets the core requirements: committed and mergeable files, caller-defined keys, a cross-language hash, and `claude -p` support ([docs/prior-art.md](docs/prior-art.md#existing-solutions)) |
| 2026-11-20 | Format version 1 is specified in [spec/format.md](spec/format.md). Canonical JSON is RFC 8785. The key envelope is `{keyFormat, attempt, label?, variant?, request}`, and file member names are camelCase | RFC 8785 is what JavaScript does already (Threshold's `canonicalJson` matches it). camelCase matches Threshold's entries and JavaScript |
| 2026-11-20 | A number whose value is an integer above 2^53 − 1 is rejected, even a float such as `1e16`. NaN, infinities, `-0`, and lone surrogates are rejected too | JavaScript can't tell `1e16` from an integer, so this is the only rule both languages can apply identically |
| 2026-11-20 | Absent and `null` differ. A member is absent only when it isn't there, and Python `None` is `null` | Callers build plain dicts, so no special "absent" value is needed. Closes the question in [docs/cross-language.md](docs/cross-language.md) |
| 2026-11-20 | Strings are stored as text when they contain a line feed or exceed 512 UTF-8 bytes. References look like `{"$llmemo.text": "<sha256>"}`, and caller member names starting with `$…llmemo` get an extra `$` in files | Bytes count the same in every language. Escaping means no caller data is ever refused, and `$schema`/`$ref` are left alone |
| 2026-11-20 | Each entry stores `contentSha256` of its stored reply. A damaged entry is an error (with a fixed code), never a miss | Hand edits to replies are detected, as edited keys are (K10). Treating damage as a miss would hide it |
| 2026-11-20 | Files are published by hard link, with no fallback. Locks are refreshed every 10 s by a timer and taken over after 60 s by renaming | A fallback would let readers see half-written files. A timer keeps long `claude -p` calls from looking abandoned, whatever the backend's timeout |
| 2026-11-20 | Vectors are generated by a Python reference (`spec/tools/`) and checked by an independent JavaScript implementation | Two implementations written from the spec catch places where the spec is ambiguous |
| 2026-11-21 | uv runs everything: the package's tools, the spec tools, and CI. The spec tools run in the package's environment but may not import `llmemo` (a ruff rule enforces this) | One pinned Python and toolset everywhere. The oracle's independence is about imports, not which interpreter runs it |
| 2026-11-21 | CI is GitHub Actions in cache-only mode, with no secrets and no live LLM calls | It can't spend money unattended, and it exercises replay, which is what llmemo is for. Live calls are tested in the codespace |
| 2026-11-21 | One ruff config at the repo root, line length 100, with E501 off. `uv_build` is the build backend | The formatter keeps code short. The long lines left are vector descriptions, clearer unwrapped |
| 2026-11-21 | `LICENSE` is at the repo root, and `python/LICENSE` is a symlink to it. `python/README.md` is a short package readme | GitHub detects the root license, and builds can't include files outside `python/` |
| 2026-11-21 | The modes are named `record` and `replay` (also the values of `LLMEMO_MODE`) | Short, and match Threshold's `LLM_CACHE=record` |
| 2026-11-21 | The public API is `Cache(store, mode=...)` with `call`, `acall`, `contains`, and `entry_id`. A backend is any `request -> Reply \| JSON value` callable. `Vector` marks embeddings. Errors are `CacheMiss`, `FormatError` (with the spec's `.code`), `SpendLimitExceeded`, `BackendError`, and `ReplayedError` | Follows A2 and Threshold's shape. The owner approved it before the core was built |
| 2026-11-21 | Only a `BackendError` raised by the backend is saved as a failure (`failures="save"` or `"replay"`). Other exceptions propagate and are never written | Any other exception is a bug, and its message could hold a secret (E4) |
| 2026-11-21 | Refresh (M3) is a predicate over `CallInfo` (entry ID, label, variant, attempt, tags). It replaces each entry at most once per `Cache`, and only in record mode | A single predicate covers selection by label, tag, or hash. Once per cache means repeated calls in one run hit the new entry |
| 2026-11-21 | The spending limit is checked before each backend call. A `SpendLimit` object can be shared by several caches. A reply with no cost logs a warning once | Calls already running can overshoot it slightly, which is fine for a guard against runaway loops |
| 2026-11-21 | A recorded call returns its reply as a replay would return it (parsed back from the bytes written) | Record and replay runs then see identical values, e.g. `1.0` comes back as `1` in both |
| 2026-11-21 | Within a process, callers waiting on the same entry share a future; across processes they use lock files. Each `FileStore` has one daemon thread that refreshes its locks. Nothing scans the cache at startup, so cleaning up old temp files is a separate `FileStore.cleanup()` | S6 without polling inside one process, and S9 |
| 2026-11-21 | `meta.recordedBy` defaults to `LLMEMO_RECORDED_BY`, else `ci` when `CI` is set, `claude` when `CLAUDECODE=1`, else `developer`. `codeVersion` is set only when the caller passes it (`git_code_version()` helps) | Follows Threshold. Running git on every cache open would be slow in big repos |
| 2026-11-21 | Added a spending ledger (M7), modeled on Threshold's `llm-spend/`, before the backends | Spending should be tracked over time, including while the backends are developed. This is new, not the withdrawn run manifest (T2), which listed the entries a run used |
| 2026-11-21 | Ledger rows are canonical JSON lines with their own `ledgerFormat`, in `<root>/<branch>/<developer>.jsonl`, with names made safe by replacing other characters with `-` | Identical bytes from Python and TypeScript, and paths that work on every file system. Follows Threshold |
| 2026-11-21 | A `call` row is written for every backend call that reported a cost, including a reply that couldn't be recorded and the loser of a recording race (`duplicate: true`). Hits and calls with no reported cost write nothing | The ledger records money spent, and those calls were paid for |
| 2026-11-21 | `meta.billing` (`api` or `subscription`) is an optional metadata member, also copied into ledger rows | Reports can total real money separately from a subscription's API-equivalent cost (M6). An optional member needs no format change |
| 2026-11-21 | `key-total` rows are part of the format now, but only the OpenRouter backend will write them | Only a provider with a spending API can report a key's total |
| 2026-11-21 | The built-in backends are `ClaudeCli` and `OpenRouter`, exported from `llmemo`. Each instance is a backend callable with an `acall` method for `Cache.acall`, and static methods build requests (`ClaudeCli.request`, `OpenRouter.chat_request`, `OpenRouter.embeddings_request`). Their request shapes are specified in [spec/backends.md](spec/backends.md) | Same shape as any caller-written backend (A1). A written request shape lets a TypeScript package share their entries |
| 2026-11-21 | A built-in backend rejects request members it doesn't define. The Claude CLI request requires `system` and a full model ID (aliases are rejected). `prompt` and `system` may be lists of strings, which are concatenated | An ignored member would split the cache without changing the call. Without `system`, Claude Code's own prompt would apply and change with CLI versions. Lists let shared parts be stored once (S4) |
| 2026-11-21 | The Claude CLI backend stores a fixed list of members of the CLI's JSON output (result, structured output, stop reason, usage, and so on), not the whole object | The rest is session IDs, dozens of timings, and the login's credential expiry. A fixed list keeps whatever future CLI versions add out of committed files (E4). This is a narrow reading of E1 / CE 7 |
| 2026-11-21 | Each `claude -p` call runs in a new empty temp folder, with the system prompt in a file, the prompt on stdin, and `CLAUDECODE` removed from its environment. `billing` is `subscription` unless `ANTHROPIC_API_KEY` is set | No project files or `CLAUDE.md` leak into the call, long prompts aren't limited by the command line, and it runs inside Claude Code too |
| 2026-11-21 | The OpenRouter backend reads its key when a call is made. A failure's message keeps only the error body's `error.message`, with the key replaced by `[key]` | Replay needs no key. Error bodies also contain the account's user ID |
| 2026-11-21 | Version 0.1.0, released as the git tag `v0.1.0`. Users install from the tag | Gives ConceptExpansion a readable pin and a fixed point for bug reports. Still no PyPI |
| 2026-11-22 | The ledger's developer name is `LLMEMO_DEVELOPER`, else git's `user.name`, else `GITHUB_USER`, else `unknown`. Never an email address | `GITHUB_USER` came first before, so one person got `ForrestTrepte.jsonl` in a codespace and `Forrest-Trepte.jsonl` elsewhere (ConceptExpansion's feedback). git's name is set wherever the person commits. Rows don't hold the developer, so old files can be renamed or concatenated |
| 2026-11-22 | In replay mode, a cache folder that doesn't exist is an empty cache: lookups miss, and `CacheMiss` says the folder doesn't exist. A folder that exists without `llmemo.json` is still `not-a-cache` | Replaying before the first recording raised `FormatError`, which a harness can't tell from damage (ConceptExpansion's feedback). An existing folder with no marker is the real wrong-folder case |
| 2026-11-22 | A usage limit is failure kind `usage-limit`, raised as `UsageLimitReached` with `resets_at` and `limit_type`. It's never stored, whatever `failures` is. `ClaudeCli` detects it from the CLI's `api_error: "usage_limit_reached"` and reads the reset time from `api_error_params.rate_limit_info`, with no guessing from message text | A harness can stop and resume from the cache (ConceptExpansion's feedback). A stored one would replay after the limit resets. The CLI marks those members internal, so if they change, the call falls back to `http`/`process` |

## Open questions

None right now.

## Next steps

1. **Hand off to ConceptExpansion** for its harness phase 1. Point it to
   [Using llmemo](#using-llmemo) above. Don't edit that repo unless the owner asks.
2. **Maintenance tools:** a spending report from the ledgers (`summarize_spending`
   exists; compare it with the latest `key-total` row, which `OpenRouter.record_key_total`
   writes, to show unseen spending), prune (T3, from hashes collected by `on_entry_used`),
   a cost and latency report (T4, from `Stats`), and verify (T5, the §8 checks on every
   file, plus leftover locks and temp files and known secret values).
