"""The built-in OpenRouter backend (A10): chat completions and embeddings over HTTP.

The request shape and the metadata taken from replies are specified in
spec/backends.md §2, so a TypeScript package can make the same keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Literal

from ._cache import Reply, Usage
from ._errors import BackendError
from ._ledger import Ledger
from ._stored import Vector

BACKEND = "openrouter"
ENDPOINTS = frozenset({"chat/completions", "embeddings"})
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_MESSAGE_CHARS = 300


def _check(request: object) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("an openrouter request must be an object (see OpenRouter.chat_request)")
    unknown = set(request) - {"backend", "endpoint", "body"}
    if unknown:
        raise ValueError(f"openrouter request: unknown members {sorted(unknown)}")
    if request.get("backend") != BACKEND:
        raise ValueError(f"openrouter request: backend must be {BACKEND!r}")
    if request.get("endpoint") not in ENDPOINTS:
        raise ValueError(f"openrouter request: endpoint must be one of {sorted(ENDPOINTS)}")
    body = request.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        raise ValueError("openrouter request: body must be an object with a model")
    return request


def _count(value: object) -> int | None:
    return value if type(value) is int else None


def _usage(usage: Mapping[str, Any]) -> Usage:
    prompt = usage.get("prompt_tokens_details")
    prompt = prompt if isinstance(prompt, dict) else {}
    completion = usage.get("completion_tokens_details")
    completion = completion if isinstance(completion, dict) else {}
    return Usage(
        input_tokens=_count(usage.get("prompt_tokens")),
        output_tokens=_count(usage.get("completion_tokens")),
        reasoning_tokens=_count(completion.get("reasoning_tokens")),
        cache_read_tokens=_count(prompt.get("cached_tokens")),
        cache_write_tokens=_count(prompt.get("cache_write_tokens")),
    )


class OpenRouter:
    """Calls OpenRouter's HTTP API (A10). The API key is read from the environment
    variable `api_key_env` when a call is made, so replaying needs no key.

        openrouter = OpenRouter()
        request = OpenRouter.chat_request({"model": "...", "messages": [...]})
        result = cache.call(request, openrouter)          # or: cache.acall(request, openrouter.acall)
        result.reply["choices"][0]["message"]["content"]

    The body is sent as given and the reply body is stored unchanged (E1). In embedding
    replies, each `embedding` array is stored as a binary vector file (S12): float64 by
    default, or float32 with `float32=True`. The key never reaches a file: it is sent only
    in a header, and error messages are taken from the reply body with the key removed (E4).
    """

    def __init__(
        self,
        *,
        api_key_env: str = "OPENROUTER_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 120,
        float32: bool = False,
    ) -> None:
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.dtype: Literal["f64", "f32"] = "f32" if float32 else "f64"

    def __repr__(self) -> str:
        return f"OpenRouter(base_url={self.base_url!r})"

    @staticmethod
    def chat_request(body: Mapping[str, Any]) -> dict[str, Any]:
        """A request for `POST /chat/completions` with this body (spec/backends.md §2.1)."""
        return _check({"backend": BACKEND, "endpoint": "chat/completions", "body": dict(body)})

    @staticmethod
    def embeddings_request(body: Mapping[str, Any]) -> dict[str, Any]:
        """A request for `POST /embeddings` with this body (spec/backends.md §2.1)."""
        return _check({"backend": BACKEND, "endpoint": "embeddings", "body": dict(body)})

    # Calling ----------------------------------------------------------------------------

    def __call__(self, request: Any) -> Reply:
        _check(request)
        body = self._send("POST", request["endpoint"], request["body"])
        if request["endpoint"] == "embeddings":
            return self._embeddings_reply(body)
        return self._chat_reply(body)

    async def acall(self, request: Any) -> Reply:
        """The async form, for `Cache.acall`. It runs the call in a worker thread."""
        return await asyncio.to_thread(self, request)

    def key_total(self) -> tuple[float, float | None]:
        """The key's total spending so far and its limit, from `GET /key`, in US dollars."""
        data = self._send("GET", "key", None).get("data")
        data = data if isinstance(data, dict) else {}
        usage, limit = data.get("usage"), data.get("limit")
        if not isinstance(usage, (int, float)) or isinstance(usage, bool):
            raise BackendError("other", "OpenRouter's /key reply has no usage")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            return float(usage), None
        return float(usage), float(limit)

    def record_key_total(self, ledger: Ledger, *, source: str | None = None) -> float:
        """Append a `key-total` row (spec §10.2) to `ledger`, e.g. at the start of a
        recording run, so reports can show spending the call rows don't see. Returns the
        key's total."""
        usage, limit = self.key_total()
        ledger.record_key_total(BACKEND, usage, limit_usd=limit, source=source)
        return usage

    # Steps ------------------------------------------------------------------------------

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"OpenRouter needs an API key in the {self.api_key_env} variable")
        return key

    def _send(self, method: str, path: str, body: object) -> dict[str, Any]:
        key = self._key()
        req = urllib.request.Request(
            f"{self.base_url}/{path}",
            method=method,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                detail = self._error_message(json.loads(e.read()), key)
            except (OSError, ValueError):
                detail = None
            message = f"HTTP {status} from OpenRouter" + (f": {detail}" if detail else "")
            raise BackendError("http", message, http_status=status) from None
        except TimeoutError:
            raise BackendError("timeout", f"OpenRouter took over {self.timeout_s} s") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise BackendError("timeout", f"OpenRouter took over {self.timeout_s} s") from None
            raise BackendError(
                "network", f"can't reach OpenRouter: {type(e.reason).__name__}"
            ) from None
        try:
            out = json.loads(data)
        except ValueError:
            raise BackendError("other", "OpenRouter's reply isn't JSON") from None
        if not isinstance(out, dict):
            raise BackendError("other", "OpenRouter's reply isn't a JSON object")
        if "error" in out and "choices" not in out and "data" not in out:
            # An error reported with status 200, e.g. when the provider fails.
            error = out["error"]
            code = error.get("code") if isinstance(error, dict) else None
            detail = self._error_message(out, key) or "unknown error"
            raise BackendError(
                "http",
                f"OpenRouter reported an error: {detail}",
                http_status=code if type(code) is int else None,
            )
        return out

    @staticmethod
    def _error_message(body: object, key: str) -> str | None:
        """Only `error.message`, shortened and with the key removed. The rest of an error
        body (e.g. the account's user ID) isn't kept."""
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if not isinstance(message, str):
            return None
        message = message.replace(key, "[key]").strip()
        return message if len(message) <= _MESSAGE_CHARS else message[:_MESSAGE_CHARS] + "…"

    def _chat_reply(self, body: dict[str, Any]) -> Reply:
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        cost = usage.get("cost")
        model = body.get("model")
        provider = body.get("provider")
        return Reply(
            body,
            usage=_usage(usage),
            cost_usd=cost if type(cost) in (int, float) else None,
            served_model=model if isinstance(model, str) else None,
            served_by=provider if isinstance(provider, str) else None,
            billing="api",
        )

    def _embeddings_reply(self, body: dict[str, Any]) -> Reply:
        data = body.get("data")
        if isinstance(data, list):
            for item in data:
                # A base64 embedding (encoding_format "base64") is a string, kept as is.
                if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                    item["embedding"] = Vector(item["embedding"], self.dtype)
        return self._chat_reply(body)
