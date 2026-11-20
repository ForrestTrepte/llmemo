# Prior art: how the owner's projects cache LLM calls

This doc surveys the existing caches that llmemo's requirements come from. Back to the
[README](../README.md). It describes each repository as of 2026-11-20. These repos aren't
being changed for now, so this is a snapshot. Requirement IDs (K1, S5, …) refer to the
README.

## Summary

| | WordSpace | SoCloverAI (GenerateClues) | SoCloverAI (GenerateKeywordCards2) | Threshold | ConceptExpansion |
|---|---|---|---|---|---|
| Language | Python | Python | Python | TypeScript | Python (planned) |
| Cache code | `llm_cache.py` | LangChain `InMemoryCache` subclass | litellm `Cache(type="disk")` | `src/sim/llm/cache.ts`, `src/node/file-cache-store.ts` | none yet; waiting for llmemo |
| Key | `str()` of a frozen dataclass, with fields equal to their default left out, plus the prompt | `"trial N ::: prompt ::: llm_string"` | litellm's internal key; the trial is passed in the `user` param | `{promptId, attempt, variant?, request}` as canonical JSON | caller-supplied (CE 2) |
| Hash | SHA-1 | none (the string is the dict key) | litellm's | SHA-256 | SHA-256 (CE 1) |
| Storage | one `.txt` per entry, flat directory | one JSON file holding everything | diskcache: SQLite plus pickled `.val` files | one `.json` per entry, flat directory | one JSON file per entry, hash-prefix folders (CE 11) |
| Committed | yes | yes | yes (completions); embeddings are gitignored | yes | yes |
| Size | 103,834 entries, 517 MB | 1,610 entries; separately 60,052 embedding files, 2.1 GB | 75 MB | 879 entries, 13 MB | — |
| Parallel safety | none (async calls that miss together all pay) | none | diskcache's | lock files, temp file and link, first writer wins | required (CE 13) |
| Modes | read-write; global "don't read" flag | read-write | read-write | record / replay-only (default) | read-write, cache-only, refresh, dry run |
| Failures | `.exception` files for diagnosis; `.inject` replays one | not cached | not cached | invalid replies cached; transport errors never | configurable (CE 10) |

## WordSpace

Python. The code is in `llm_cache.py`, `llm.py`, `llm_cache_maintenance.py`, and `shared_types.py`.

**How it works**

- `InvokeCacheKey` is a frozen dataclass holding the model, input approach, temperature,
  reasoning level, the JSON flag, the output JSON schema, and the trial number. Every
  field must have a default. `key_to_str()` leaves out fields equal to their default and
  then calls `str()` on the remaining dict.
- The prompt is `system + "\n\u001f\n" + user`. The file name is
  `sha1(key_str + "\u001f" + prompt)` followed by `.txt`.
- Each file has `Key:`, `Prompt:`, `Response:`, and `Metadata:` sections, separated by the
  `\u001f` (unit separator) character. The prompt and response appear as real text with
  real newlines, so entries are easy to read.
- The metadata is a versioned pydantic model (`InvokeMetadata`, version 3): a reasoning
  summary, token usage (input, output, reasoning, and cached tokens, plus the service
  tier), cost as a `Decimal`, and elapsed seconds.
- An in-memory dict sits in front of the disk, and the cache counts hits and misses.
- `invalidate_cache` is a global flag that skips reading from disk. `rewrite_cache_files`
  rewrites each entry in the newest metadata format as it is read.
- When a call fails, the exception is saved next to the entry's path as `.exception`, and
  repeated failures as `(1).exception`, `(2).exception`, and so on. Renaming one to
  `.inject` makes later calls raise that saved exception instead of calling the API. There
  are 91 `.inject` files: 39 refusals, 38 replies cut off at the token limit, 13 timeouts or
  connection errors, and 1 HTTP 400. `maintain_exception_cache()` deletes
  exceptions for calls that later succeeded, and promotes failures that keep repeating to
  `.inject`.

**Lessons**

- Leaving out default-valued fields lets the key gain a field without invalidating old
  entries. **But** it ties the hash to the defaults in the code. For example, the
  `model` field defaults to `GPT_4O`, and entries recorded with that default store only
  `Key: {'trial': 3}`. If the default ever changed, calls with the new default would
  silently hit entries recorded with the old model. llmemo's K8 keeps the ability to add
  fields while removing this risk: a field is left out only when it is absent.
- `str(dict)` is Python's `repr`. Its output depends on enum reprs, float formatting, and
  quoting rules, so no other language can reproduce the hash (K1, X3).
- A flat directory with 104k files is slow to browse, and GitHub's web UI lists only the
  first 1,000 (S2).
- Writes open the target file directly, so a crash leaves a truncated entry (S5). Two
  coroutines that miss the same key both pay for the call (S6). Reads don't compare the
  stored key with the requested one (K10).
