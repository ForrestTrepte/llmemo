# llmemo built-in backends

llmemo works with any backend (README A1). It also ships two (A10): the Claude CLI and
OpenRouter. This document defines the **request** each one takes, since the request is
the key's `request` member ([format.md](format.md) §3), and how each fills in the reply
and metadata. A package in another language that follows it makes the same keys, so it
can replay entries that the Python package recorded, and record entries Python can
replay.

Nothing here changes the cache format. The request is an ordinary JSON value, and the
reply and metadata are ordinary entry members (format.md §5).

The words **must**, **must not**, **should**, and **may** are used as in RFC 2119.

Each backend's request is an object with a `backend` member naming the backend. A backend
must reject a request with members it doesn't define, since an ignored member would
still be hashed and would split the cache without changing the call. Absent optional
members are absent, never `null` (format.md §1).

## 1. Claude CLI (`claude-cli`)

The official Claude Code CLI, run headless (`claude -p`) as a subprocess. Calls use the
Claude subscription that the CLI is logged in to.

### 1.1 Request

| Member | Required | Value |
|---|---|---|
| `backend` | yes | `"claude-cli"` |
| `model` | yes | A full model ID, e.g. `"claude-haiku-5-5"`. The CLI's aliases (`default`, `best`, `haiku`, `sonnet`, `opus`, `opusplan`, with or without a `[…]` suffix) must be rejected, since they move to new models (K2) |
| `system` | yes | The system prompt: a string, or an array of strings that are concatenated with nothing between them. It replaces Claude Code's own system prompt |
| `prompt` | yes | The user prompt: a string, or an array of strings concatenated in the same way |
| `jsonSchema` | no | An object: a JSON Schema for structured output |
| `effort` | no | One of `"low"`, `"medium"`, `"high"`, `"xhigh"`, `"max"` |

Arrays of strings let long parts that many calls share (e.g. reference documents) be
stored once as text files (format.md §4, README S4). Each part is a separate string, so
each one is stored separately.

### 1.2 The call

A backend runs the CLI like this, with `<model>` from the request:

```
claude -p --model <model> --tools "" --strict-mcp-config --setting-sources ""
  --disable-slash-commands --no-session-persistence
  --system-prompt-file system.txt --output-format json
  [--json-schema <jsonSchema as JSON>] [--effort <effort>]
```

- The working directory is a new, empty temporary folder, so no project files or
  `CLAUDE.md` are read. `system.txt` in it holds the system prompt, encoded as UTF-8.
- The prompt is written to standard input as UTF-8.
- `CLAUDECODE` is removed from the environment, since the CLI refuses to start inside
  another Claude Code session when it's set.

These flags make the reply depend only on the request: no tools, MCP servers, settings,
skills, or earlier sessions. They aren't part of the key. A change to them that could
change replies needs a new `backend` value.

### 1.3 Reply

The CLI prints one JSON object. If it can't be parsed, or its `type` isn't `"result"`,
nothing arrived: the call fails with kind `process` and the exit code. If `is_error` is
`true`, nothing arrived either (e.g. an unknown model, an API error, or a usage limit). The
call fails with kind `http` and `httpStatus` set to `api_error_status` when that is an
integer, else with kind `process`. The message is the object's `result`, cut to 500
characters. These failures are handled as format.md §5.3 and README E5 say: not stored
by default.

Otherwise the stored reply is the object with only these members, when present, unchanged:
`type`, `subtype`, `is_error`, `result`, `structured_output`, `stop_reason`,
`terminal_reason`, `num_turns`, `usage`, `modelUsage`, `permission_denials`,
`api_error_status`, `errors`.

The others describe the session, not the reply: its ID, timings, and when the login's
credential expires. Keeping a fixed list also keeps whatever a future CLI version adds
out of committed files (E4). The reply text is `result`. With `jsonSchema`, the parsed
object is `structured_output`.

### 1.4 Metadata

| Member (format.md §5.4) | From |
|---|---|
| `costUsd` | `total_cost_usd`. On a subscription this is what the call would have cost through the API (M6) |
| `usage.inputTokens` | `usage.input_tokens` |
| `usage.outputTokens` | `usage.output_tokens` |
| `usage.reasoningTokens` | `usage.output_tokens_details.thinking_tokens` |
| `usage.cacheReadTokens` | `usage.cache_read_input_tokens` |
| `usage.cacheWriteTokens` | `usage.cache_creation_input_tokens` |
| `servedModel` | The member of `modelUsage` named like the request's `model`, or its only member. Absent otherwise |
| `servedBy` | That member's `provider` (e.g. `firstParty`) |
| `toolVersion` | The output of `claude --version`, trimmed (e.g. `2.1.329 (Claude Code)`) |
| `billing` | `subscription`, or `api` when `ANTHROPIC_API_KEY` is set, since the CLI then bills that key |

A member is absent when its source is missing or isn't of the right type (an integer for
token counts, a number for cost).

## 2. OpenRouter (`openrouter`)

OpenRouter's HTTP API at `https://openrouter.ai/api/v1`, with the key sent only in the
`Authorization: Bearer` header.

### 2.1 Request

| Member | Required | Value |
|---|---|---|
| `backend` | yes | `"openrouter"` |
| `endpoint` | yes | `"chat/completions"` or `"embeddings"` |
| `body` | yes | The JSON body to `POST`, sent as given. It must have a string `model` |

The body holds every setting that affects the reply (K2), such as `messages`,
`temperature`, `max_tokens`, `provider` routing, and `dimensions`, under OpenRouter's own
names. Long strings in it (e.g. messages) are stored once as text files (format.md §4).

### 2.2 Reply

An HTTP error status, a network error, or a timeout means nothing arrived, and the call
fails with kind `http` (with `httpStatus`), `network`, or `timeout`. A status 200 body
with an `error` member and no `choices` or `data` is also a failure of kind `http`, with
`httpStatus` set to `error.code` when that's an integer. A failure's message includes only
the body's `error.message`, cut to 300 characters, with the API key replaced by `[key]`.
Nothing else from an error body is kept: it can include the account's user ID.

Otherwise the stored reply is the body unchanged. For `embeddings`, each `data[i].embedding`
that is an array is stored as a vector file (format.md §4.5): `f64` by default, or `f32`
if the caller asked for it. A base64 `embedding` is a string and is stored as one.

### 2.3 Metadata

| Member (format.md §5.4) | From |
|---|---|
| `costUsd` | `usage.cost` |
| `usage.inputTokens` | `usage.prompt_tokens` |
| `usage.outputTokens` | `usage.completion_tokens` |
| `usage.reasoningTokens` | `usage.completion_tokens_details.reasoning_tokens` |
| `usage.cacheReadTokens` | `usage.prompt_tokens_details.cached_tokens` |
| `usage.cacheWriteTokens` | `usage.prompt_tokens_details.cache_write_tokens` |
| `servedModel` | `model` |
| `servedBy` | `provider` (e.g. `Groq`) |
| `billing` | `api` |

### 2.4 Key totals

A `key-total` ledger row (format.md §10.2) comes from `GET /key`: `usageUsd` is
`data.usage`, `limitUsd` is `data.limit` when that is a number, and `provider` is
`openrouter`.
