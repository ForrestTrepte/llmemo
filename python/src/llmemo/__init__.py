"""llmemo: a record-and-replay cache for LLM calls, stored as plain files committed to git.

    from llmemo import Cache, Reply

    cache = Cache("sim/cache", mode="record")  # or "replay"; LLMEMO_MODE overrides
    result = cache.call({"model": "...", "prompt": "Say hi."}, my_backend, label="greeting")
    result.reply, result.cached, result.meta

The format is specified in spec/format.md at the repository root.
"""

from ._cache import Cache, CallInfo, Reply, Result, Usage, git_code_version
from ._errors import (
    BackendError,
    CacheMiss,
    FormatError,
    LlmemoError,
    ReplayedError,
    SpendLimitExceeded,
)
from ._ledger import Ledger, LedgerFile, Spending, read_ledgers, summarize_spending
from ._stats import SpendLimit, Stats, Totals
from ._store import FileStore, MemoryStore, Store
from ._stored import Vector

__all__ = [
    "BackendError",
    "Cache",
    "CacheMiss",
    "CallInfo",
    "FileStore",
    "FormatError",
    "Ledger",
    "LedgerFile",
    "LlmemoError",
    "MemoryStore",
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
    "Vector",
    "git_code_version",
    "read_ledgers",
    "summarize_spending",
]