- Saving and injecting failures is useful (E5, T9). Under llmemo's rules, most of these
  (refusals and cut-off replies) are replies that arrived but are invalid, so they would be
  cached as ordinary entries. The timeouts show that a transport failure is sometimes worth
  replaying on purpose too. One example is a request that always times out because its
  output is too long.

**Retrofitting.** The `.txt` files hold the key string and the full prompt, so an
importer could convert each entry to llmemo's format. It would have to rebuild the
structured key from the `repr` string and map enum names (T8). `invoke_llm()` would call
`cache.call()` instead of using `LLMCache` directly. Exception injection would map to E5.

## SoCloverAI

Python. It has three generations of caching, one per subproject.

**GenerateClues (oldest)**

- `simple_llm_cache.py` subclasses LangChain's `InMemoryCache`. It stores everything in one
  `llm-cache.json`, which is rewritten in full on every update. The key is
  `f"trial {trial} ::: {prompt} ::: {llm_string}"`. Here `llm_string` is LangChain's own
  rendering of the model parameters (e.g. `[('_type', 'openai-chat'), ('model_name',
  'gpt-4'), ('temperature', 0.5)]`). Values are LangChain-serialized `Generation` objects.
- `llm_cache_stats_wrapper.py` wraps any LangChain cache to count hits, misses, and
  tokens for each model.
- Embeddings go through LangChain's `CacheBackedEmbeddings` with a `LocalFileStore`. That
  makes 60,052 files (2.1 GB, committed), each named by the model plus a UUID5 of the
  text, and each holding a raw JSON array of floats.

**GenerateKeywordCards2 (current)**

- Completions use litellm's global disk cache (`.litellm_cache/`, committed, 75 MB).
  Underneath it is diskcache, which keeps a SQLite `cache.db` with `-wal` and `-shm`
  files plus pickled `.val` files in hash-prefix folders.
- The trial number is passed to the API as `user=f"trial_{trial}"`, purely so that
  litellm's key differs from one trial to the next.
- A hit is detected through `response._hidden_params["cache_hit"]`. `LlmMetadata`
  totals hits, misses, cached cost, and uncached cost, and these totals are part of how
  the project reports on experiments (A4, T4).
- Embeddings have their own diskcache (`.litellm_embeddings_cache/`, gitignored because
  it is large and can be regenerated). It is keyed per word, so a growing vocabulary
  doesn't miss on every new batch (A8). diskcache's default 1 GiB size limit silently
  evicted old entries (only about 47k embeddings fit), so the limit was raised to 20 GB
  by hand.

**Lessons**

- Committing SQLite with its WAL files and pickle files produces binary diffs and
  unmergeable conflicts. diskcache's pickle loading has an advisory against it,
  GHSA-w8v5-vhqr-4h9v, that the project accepted as a risk (S3).
- Depending on a library's internal cache is fragile. litellm 1.105.0's cache-hit path
  imported proxy code that needed `fastapi`, so every cached call failed. litellm is now
  pinned to one minor line, and the AGENTS.md says to check the cache path by hand after
  any upgrade.
- A one-file JSON cache rewritten on every call doesn't scale, and it conflicts on every
  merge (S1, S3).
- Passing the trial number through an API parameter is a workaround for a key that the
  caller can't control. llmemo's K2 and K3 avoid it.
- Different data needs different commit policies: completions are committed, while
  embeddings aren't (S11).

**Retrofitting.** `generate_async()` and `generate_structured_async()` would call
`cache.call()` around `acompletion()`, with the litellm cache turned off. `LlmMetadata`
would be built from llmemo's cached/recorded flag and metadata (A4). The existing litellm
entries can't be imported easily, since the pickled responses are tied to litellm's
classes. Re-recording is likely simpler.

## Threshold

TypeScript (Node for tests, plus the browser). The design is in `Architecture.md` (the
"LLM cache" and "LLM output handling" sections). The code is in `src/sim/llm/cache.ts`,
`canonical.ts`, `client.ts`, `provider.ts`, `src/node/file-cache-store.ts`,
`cache-prune.ts`, and `spend-ledger.ts`.

**How it works**

- The key is `{ promptId, attempt, variant?, request }`. `request` is the exact
  OpenRouter request body: model, pinned provider routing, messages, response schema,
  and parameters. Any change to a template or an input changes the key, so no separate
  prompt version is needed. `promptId` labels entries but isn't needed for uniqueness.
  `variant` is a free-form string set on the client (`llm.withVariant("trial-3")`), so
  simulation code doesn't need to know about it.
- The hash is the SHA-256 of canonical JSON, computed with Web Crypto so it runs in Node
  and the browser. Canonical JSON means keys sorted at every level and `undefined`
  values left out, then `JSON.stringify`.
