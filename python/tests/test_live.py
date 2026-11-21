"""Real LLM calls through OpenRouter, recorded in the committed cache in tests/cache/.

With OPENROUTER_API_KEY set (the codespace), a missing entry is recorded, at a cost of
about $0.000002. Without it (CI), the cache runs in replay mode, so these tests check
that committed entries replay with no key and no network. To re-record one, delete its
files from tests/cache/ and run the tests in the codespace.

The backend here is a minimal test helper, not the built-in OpenRouter backend (A10).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from llmemo import BackendError, Cache, Ledger, Reply, SpendLimit, Usage, Vector

CACHE = Path(__file__).parent / "cache"
KEY = os.environ.get("OPENROUTER_API_KEY")
CHAT_MODEL = "meta-llama/llama-3.1-8b-instruct"  # among the cheapest on OpenRouter
EMBEDDING_MODEL = "openai/text-embedding-3-small"


def post(path: str, body: Any) -> Any:
    assert KEY, "only called in record mode"
    req = urllib.request.Request(
        f"https://openrouter.ai/api/v1/{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # The message has no headers, so the key can't leak into a failure record (E4).
        raise BackendError("http", f"HTTP {e.code} from OpenRouter", http_status=e.code) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise BackendError("network", type(e).__name__) from None


def chat(request: dict[str, Any]) -> Reply:
    body = post("chat/completions", request["body"])
    usage = body.get("usage", {})
    return Reply(
        body,
        usage=Usage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        ),
        cost_usd=usage.get("cost"),
        served_model=body.get("model"),
        served_by=body.get("provider"),
        billing="api",
    )


def embed(request: dict[str, Any]) -> Reply:
    body = post("embeddings", request["body"])
    for item in body["data"]:
        item["embedding"] = Vector(item["embedding"])  # stored as .f64 (S12)
    usage = body.get("usage", {})
    return Reply(body, cost_usd=usage.get("cost"), served_by=body.get("provider"), billing="api")


@pytest.fixture
def cache(spend_limit: SpendLimit, ledger: Ledger) -> Cache:
    return Cache(
        CACHE,
        mode="record" if KEY else "replay",
        spend_limit=spend_limit,
        ledger=ledger,
        recorded_by="llmemo-tests",
    )


def test_chat(cache: Cache) -> None:
    request = {
        "backend": "openrouter",
        "body": {
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": "Reply with the single word: hi"}],
            "max_tokens": 5,
            "temperature": 0,
            "usage": {"include": True},
        },
    }
    result = cache.call(request, chat, label="live-chat")
    assert isinstance(result.reply["choices"][0]["message"]["content"], str)
    assert result.meta["costUsd"] < 0.001
    assert result.meta["usage"]["inputTokens"] > 0
    again = cache.call(request, chat, label="live-chat")
    assert again.cached and again.reply == result.reply


def test_embedding(cache: Cache) -> None:
    request = {
        "backend": "openrouter",
        "body": {"model": EMBEDDING_MODEL, "input": ["sea", "light"], "dimensions": 8},
    }
    result = cache.call(request, embed, label="live-embedding")
    vectors = [item["embedding"] for item in result.reply["data"]]
    assert [len(v) for v in vectors] == [8, 8]
    assert all(isinstance(x, float) for v in vectors for x in v)


def test_no_secret_in_the_cache_or_ledger(ledger: Ledger) -> None:
    """E4: the key is sent in a header and must never reach a file."""
    if not KEY:
        pytest.skip("no key to look for")
    for path in [*CACHE.rglob("*"), *ledger.root.rglob("*")]:
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
