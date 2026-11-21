# llmemo (Python)

A record-and-replay cache for LLM calls, stored as plain files committed to git.

This is the Python package. The project's goals, requirements, and the language-neutral
format spec are in the [repository](https://github.com/ForrestTrepte/llmemo).

Install from git (there's no PyPI release yet):

```
uv add "llmemo @ git+https://github.com/ForrestTrepte/llmemo#subdirectory=python"
```

Two backends are built in: `ClaudeCli` (`claude -p` on a Claude subscription) and
`OpenRouter`. [examples/quickstart.py](examples/quickstart.py) sets up a cache and makes
one call through each. It runs with no key or login, because its calls are already
recorded in `examples/cache/`:

```
uv run python examples/quickstart.py
```

Any other backend is a callable you write:

```python
from llmemo import Cache, Reply, Usage


def backend(request):  # any request -> reply callable: an SDK, HTTP, or a subprocess
    ...
    return Reply(raw_reply, usage=Usage(input_tokens=..., output_tokens=...), cost_usd=...)


cache = Cache("sim/cache", mode="record")  # or "replay"; LLMEMO_MODE overrides it
result = cache.call(
    {"backend": "my-backend", "model": "my-model-2026-11-01", "prompt": "Say hi."},
    backend,
    label="greeting",
    variant="trial-1",
    attempt=0,
    tags={"run": "a"},
)
result.reply  # the raw reply, exactly as recorded; parse and validate it yourself
result.cached  # False if this call paid for the backend
cache.stats.total.uncached_cost_usd
```