- Each entry, `llm-cache/<hash>.json`, holds `entryFormat: 1`, the full key, and the
  response. The response contains the content, refusal, finish reason, served model and
  provider, usage (input, output, reasoning, cache-read, and cache-write tokens), and cost
  in USD. The entry also records `latencyMs`, `recordedAt`, `codeVersion`
  (e.g. `5203929-dirty`), and `recordedBy` (`claude` when `CLAUDECODE=1` is set, otherwise
  `developer`, plus `browser` and `save` in other contexts).
- Every read enforces a 1 MB size limit, validates the schema with zod, and compares the
  stored key with the requested one.
- There are two modes, record and replay, and replay is the default. A miss in replay
  raises `CacheMissError`, which names the prompt, attempt, variant, and hash, and says
  to rerun with `LLM_CACHE=record`.
- The `CacheStore` interface has three methods: `get`, `claim` (a lock), and `put`
  (first writer wins). `MemoryCacheStore` serves the browser and tests, and
  `FileCacheStore` serves Node.
  - A lock is a `<hash>.lock` file created with the `wx` flag. The holder refreshes its
    modification time before each API request. A lock older than the API timeout plus 10
    seconds is taken over, and stale locks and `.tmp` files are deleted at the start of
    each run.
  - An entry is published by writing a temp file and then calling `link()`, which fails
    if the entry already exists. If another writer won, the loser reads the winner's
    entry and uses it.
- The client validates every reply on every use. An invalid reply counts as a failed
  attempt, and the next attempt is a new key. Transport errors are retried within the
  same attempt (1 s and 2 s waits, or 2–16 s and `Retry-After` for HTTP 429) and are
  never cached.
- Hooks: `before` and `after` each recorded call feed the spending guard ($0.50 per run)
  and the committed ledger `llm-spend/<branch>/<developer>.jsonl`. `onEntryUsed` feeds the
  test usage reporter and pruning.
- `pnpm cache:prune` runs the whole test suite in replay mode, collects the hashes it
  used, and deletes the rest, but deletes nothing if any test failed. A test fails if
  `llm-cache/` grows past 1 GB.
- In the browser, each game gets its own in-memory cache in record mode. A save's logs
  hold every call, so `cacheFromLogs()` can rebuild that game's cache, and a save can be
  replayed offline.
- The VS Code settings leave `llm-cache/` out of search and the file watcher.

**Lessons.** Most of llmemo's storage, mode, and safety requirements come from Threshold:
K4, K5, K10, E1, E3, S5, S6, S8, M2, A5, T2, T3, and T5. The README's notes on
committing and pruning carry over unchanged. Pruning shrinks the working tree, not the
repository. Git compresses entries well (about 20× when packed). A flat directory hits
GitHub's 1,000-file listing limit, which is why S2 uses folders.

**Retrofitting.** Threshold would use a future TypeScript llmemo package. Its
`CacheStore` and `LlmClient` already have the right shape. Its `canonicalJson()` (sorted
keys plus `JSON.stringify`) is probably already equivalent to RFC 8785 for the data it
hashes (see [cross-language.md](cross-language.md)). The changes would be llmemo's key
structure, hash-prefix folders, and the shared entry schema. Because the key structure
changes, its existing entries would be rehashed and moved by an importer (T8).

## ConceptExpansion

Python, planned. The plan is in `docs/simulated-playtesting.md`, and the README's
Decisions table records that results are stored as a committed cache of LLM calls.

- It will run simulated games of the board game *Concept* by calling `claude -p`
  headless as a subprocess, on the owner's Claude subscription. OpenRouter may come
  later. So the subprocess backend is required (CE 19, A1).
- The plan suggests `sim/cache/` for entries and `sim/runs/<run-id>.json` for each run's
  settings and games, so run manifests matter (CE 23, T2).
- Large reference docs (`board-icons.md` and `pictured-elements.md`) go into every call,
  so storing large inputs once matters (CE 12, S4). Editing them invalidates the whole
  cache, which is expected.
- The Pro subscription's usage limits are tight, so a dry run that counts misses and
  estimates usage before a run matters (CE 18, M4).
- Reruns must make exactly the same calls, so the harness avoids unseeded randomness.
  That determinism is the caller's job (see the README's Non-goals).
- The cache will contain the answers to the game. The harness keeps them away from the
  guesser model, and llmemo doesn't need to do anything special for this.

ConceptExpansion's 26 cache requirements are all included in the README's requirements,
tagged `[CE n]`.

## Patterns that repeat across projects

- **Trial or replicate numbers** appear in all four projects (K3).
- **Totals of hits, misses, and cached vs. uncached cost** appear in all three Python
  projects and in Threshold's reporter (A4, T4).
- **Semaphores, per-provider concurrency limits, and rate-limit retries** appear in
  WordSpace and SoCloverAI, implemented almost identically. They're outside llmemo's
  scope, and would go in a separate module (README Decisions).
- **Jupyter compatibility** matters in WordSpace and SoCloverAI, where a running event
  loop breaks `asyncio.TaskGroup` (A3).
