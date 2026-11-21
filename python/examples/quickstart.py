"""Quickstart: cache one call through each built-in backend (A10).

Run it from python/:

    uv run python examples/quickstart.py

The calls are already recorded in examples/cache/, so this replays them: no API key, no
Claude login, no network, no cost. To make real calls, change the code, or delete
examples/cache/ and run with LLMEMO_MODE=record, which calls OpenRouter (needs
OPENROUTER_API_KEY) and `claude -p` (needs a Claude login) for whatever is missing.

tests/test_examples.py runs this in replay mode, so it can't go stale.
"""

from __future__ import annotations

from pathlib import Path

from llmemo import Cache, ClaudeCli, OpenRouter


def main() -> None:
    cache = Cache(
        Path(__file__).parent / "cache",  # a folder, meant to be committed to git
        mode="replay",  # "record" calls the backend on a miss; LLMEMO_MODE overrides this
        spend_limit=0.10,  # stop calling backends once a run has spent $0.10 (M6)
    )

    # Claude CLI: `claude -p` on the Claude subscription it is logged in to.
    claude = ClaudeCli()
    request = ClaudeCli.request(
        "claude-haiku-5-5",  # a full model ID, not an alias like "haiku"
        "Name one primary color.",
        system=["You are terse. ", "Always answer in French."],  # parts are concatenated
        json_schema={
            "type": "object",
            "properties": {"color": {"type": "string"}},
            "required": ["color"],
        },
    )
    result = cache.call(request, claude, label="quickstart-claude")
    print("Claude:", result.reply["structured_output"])  # the reply text is in "result"
    print("  cached:", result.cached, " model:", result.meta["servedModel"])

    # OpenRouter: chat completions, with the key in OPENROUTER_API_KEY.
    openrouter = OpenRouter()
    request = OpenRouter.chat_request(
        {
            "model": "meta-llama/llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "Reply with the single word: hi"}],
            "max_tokens": 5,
            "temperature": 0,
        }
    )
    result = cache.call(request, openrouter, label="quickstart-openrouter")
    print("OpenRouter:", result.reply["choices"][0]["message"]["content"])
    print("  cached:", result.cached, " model:", result.meta["servedModel"])

    # What this run spent, and what replayed calls originally cost (A4).
    total = cache.stats.total
    print(f"Spent ${total.uncached_cost_usd:.6f}; replayed calls cost ${total.cached_cost_usd:.6f}")


if __name__ == "__main__":
    main()
