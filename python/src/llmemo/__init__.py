"""llmemo: a record-and-replay cache for LLM calls, stored as plain files committed to git.

    from llmemo import Cache, Reply

    cache = Cache("sim/cache", mode="record")  # or "replay"; LLMEMO_MODE overrides
    result = cache.call({"model": "...", "prompt": "Say hi."}, my_backend, label="greeting")
    result.reply, result.cached, result.meta

A backend is any `request -> Reply | JSON value` callable. Two are built in: ClaudeCli
(`claude -p`) and OpenRouter.

The format is specified in spec/format.md at the repository root.
"""

from ._cache import Cache, CallInfo, Reply, Result, Usage, git_code_version
from ._claude_cli import ClaudeCli
from ._errors import (
    BackendError,
    CacheMiss,
    FormatError,
    LlmemoError,
    ReplayedError,
    SpendLimitExceeded,
    UsageLimitReached,
)
from ._ledger import Ledger, LedgerFile, Spending, read_ledgers, summarize_spending
from ._openrouter import OpenRouter
from ._stats import SpendLimit, Stats, Totals
from ._store import FileStore, MemoryStore, Store
from ._stored import Vector

__all__ = [
    "BackendError",
    "Cache",
    "CacheMiss",
    "CallInfo",
    "ClaudeCli",
    "FileStore",
    "FormatError",
    "Ledger",
    "LedgerFile",
    "LlmemoError",
    "MemoryStore",
    "OpenRouter",
    "ReplayedError",
    "Reply",
    "Result",
    "SpendLimit",
    "SpendLimitExceeded",
    "Spending",
    "Stats",
    "Store",
    "Totals",
    "Usage",
    "UsageLimitReached",
    "Vector",
    "git_code_version",
    "read_ledgers",
    "summarize_spending",
]
