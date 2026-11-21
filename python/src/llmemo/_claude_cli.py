"""The built-in Claude CLI backend (A10): `claude -p` run headless as a subprocess.

The request shape and what is kept from the CLI's output are specified in
spec/backends.md §1, so a TypeScript package can make the same keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._cache import Reply, Usage
from ._errors import BackendError

BACKEND = "claude-cli"

# The CLI's model aliases. K2 asks for exact model IDs, since an alias moves to new models.
ALIASES = frozenset({"default", "best", "haiku", "sonnet", "opus", "opusplan"})

# The members of the CLI's JSON output that are stored (spec/backends.md §1.3). The others
# (session IDs, timings, credential expiry) describe the session rather than the reply, and
# an allowlist keeps whatever a future CLI version adds out of committed files (E4).
KEPT = (
    "type",
    "subtype",
    "is_error",
    "result",
    "structured_output",
    "stop_reason",
    "terminal_reason",
    "num_turns",
    "usage",
    "modelUsage",
    "permission_denials",
    "api_error_status",
    "errors",
)

_REQUEST_MEMBERS = frozenset({"backend", "model", "system", "prompt", "jsonSchema", "effort"})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_MESSAGE_CHARS = 500


def _text(value: object, name: str) -> str:
    """A string, or a list of strings that are concatenated with nothing between them."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "".join(value)
    raise ValueError(f"claude-cli request: {name} must be a string or a list of strings")


def _check(request: object) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("a claude-cli request must be an object (see ClaudeCli.request)")
    unknown = set(request) - _REQUEST_MEMBERS
    if unknown:
        # A member the backend ignored would still be hashed, so it would split the cache
        # without changing the call.
        raise ValueError(f"claude-cli request: unknown members {sorted(unknown)}")
    if request.get("backend") != BACKEND:
        raise ValueError(f"claude-cli request: backend must be {BACKEND!r}")
    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("claude-cli request: model must be a model ID")
    if model in ALIASES or model.split("[", 1)[0] in ALIASES:
        raise ValueError(f"claude-cli request: {model!r} is an alias; use a full model ID (K2)")
    for name in ("system", "prompt"):
        if name not in request:
            raise ValueError(f"claude-cli request: {name} is required")
        _text(request[name], name)
    if "jsonSchema" in request and not isinstance(request["jsonSchema"], dict):
        raise ValueError("claude-cli request: jsonSchema must be an object")
    if "effort" in request and request["effort"] not in _EFFORTS:
        raise ValueError(f"claude-cli request: effort must be one of {sorted(_EFFORTS)}")
    return request


def _usage(usage: Mapping[str, Any]) -> Usage:
    def count(value: object) -> int | None:
        return value if type(value) is int else None

    details = usage.get("output_tokens_details")
    return Usage(
        input_tokens=count(usage.get("input_tokens")),
        output_tokens=count(usage.get("output_tokens")),
        reasoning_tokens=count(details.get("thinking_tokens"))
        if isinstance(details, dict)
        else None,
        cache_read_tokens=count(usage.get("cache_read_input_tokens")),
        cache_write_tokens=count(usage.get("cache_creation_input_tokens")),
    )


