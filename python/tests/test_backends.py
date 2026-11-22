"""The built-in backends (A10), for what real calls can't produce on demand: timeouts,
crashes, network errors, and odd replies. Real calls are in test_live.py.

The Claude CLI is replaced by a small script, and OpenRouter by a local HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal

import pytest

from llmemo import BackendError, Cache, ClaudeCli, MemoryStore, OpenRouter, UsageLimitReached

# Claude CLI ---------------------------------------------------------------------------

SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "hi",
    "session_id": "a5a93708-c9ca-41b6-a5b8-3206c2f00144",
    "credential_expires_at": "2026-12-19T02:26:00.710Z",
    "duration_ms": 1090,
    "total_cost_usd": 0.000196,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 969,
        "cache_read_input_tokens": 0,
        "output_tokens": 4,
        "output_tokens_details": {"thinking_tokens": 0},
    },
    "modelUsage": {"claude-haiku-5-5": {"provider": "firstParty", "costUSD": 0.000196}},
}


def fake_cli(tmp_path: Path, body: str) -> str:
    """An executable that runs `body` as Python, with `args`, `stdin`, and `system` set."""
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('9.9.9 (Claude Code)'); sys.exit(0)\n"
        "stdin = sys.stdin.read()\n"
        "system = open('system.txt').read()\n" + body
    )
    script.chmod(0o755)
    return str(script)


def request(**extra: Any) -> dict[str, Any]:
    return ClaudeCli.request("claude-haiku-5-5", ["Say ", "hi."], system="Be terse.", **extra)


def test_request_shape() -> None:
    assert request(effort="low", json_schema={"type": "object"}) == {
        "backend": "claude-cli",
        "model": "claude-haiku-5-5",
        "system": "Be terse.",
        "prompt": ["Say ", "hi."],
        "jsonSchema": {"type": "object"},
        "effort": "low",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"model": "haiku"},  # an alias (K2)
        {"model": "sonnet[1m]"},
        {"temperature": 0},  # the CLI can't set it, so it mustn't split the cache
        {"backend": "openrouter"},
        {"prompt": ["a", 1]},
        {"effort": "extreme"},
    ],
)
def test_request_rejected(tmp_path: Path, bad: dict[str, Any]) -> None:
    cli = ClaudeCli(executable=fake_cli(tmp_path, "raise SystemExit(9)"))
    with pytest.raises(ValueError):
        cli({**request(), **bad})


def test_missing_system_rejected() -> None:
    with pytest.raises(ValueError, match="system"):
        ClaudeCli()({"backend": "claude-cli", "model": "claude-haiku-5-5", "prompt": "hi"})


def test_call_and_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen = tmp_path / "seen.json"
    cli = ClaudeCli(
        executable=fake_cli(
            tmp_path,
            f"json.dump(dict(args=args, stdin=stdin, system=system, cwd=os.getcwd(),"
            f" claudecode=os.environ.get('CLAUDECODE')), open({str(seen)!r}, 'w'))\n"
            f"print(json.dumps({SUCCESS!r}))\n",
        )
    )
    reply = cli(request(json_schema={"type": "object"}))
    got = json.loads(seen.read_text())
    assert got["stdin"] == "Say hi." and got["system"] == "Be terse."
    assert got["claudecode"] is None
    assert not got["cwd"].startswith(str(Path.cwd()))
    assert got["args"][:3] == ["-p", "--model", "claude-haiku-5-5"]
    assert got["args"][got["args"].index("--json-schema") + 1] == '{"type": "object"}'
    assert "session_id" not in reply.value and "duration_ms" not in reply.value
    assert reply.value["result"] == "hi"
    assert reply.cost_usd == 0.000196
    assert reply.usage is not None and reply.usage.to_json() == {
        "inputTokens": 2,
        "outputTokens": 4,
        "reasoningTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 969,
    }
    assert (reply.served_model, reply.served_by) == ("claude-haiku-5-5", "firstParty")
    assert reply.tool_version == "9.9.9 (Claude Code)"
    assert reply.billing == "subscription"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert cli(request()).billing == "api"


def test_async(tmp_path: Path) -> None:
    cli = ClaudeCli(executable=fake_cli(tmp_path, f"print(json.dumps({SUCCESS!r}))\n"))
    cache = Cache(MemoryStore(), mode="record")
    result = asyncio.run(cache.acall(request(), cli.acall))
    assert result.reply["result"] == "hi" and result.meta["toolVersion"] == "9.9.9 (Claude Code)"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_timeout(tmp_path: Path, asynchronous: bool) -> None:
    cli = ClaudeCli(executable=fake_cli(tmp_path, "time.sleep(30)\n"), timeout_s=0.5)
    with pytest.raises(BackendError) as e:
        asyncio.run(cli.acall(request())) if asynchronous else cli(request())
    assert e.value.kind == "timeout"


def test_crash(tmp_path: Path) -> None:
    cli = ClaudeCli(executable=fake_cli(tmp_path, "print('boom', file=sys.stderr); sys.exit(3)\n"))
    with pytest.raises(BackendError) as e:
        cli(request())
    assert e.value.kind == "process" and e.value.exit_code == 3
    assert "boom" in e.value.message


def test_error_without_status(tmp_path: Path) -> None:
    error = {"type": "result", "subtype": "success", "is_error": True, "result": "limit"}
    cli = ClaudeCli(executable=fake_cli(tmp_path, f"print(json.dumps({error!r})); sys.exit(1)\n"))
    with pytest.raises(BackendError) as e:
        cli(request())
    assert (e.value.kind, e.value.message, e.value.exit_code) == ("process", "limit", 1)


# What claude -p 2.1.329 prints when a subscription's usage limit refuses a call. A real
# one can't be produced on demand, so this is a fake.
USAGE_LIMIT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "result": "You've hit your session limit · resets 3pm (UTC)",
    "api_error_status": 429,
    "api_error": "usage_limit_reached",
    "api_error_params": {
        "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": 1795014000,
            "isUsingOverage": False,
        }
    },
}


@pytest.mark.parametrize("failures", ["discard", "save", "replay"])
def test_usage_limit(tmp_path: Path, failures: Literal["discard", "save", "replay"]) -> None:
    """A usage limit has its own error and reset time, and is never saved (spec §5.3)."""
    store = MemoryStore()
    body = f"print(json.dumps({USAGE_LIMIT!r})); sys.exit(1)\n"
    cli = ClaudeCli(executable=fake_cli(tmp_path, body))
    with pytest.raises(UsageLimitReached) as e:
        Cache(store, mode="record", failures=failures).call(request(), cli)
    assert e.value.kind == "usage-limit" and e.value.http_status == 429
    assert e.value.resets_at == datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
    assert e.value.limit_type == "five_hour"
    assert e.value.to_json()["details"] == {
        "resetsAt": "2026-11-18T15:00:00.000Z",
        "limitType": "five_hour",
    }
    assert not store._files


def test_usage_limit_without_reset_time(tmp_path: Path) -> None:
    error = {k: v for k, v in USAGE_LIMIT.items() if k != "api_error_params"}
    cli = ClaudeCli(executable=fake_cli(tmp_path, f"print(json.dumps({error!r})); sys.exit(1)\n"))
    with pytest.raises(UsageLimitReached) as e:
        cli(request())
    assert e.value.resets_at is None and e.value.limit_type is None
    assert "details" not in e.value.to_json()


def test_failure_saved(tmp_path: Path) -> None:
    """E5: with failures="save", a crash is kept for diagnosis."""
    store = MemoryStore()
    cli = ClaudeCli(executable=fake_cli(tmp_path, "sys.exit(3)\n"))
    with pytest.raises(BackendError):
        Cache(store, mode="record", failures="save").call(request(), cli)
    assert any(".failure." in name for name in store._files)


# OpenRouter ----------------------------------------------------------------------------

FAKE_KEY = "sk-or-v1-llmemo-test-key"


class Server:
    """A local HTTP server that answers every request with `status` and `body`."""

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {}
        self.seen: list[tuple[str, str, Any, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.seen.append((self.command, self.path, body, self.headers["Authorization"]))
                data = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    s = Server()
    yield s
    s.httpd.shutdown()


def chat() -> dict[str, Any]:
    return OpenRouter.chat_request({"model": "x/y", "messages": []})


def test_openrouter_sends_body_and_key(server: Server) -> None:
    server.body = {
        "choices": [],
        "model": "x/y",
        "provider": "P",
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 1,
            "cost": 1e-6,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    }
    reply = OpenRouter(base_url=server.url)(chat())
    assert server.seen == [
        ("POST", "/api/v1/chat/completions", {"model": "x/y", "messages": []}, f"Bearer {FAKE_KEY}")
    ]
    assert (reply.cost_usd, reply.served_model, reply.served_by) == (1e-6, "x/y", "P")
    assert reply.usage is not None
    assert reply.usage.to_json() == {"inputTokens": 3, "outputTokens": 1, "cacheReadTokens": 2}


def test_openrouter_float32(server: Server) -> None:
    server.body = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": "AAAA"}]}
    request = OpenRouter.embeddings_request({"model": "x/e", "input": ["a", "b"]})
    store = MemoryStore()
    result = Cache(store, mode="record").call(
        request, OpenRouter(base_url=server.url, float32=True)
    )
    assert any(name.endswith(".f32") for name in store._files)
    assert result.reply["data"][0]["embedding"] == pytest.approx([0.1, 0.2], rel=1e-7)
    assert result.reply["data"][1]["embedding"] == "AAAA"


@pytest.mark.parametrize("status", [401, 200])
def test_openrouter_error_hides_key_and_user(server: Server, status: int) -> None:
    server.status = status
    server.body = {
        "error": {"message": f"bad key {FAKE_KEY}", "code": 401},
        "user_id": "user_123",
    }
    with pytest.raises(BackendError) as e:
        OpenRouter(base_url=server.url)(chat())
    assert e.value.kind == "http" and e.value.http_status == 401
    assert FAKE_KEY not in str(e.value) and "user_123" not in str(e.value)
    assert "[key]" in e.value.message


def test_openrouter_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once it closes
    with pytest.raises(BackendError) as e:
        OpenRouter(base_url=f"http://127.0.0.1:{port}")(chat())
    assert e.value.kind == "network"


def test_openrouter_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()  # accepts the connection but never answers
        url = f"http://127.0.0.1:{s.getsockname()[1]}"
        with pytest.raises(BackendError) as e:
            OpenRouter(base_url=url, timeout_s=0.3)(chat())
    assert e.value.kind == "timeout"


def test_openrouter_needs_key_only_to_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    backend = OpenRouter()  # constructing it, and replaying, need no key
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        backend(chat())


def test_openrouter_request_rejected() -> None:
    with pytest.raises(ValueError):
        OpenRouter()({**chat(), "temperature": 0})
    with pytest.raises(ValueError):
        OpenRouter()({**chat(), "endpoint": "completions"})


def test_openrouter_key_total(server: Server, tmp_path: Path) -> None:
    from llmemo import Ledger

    server.body = {"data": {"usage": 1.5, "limit": None, "label": "sk-or-v1-2e4...49f"}}
    ledger = Ledger(tmp_path, branch="main", developer="dev")
    assert OpenRouter(base_url=server.url).record_key_total(ledger, source="test") == 1.5
    row = json.loads(ledger.path.read_text())
    assert (row["kind"], row["provider"], row["usageUsd"]) == ("key-total", "openrouter", 1.5)
    assert "limitUsd" not in row and row["source"] == "test"
    assert server.seen[0][:2] == ("GET", "/api/v1/key")
