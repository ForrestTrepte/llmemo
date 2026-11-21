"""Real LLM calls through the built-in backends (A10), recorded in the committed cache in
tests/cache/.

OpenRouter calls are recorded when OPENROUTER_API_KEY is set (the codespace), at a cost of
about $0.000002. Claude CLI calls are recorded when `claude` is on the PATH outside CI,
using the owner's subscription, so there are only two small Haiku calls, and one of them
names an unknown model, so it uses nothing. Otherwise (CI) the caches run in replay mode,
so these tests check that committed entries replay with no key, no CLI, and no network.
To re-record an entry, delete its files from tests/cache/ and run the tests in the
codespace.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from llmemo import (
    BackendError,
    Cache,
    ClaudeCli,
    Ledger,
    OpenRouter,
    ReplayedError,
    SpendLimit,
)
from llmemo._cache import Failures, Mode

CACHE = Path(__file__).parent / "cache"
KEY = os.environ.get("OPENROUTER_API_KEY")
HAVE_CLAUDE = shutil.which("claude") is not None and not os.environ.get("CI")
CHAT_MODEL = "meta-llama/llama-3.1-8b-instruct"  # among the cheapest on OpenRouter
EMBEDDING_MODEL = "openai/text-embedding-3-small"
CLAUDE_MODEL = "claude-haiku-5-5"  # CLAUDE.md: the cheapest model for CLI tests

openrouter = OpenRouter()
claude = ClaudeCli()


def make_cache(
    spend_limit: SpendLimit, ledger: Ledger, live: bool, failures: Failures = "discard"
) -> Cache:
    mode: Mode = "record" if live else "replay"
    return Cache(
        CACHE,
        mode=mode,
        failures=failures,
        spend_limit=spend_limit,
        ledger=ledger,
        recorded_by="llmemo-tests",
    )


def test_openrouter_chat(spend_limit: SpendLimit, ledger: Ledger) -> None:
    cache = make_cache(spend_limit, ledger, KEY is not None)
    request = OpenRouter.chat_request(
        {
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": "Reply with the single word: hi"}],
            "max_tokens": 5,
            "temperature": 0,
        }
    )
    result = cache.call(request, openrouter, label="live-chat")
    assert isinstance(result.reply["choices"][0]["message"]["content"], str)
    assert result.meta["costUsd"] < 0.001
    assert result.meta["usage"]["inputTokens"] > 0
    assert result.meta["servedModel"] == CHAT_MODEL
    assert result.meta["billing"] == "api"
    again = cache.call(request, openrouter, label="live-chat")
    assert again.cached and again.reply == result.reply


def test_openrouter_embeddings(spend_limit: SpendLimit, ledger: Ledger) -> None:
    cache = make_cache(spend_limit, ledger, KEY is not None)
    request = OpenRouter.embeddings_request(
        {"model": EMBEDDING_MODEL, "input": ["sea", "light"], "dimensions": 8}
    )
    result = cache.call(request, openrouter, label="live-embedding")
    vectors = [item["embedding"] for item in result.reply["data"]]
    assert [len(v) for v in vectors] == [8, 8]
    assert all(isinstance(x, float) for v in vectors for x in v)


def test_openrouter_http_error(spend_limit: SpendLimit, ledger: Ledger) -> None:
    """A real 400 is free. It's recorded as an error entry, so CI replays it too."""
    cache = make_cache(spend_limit, ledger, KEY is not None, failures="replay")
    request = OpenRouter.chat_request(
        {"model": "llmemo/no-such-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    with pytest.raises(BackendError) as e:
        cache.call(request, openrouter, label="live-http-error")
    assert e.value.kind == "http" and e.value.http_status == 400
    assert "user_id" not in e.value.message
    with pytest.raises(ReplayedError):
        cache.call(request, openrouter, label="live-http-error")


def test_claude_cli(spend_limit: SpendLimit, ledger: Ledger) -> None:
    """One small Haiku call, with a system prompt in parts and structured output."""
    cache = make_cache(spend_limit, ledger, HAVE_CLAUDE)
    request = ClaudeCli.request(
        CLAUDE_MODEL,
        "Name one primary color.",
        system=["You are terse. ", "Always answer in French."],
        json_schema={
            "type": "object",
            "properties": {"color": {"type": "string"}},
            "required": ["color"],
        },
    )
    result = cache.call(request, claude, label="live-claude")
    assert isinstance(result.reply["structured_output"]["color"], str)
    assert isinstance(result.reply["result"], str)
    assert "session_id" not in result.reply  # spec/backends.md §1.3
    meta = result.meta
    assert meta["servedModel"] == CLAUDE_MODEL
    assert meta["billing"] == "subscription"
    assert meta["costUsd"] < 0.01
    assert meta["usage"]["outputTokens"] > 0
    assert "Claude Code" in meta["toolVersion"]


def test_claude_cli_unknown_model(spend_limit: SpendLimit, ledger: Ledger) -> None:
    """The CLI's own error for an unknown model: free, and recorded as an error entry."""
    cache = make_cache(spend_limit, ledger, HAVE_CLAUDE, failures="replay")
    request = ClaudeCli.request("claude-llmemo-no-such-model", "hi", system="")
    with pytest.raises(BackendError) as e:
        cache.call(request, claude, label="live-claude-error")
    assert e.value.kind == "http" and e.value.http_status == 404
    with pytest.raises(ReplayedError):
        cache.call(request, claude, label="live-claude-error")


def test_key_total(ledger: Ledger, tmp_path: Path) -> None:
    if not KEY:
        pytest.skip("needs OPENROUTER_API_KEY")
    usage, limit = openrouter.key_total()
    assert usage >= 0 and (limit is None or limit >= 0)
    # Into a scratch ledger: a row in the committed one on every test run would be noise.
    scratch = Ledger(tmp_path, branch="test", developer="test")
    assert openrouter.record_key_total(scratch, source="test") >= 0
    assert '"kind":"key-total"' in scratch.path.read_text()


def test_no_secret_in_the_cache_or_ledger(ledger: Ledger) -> None:
    """E4: the key is sent in a header and must never reach a file."""
    if not KEY:
        pytest.skip("no key to look for")
    for path in [*CACHE.rglob("*"), *ledger.root.rglob("*")]:
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