def _served(model: str, model_usage: object) -> tuple[str | None, str | None]:
    """The model that answered and its provider, from `modelUsage`."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None, None
    if model in model_usage:
        name = model
    elif len(model_usage) == 1:
        name = next(iter(model_usage))
    else:
        return None, None
    info = model_usage[name]
    provider = info.get("provider") if isinstance(info, dict) else None
    return name, provider if isinstance(provider, str) else None


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MESSAGE_CHARS else text[:_MESSAGE_CHARS] + "…"


class ClaudeCli:
    """Calls `claude -p` headless, with no tools, MCP servers, settings, slash commands, or
    saved session, so the reply depends only on the request (A10).

        claude = ClaudeCli()
        request = ClaudeCli.request("claude-haiku-5-5", "Say hi.", system="You are terse.")
        result = cache.call(request, claude)              # or: await cache.acall(request, claude.acall)
        result.reply["result"]                             # the reply text

    Each call runs in a new empty temporary folder, so no project files or CLAUDE.md are
    read. `CLAUDECODE` is removed from the CLI's environment so it runs inside Claude Code
    too. Calls use the Claude subscription the CLI is logged in to, and `billing` is
    `subscription`, unless `ANTHROPIC_API_KEY` is set, which the CLI then uses instead
    (`billing` is `api`). Pass `billing` to say otherwise.

    The CLI's version is stored as `toolVersion` metadata, outside the key (K6).
    """

    def __init__(
        self,
        *,
        executable: str = "claude",
        timeout_s: float | None = 600,
        billing: str | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_s = timeout_s
        self.billing = billing
        self._version: str | None = None
        self._version_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"ClaudeCli(executable={self.executable!r})"

    @staticmethod
    def request(
        model: str,
        prompt: str | Sequence[str],
        *,
        system: str | Sequence[str],
        json_schema: Mapping[str, Any] | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """A request (spec/backends.md §1.1). `model` is a full model ID such as
        `claude-haiku-5-5`. `system` replaces Claude Code's system prompt. `prompt` and
        `system` may be lists of strings, which are concatenated, so that long parts shared
        by many calls are stored once (S4). `json_schema` asks for structured output, which
        the reply holds in `structured_output`."""
        out: dict[str, Any] = {
            "backend": BACKEND,
            "model": model,
            "system": system if isinstance(system, str) else list(system),
            "prompt": prompt if isinstance(prompt, str) else list(prompt),
        }
        if json_schema is not None:
            out["jsonSchema"] = dict(json_schema)
        if effort is not None:
            out["effort"] = effort
        return _check(out)

    # Calling ----------------------------------------------------------------------------

    def __call__(self, request: Any) -> Reply:
        args, stdin = self._prepare(request)
        with tempfile.TemporaryDirectory(prefix="llmemo-claude-") as cwd:
            self._write_system(cwd, request)
            try:
                done = subprocess.run(
                    args,
                    input=stdin,
                    capture_output=True,
                    cwd=cwd,
                    env=self._env(),
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise BackendError("timeout", f"claude -p took over {self.timeout_s} s") from None
        return self._parse(request, done.returncode, done.stdout, done.stderr)

    async def acall(self, request: Any) -> Reply:
        """The async form, for `Cache.acall`."""
        args, stdin = self._prepare(request)
        with tempfile.TemporaryDirectory(prefix="llmemo-claude-") as cwd:
            self._write_system(cwd, request)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), self.timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise BackendError("timeout", f"claude -p took over {self.timeout_s} s") from None
            except BaseException:
                proc.kill()
                await proc.wait()
                raise
        assert proc.returncode is not None
        return self._parse(request, proc.returncode, stdout, stderr)

    def version(self) -> str | None:
        """`claude --version`, run once per ClaudeCli. None if it fails."""
        with self._version_lock:
            if self._version is None:
                try:
                    out = subprocess.run(
                        [self.executable, "--version"],
                        capture_output=True,
                        text=True,
                        env=self._env(),
                        timeout=60,
                        check=True,
                    ).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    out = ""
                self._version = out
            return self._version or None

    # Steps ------------------------------------------------------------------------------

    def _prepare(self, request: Any) -> tuple[list[str], bytes]:
        _check(request)
        args = [
            self.executable,
            "-p",
            "--model",
            request["model"],
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file",
            "system.txt",
            "--output-format",
            "json",
        ]
        if "jsonSchema" in request:
            args += ["--json-schema", json.dumps(request["jsonSchema"], ensure_ascii=False)]
        if "effort" in request:
            args += ["--effort", request["effort"]]
        # The prompt goes on stdin, so its length isn't limited by the command line.
        return args, _text(request["prompt"], "prompt").encode()

    def _write_system(self, cwd: str, request: Mapping[str, Any]) -> None:
        # A file, so a long system prompt isn't limited by the command line either.
        Path(cwd, "system.txt").write_bytes(_text(request["system"], "system").encode())

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # the CLI refuses to start inside another session
        return env

    def _parse(self, request: Mapping[str, Any], code: int, stdout: bytes, stderr: bytes) -> Reply:
        try:
            out = json.loads(stdout)
        except ValueError:
            out = None
        if not isinstance(out, dict) or out.get("type") != "result":
            last = stderr.decode(errors="replace").strip().splitlines()
            raise BackendError(
                "process",
                f"claude -p exited with code {code} and no result"
                + (f": {_short(last[-1])}" if last else ""),
                exit_code=code,
            )
        if out.get("is_error") is True:
            # E5: no reply arrived, e.g. an unknown model, an API error, or a usage limit.
            message = out.get("result")
            text = _short(message) if isinstance(message, str) else "claude -p reported an error"
            status = out.get("api_error_status")
            if type(status) is int:
                raise BackendError("http", text, http_status=status, exit_code=code)
            raise BackendError("process", text, exit_code=code)
        reply = {k: out[k] for k in KEPT if k in out}
        usage = out.get("usage")
        cost = out.get("total_cost_usd")
        served_model, served_by = _served(request["model"], out.get("modelUsage"))
        billing = self.billing
        if billing is None:
            billing = "api" if self._env().get("ANTHROPIC_API_KEY") else "subscription"
        return Reply(
            reply,
            usage=_usage(usage) if isinstance(usage, dict) else None,
            cost_usd=cost if type(cost) in (int, float) else None,
            served_model=served_model,
            served_by=served_by,
            tool_version=self.version(),
            billing=billing,
        )
