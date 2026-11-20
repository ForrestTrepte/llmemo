# Prior art: existing LLM caches

This doc surveys the existing caches that llmemo's requirements come from: first the
owner's own projects, then [libraries and tools that already exist](#existing-solutions).
Back to the [README](../README.md). It describes each repository as of 2026-11-20. These
repos aren't being changed for now, so this is a snapshot. Requirement IDs (K1, S5, …)
refer to the README.

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
| Modes | read-write; global "don't read" flag | read-write | read-write | record / replay-only (default) | read-write, cache-only, refresh |
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
structured key from the `repr` string and map enum names. This would be a one-off
script, since llmemo doesn't ship importers (T8 was withdrawn). `invoke_llm()` would call
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
  any upgrade. Since then, litellm 1.109 (2026-10-30) has removed the disk cache backend
  entirely (see [litellm](#litellm) below), so SoCloverAI can't upgrade past 1.108
  without replacing its cache.
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
K4, K5, K10, E1, E3, S5, S6, S8, M2, A5, T3, and T5. The README's notes on
committing and pruning carry over unchanged. Pruning shrinks the working tree, not the
repository. Git compresses entries well (about 20× when packed). A flat directory hits
GitHub's 1,000-file listing limit, which is why S2 uses folders.

**Retrofitting.** Threshold would use a future TypeScript llmemo package. Its
`CacheStore` and `LlmClient` already have the right shape. Its `canonicalJson()` builds
the string itself from `Object.keys(value).sort()` and `JSON.stringify` of each scalar.
That matches RFC 8785 for every value llmemo's spec accepts. It doesn't reject what the
spec rejects (`-0`, large integers, lone surrogates), so llmemo's version adds those
checks (see [spec/format.md](../spec/format.md) §1–2). The changes would be llmemo's key
structure, hash-prefix folders, and the shared entry schema. Because the key structure
changes, its existing entries would be rehashed and moved by a one-off script, or
re-recorded.

## ConceptExpansion

Python, planned. The plan is in `docs/simulated-playtesting.md`, and the README's
Decisions table records that results are stored as a committed cache of LLM calls.

- It will run simulated games of the board game *Concept* by calling `claude -p`
  headless as a subprocess, on the owner's Claude subscription. OpenRouter may come
  later. So the subprocess backend is required (CE 19, A1).
- The plan suggests `sim/cache/` for entries and `sim/runs/<run-id>.json` for each run's
  settings and games. The hashes a run used can come from the `onEntryUsed` hook (CE 23,
  A5), so llmemo has no separate manifest format (T2 was withdrawn).
- Large reference docs (`board-icons.md` and `pictured-elements.md`) go into every call,
  so storing large inputs once matters (CE 12, S4). Editing them invalidates the whole
  cache, which is expected.
- The Pro subscription's usage limits are tight. A dry run (M4) was withdrawn because
  every call after the first miss depends on the missing reply. `contains(key)` (CE 18,
  A9) and the spending limit (M6) cover this instead.
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

## Existing solutions

Researched on 2026-11-20 to answer "does something already do this?" (see the README's
Decisions). For each tool, the source was read at its main branch on that date unless
noted. A tool doesn't need to meet every requirement to be useful. The question is whether
it could do the same job for ConceptExpansion, WordSpace, SoCloverAI, and Threshold, and
what would go wrong if it did.

### Conclusion

No existing tool does the core job: a committed, mergeable cache, keyed by data the caller
controls, with a hash that two languages can reproduce. The tools fall into four groups,
and each group misses for its own reason:

- **HTTP record/replay** (vcrpy, Polly.js, retake). These are the closest in spirit:
  record once, replay in CI, fail on a miss. But they can't see `claude -p`, they key on
  HTTP requests the caller doesn't fully control, and their cassettes are one file per
  test.
- **Framework caches** (litellm, LangChain, DSPy, Inspect AI, promptfoo). These are
  speedups for one framework. Each one hashes Python- or framework-specific strings,
  stores pickles or one big file, and doesn't record cost.
- **General function caches** (joblib, diskcache, requests-cache). These cache anything,
  but use pickle and have no parallel-safe recording or LLM metadata.
- **New LLM record/replay libraries** (agent-replay-fixtures, llm-cassette, llm-replay).
  They have the right idea, but each is a few weeks old with one author and no users, and
  none covers storage at scale, cost, or a second language.

Recommendation: build llmemo, and borrow the ideas listed in
[Ideas to borrow](#ideas-to-borrow).

### Summary

| Tool | Kind | Language | Storage | Key and hash | Parallel recording | Cost stored | Fit |
|---|---|---|---|---|---|---|---|
| [vcrpy](https://github.com/kevin1024/vcrpy) + [pytest-recording](https://github.com/kiwicom/pytest-recording) | HTTP record/replay | Python | one YAML cassette per test | HTTP method and URL by default. No hash | no (whole cassette rewritten) | no | partial, for tests only |
| [Polly.js](https://github.com/Netflix/pollyjs) | HTTP record/replay | JS | one HAR file per recording | request fields, hashed | no | no | partial, for tests only |
| [retake](https://github.com/Zecrari/retake) | HTTP proxy | Rust binary | cassettes folder | normalized request | not reviewed | no | poor |
| [litellm](https://github.com/BerriAI/litellm) | framework cache | Python | memory, Redis, or cloud buckets (disk removed) | SHA-256 of Python `repr` fragments | no | no | no |
| [LangChain](https://github.com/langchain-ai/langchain) | framework cache | Python, JS | memory, SQLite, Redis, … | prompt plus `str(sorted(params))` | depends on backend | no | no |
| [DSPy](https://github.com/stanfordnlp/dspy) | framework cache | Python | diskcache (SQLite plus pickle) | SHA-256 of sorted-key JSON | SQLite | no (usage cleared on hit) | no |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | framework cache | Python | one pickle per entry, a folder per model | MD5 of `str()` of pydantic dumps | no | token usage, inside the pickled output | no |
| [promptfoo](https://github.com/promptfoo/promptfoo) | framework cache | TypeScript | one `cache.json` | SHA-256 of the fetch request | no | not checked | no |
| [Vercel AI SDK middleware](https://ai-sdk.dev/docs/advanced/caching) | hook only | TypeScript | whatever you write | whatever you write | — | — | an integration point |
| [joblib.Memory](https://joblib.readthedocs.io/en/stable/memory.html) | function cache | Python | pickle files, a folder per function | joblib hash of pickled arguments | no | no | no |
| [requests-cache](https://github.com/requests-cache/requests-cache) | HTTP cache | Python | SQLite, or one JSON or YAML file per response | request, including body | thread lock only | no | no |
| [agent-replay-fixtures](https://github.com/buxerr/agent-replay-fixtures) | LLM record/replay | Python | one compact JSON file per request | SHA-256 of sorted-key JSON | lock folder, then rename | no | closest, but immature |
| [llm-cassette](https://github.com/gangjig/llm-cassette) | LLM record/replay | Python | one JSONL cassette per test | SHA-256 of sorted-key JSON | no | no | partial |
| [llm-replay](https://github.com/y-kum/llm-replay) | LLM record/replay | Python | one JSONL tape | SHA-256 of JSON with `default=str` | thread lock only | no | partial |
| [GPTCache](https://github.com/zilliztech/GPTCache) | semantic cache | Python | vector store plus SQL | embedding similarity | — | no | out of scope |

### HTTP record/replay

#### vcrpy and pytest-recording

vcrpy (3k stars, MIT, active) patches Python HTTP clients. It records each request and
response into a YAML "cassette", one per test by default, and replays them later.
pytest-recording adds `--record-mode` and `--block-network`, and its default mode is
`none`, which replays only and fails on a miss. Many LLM projects test this way, including
Pydantic AI and CrewAI.

**How it works**

- The default matchers are method, scheme, host, port, path, and query. **The request
  body isn't compared by default**, so every POST to `/chat/completions` matches every
  other one, and replies are replayed in the order they were recorded. Adding the `body`
  matcher compares JSON bodies as parsed data.
- A cassette is written in full, with a plain `open("w")`, when the test ends.
- Auth headers are recorded unless they're listed in `filter_headers`.
- The YAML loader is now a safe loader that refuses `!!python/object` tags.
- Bodies are stored as one escaped string. Pydantic AI wrote its own serializer that moves
  JSON bodies into a `parsed_body` field, so cassettes are readable in diffs.

**Shortcomings with these projects**

- **It can't see `claude -p`.** The CLI is a subprocess, and vcrpy only patches HTTP in
  its own process. That's ConceptExpansion's main backend (A1, A10). Pointing the CLI at a
  proxy wouldn't help either. One `claude -p` run makes several API calls whose bodies are
  likely to include per-session values, so the keys would never repeat.
- **The key is the HTTP request, not the caller's key.** There's no place for a variant,
  attempt, or label (K3–K5) except stuffing them into the request, which is the `user=`
  workaround SoCloverAI already regrets. Fields that matter to the caller but aren't in
  the request, such as attachment hashes (K9), can't be added. Fields in the request that
  don't matter, such as a client version header, have to be filtered out by hand.
- **Order-based matching is fragile.** With the default matchers, a test that makes the
  same calls in a different order, or in parallel, gets the wrong replies without any
  error.
- **One file per test doesn't scale.** WordSpace's 104k calls and SoCloverAI's
  experiments aren't tests. A cassette for a whole experiment would be one huge YAML file
  that conflicts on every merge (S1, S3, S9). Two pytest-xdist workers or two processes
  writing the same cassette overwrite each other (S5, S6).
- **Long text isn't readable.** Prompts and replies are escaped JSON inside YAML strings,
  and gzip-compressed responses are stored as binary unless `decode_compressed_response`
  is set (S4).
- **Secrets are recorded by default** unless every project lists the right headers
  (`authorization`, `x-api-key`, `api-key`) (E4).
- **No cost, latency, or stats** (E2, A4, T4), and no pruning by used entries (T3).
- **No shared format with TypeScript.** Polly.js and nock each use their own (X1).

What it gets right: record modes, failing loudly on a miss in CI, blocking the network,
and replaying streamed responses as streams (E6).

#### Polly.js

Netflix's record/replay library for Node and the browser (10k stars, Apache-2.0). Its last
commit was in May 2025, so it's effectively unmaintained. Recordings are HAR files, one
per recording name, and requests are matched by configurable fields (`matchRequestsBy`).
It has the same shortcomings as vcrpy: one file per test, keys built from HTTP requests,
and no cost. It also has an open issue that recordings aren't rematched when
`matchRequestsBy` changes. Threshold's own cache already does more than Polly would.

#### retake

A single-binary Rust proxy, created in October 2026, with no users yet. It sets
`OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` for the command it runs and records to a
cassettes folder, so it works for any language. Being a proxy, it has the same key problems
as vcrpy, and the `claude -p` problem above. It's too new to depend on.

### Framework caches

#### litellm

litellm's `Cache` covers completions and embeddings. **litellm 1.109 (2026-10-30,
[PR #48114](https://github.com/BerriAI/litellm/pull/48114)) removed the disk backend** that
SoCloverAI's GenerateKeywordCards2 uses, along with the diskcache dependency, because
diskcache hasn't had a release since August 2023. The remaining backends are in-memory,
Redis, S3, GCS, Azure Blob, and semantic caches in Redis, Valkey, or Qdrant. None of them
can be committed.

**Shortcomings with these projects**

- No local file backend at all now, so nothing to commit (Goal 2).
- The key is a SHA-256 over `repr()` fragments of the call's keyword arguments, filtered
  by litellm's own list of API parameters. No other language can reproduce it (K1, X3),
  and a litellm upgrade can change which parameters count, silently invalidating the
  cache.
- There's no variant or attempt (K3, K4), which led to SoCloverAI's `user=` workaround.
- It only works for calls made through litellm, so not for `claude -p` (A1).
- SoCloverAI has already been broken by its cache-hit path once (litellm 1.105.0).

#### LangChain

`BaseCache.lookup(prompt, llm_string)`: the key is the prompt plus a string describing the
model, built with `str(sorted(params.items()))` or `json.dumps` of LangChain's own
serialization. Backends include in-memory, SQLite, Redis, and many databases. Values are
LangChain `Generation` objects. SoCloverAI's GenerateClues already showed the problems:
the key is LangChain-specific and Python-specific (K1), values are tied to LangChain's
classes (E1), and nothing records cost. It only works inside LangChain.

#### DSPy

DSPy (39k stars) has a two-level cache: an in-memory LRU in front of a diskcache
`FanoutCache` (16 SQLite shards plus pickled values).

**How it works**

- The key is the SHA-256 of `orjson.dumps(request, OPT_SORT_KEYS)`. That's the closest
  any framework comes to canonical JSON. But before hashing, pydantic models become their
  JSON schema and **callables become their source code**, so editing an unrelated line in
  a callable changes the key.
- `rollout_id`, an integer, separates otherwise identical calls, much like K3.
- Unpickling is restricted to known-safe types by default (`restrict_pickle=True`).
- The disk cache's default limit is 30 GB, and diskcache evicts old entries silently
  when the limit is reached.
- **On a hit, usage is cleared** to `{}`, so a replayed run can't report what it would have
  cost.

**Shortcomings with these projects:** SQLite plus pickle can't be committed or merged (S3,
the same problem SoCloverAI has with litellm's disk cache). Silent eviction breaks "replay
forever" (Goal 1), as it did for SoCloverAI's embeddings. Cost isn't kept (Goal 6). It
only works inside DSPy programs.

#### Inspect AI

The UK AI Security Institute's evaluation framework (3.3k stars). Caching is opt-in per
`generate()` call.

**How it works**

- Entries live in the user's cache directory (`INSPECT_CACHE_DIR` overrides it), in
  `generate/<model>/<md5>`, one pickle file per entry holding `(expiry, ModelOutput)`.
- The key is the MD5 of `"|".join(str(c) for c in components)`, where the components are
  the generate config, `str(message.model_dump())` for each message, the base URL, tools,
  tool choice, the expiry period, scopes, and the epoch.
- The epoch works like a variant (K3), and `scopes` adds caller-defined key fields (K2).
- Entries expire after one week by default and are deleted when read after that.
- Refusals stopped by a content filter are deliberately not cached, so a retry gets a
  fresh attempt. (llmemo instead puts the attempt in the key, K4.)
- It sorts config fields into two groups: *dropped* fields, which never enter the key,
  and *neutralized* fields, which already entered old keys and are now always hashed at
  their default. That way, deciding a field doesn't matter doesn't invalidate existing
  entries. This is a careful answer to the same problem as K8.

**Shortcomings with these projects:** `str()` of pydantic dumps and MD5 can't be
reproduced in TypeScript (K1). Pickle is unsafe to load from a shared repo and can't be
diffed (S3, S8). **The expiry period is part of the key**, so changing the cache policy
invalidates everything, and the default expiry deletes entries, which defeats a permanent
record (Goal 1). Files are written in place with no lock, so a crash leaves a broken entry
and parallel samples that miss together all pay (S5, S6). It's tied to Inspect's own model
API.

#### promptfoo

An LLM evaluation CLI (27k stars, TypeScript). Its cache is a single `cache.json` file in
`~/.promptfoo/cache`, written through keyv-file, with a 14-day expiry by default. Keys are
`fetch:v3:` plus a hash of the request. One file rewritten on every change can't be
committed usefully (S1, S3), and the expiry works against a permanent record. It speeds up
repeated evals and isn't a library for application code.

#### Vercel AI SDK middleware

Not a cache. The AI SDK's `wrapLanguageModel` takes middleware with `wrapGenerate` and
`wrapStream` hooks, and the docs show caching recipes (a `Map`, Redis, a local file).
It's the natural place for a future TypeScript llmemo package to plug in for projects
that use the AI SDK, and its stream helpers show how a cached reply can be replayed as a
stream (E6). Threshold calls OpenRouter directly, so it wouldn't need it.

### General function caches

#### joblib.Memory

Caches any Python function's results on disk, in a folder per function named after its
module and function name, keyed by joblib's hash of the pickled arguments. It would work
around a `claude -p` subprocess call, which the HTTP tools can't. But results are pickled
(S3, S8), the hash is Python-only (K1), renaming or moving the function orphans its
entries, and changing the function's source invalidates its cache, which is the opposite
of what E1 wants. There's no parallel-safe recording, metadata, or stats.

#### diskcache

Already described under SoCloverAI. SQLite plus pickle, not mergeable, and silent
eviction at its size limit. Its last commit was in August 2024 and its last release in
August 2023.

#### requests-cache (and hishel)

HTTP caching for `requests` (and hishel for `httpx`). requests-cache's filesystem backend
writes one JSON or YAML file per response, which is closer to what git wants. But it
follows HTTP caching rules (expiry, revalidation), keys on the HTTP request, uses only a
thread lock (so it's not safe across processes), and has the same `claude -p` and key
problems as vcrpy.

### New LLM record/replay libraries

Several small libraries appeared in October and November 2026. Each has one author and
0–1 stars. They're useful as a check on llmemo's design, not as dependencies.

#### agent-replay-fixtures

The closest to llmemo's design (about 460 lines of Python, MIT).

- `TraceRecorder.record(request, events)` writes `<sha256>.json`. The hash is of the
  request after secrets are redacted, serialized with `json.dumps(sort_keys=True,
  separators=(",", ":"), ensure_ascii=False, allow_nan=False)`.
- The file is an envelope holding the payload and a SHA-256 of the payload, so edits are
  detected on read (like K10). Reads enforce a 16 MiB size limit and reject duplicate JSON
  keys (S8).
- Writes go to a temp file with `fsync`, take a lock by creating a `<hash>.lock` folder,
  and then `os.replace` the file into place. If a different trace already exists, it
  raises an error (S5, S7).
- A miss raises an error. There's no fallback to a live call (M2).

**Shortcomings with these projects**

- Python's `json.dumps` isn't RFC 8785: it formats floats and sorts keys differently from
  JavaScript (see [cross-language.md](cross-language.md)), so a TypeScript port couldn't
  match every hash (K1).
- Entries are compact one-line JSON, so prompts and replies aren't readable in diffs, and
  repeated text is stored again in every entry (S1, S4).
- The directory is flat (S2).
- A writer that crashes while holding the lock leaves the lock folder behind, and every
  later write of that key fails. Nothing ever expires the lock (S6).
- A second caller that needs the same missing entry gets an error instead of waiting for
  the first one (S6).
- Redacting before hashing means that changing the redaction rules changes keys.
- There's no read-write mode, async API, cost or latency metadata, stats, or pruning (M1,
  A3, E2, A4, T3).

#### llm-cassette

Wraps any callable (A1) or runs as an OpenAI-compatible replay server or recording proxy.
Interactions are appended to a JSONL cassette, one per test, and matched by the SHA-256 of
sorted-key JSON. One cassette per test has the same scaling and merge problems as vcrpy,
and JSONL escapes every newline (S1, S4, S9).

#### llm-replay

Adapters for the OpenAI, Anthropic, and Google SDKs that record to a JSONL "tape", with a
read-only replay mode. Its canonical JSON uses `default=str`, so any non-JSON value enters
the key as its Python `str()` (K1, X3). It has the same one-file problems as llm-cassette.

### Semantic caches

GPTCache (8k stars) and litellm's semantic backends return a cached reply when a new prompt
is *similar* to an old one. That's a non-goal for llmemo, since a hit must mean the same
key.

### Ideas to borrow

- **Record modes and blocking the network** in CI (vcrpy, pytest-recording) match M1, M2,
  and M5. A pytest plugin could offer the same flags later.
- **Readable bodies:** Pydantic AI's `parsed_body` serializer exists because escaped JSON
  is unreadable in cassettes. That's the same reason for S4.
- **Dropped vs. neutralized key fields** (Inspect AI) is a clean way to state K8's rule
  for fields that turn out not to matter.
- **Don't put expiry or cache policy in the key** (Inspect AI shows why).
- **A hash of the stored payload** inside the entry, and `fsync` before linking
  (agent-replay-fixtures), strengthen K10 and S5.
- **Keep usage on a hit.** DSPy clears it, so its replayed runs can't report cost. llmemo
  returns the recorded usage and cost with every hit (A4, Goal 6).
- **An integer replicate isn't enough on its own.** DSPy's `rollout_id` and Inspect's
  epoch are both integers. llmemo's free-form `variant` also covers rerolls and branches
  (README Decisions).
