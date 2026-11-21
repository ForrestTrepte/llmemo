"""Regenerate spec/vectors/ from the cases below, using reference.py. From the repo root:

    uv run --project python python spec/tools/generate_vectors.py

Every generated case is read back with the reference reader before it's written out, so a
case that doesn't behave as described fails here. Then check the result with the
independent JavaScript implementation:

    node spec/tools/check_vectors.mjs

Output is deterministic: running this twice gives identical files.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import reference as ref

VECTORS = Path(__file__).resolve().parent.parent / "vectors"
GENERATED = ["keys.json", "invalid-keys.json", "entries", "read-ok", "read-errors"]


def dump(v: object) -> str:
    """Case files are ordinary JSON. Python keeps 1.0 and 1e-07 as written, which is the
    point of several key cases."""
    return json.dumps(v, indent=2, ensure_ascii=False) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def key_input(env: dict[str, Any]) -> dict[str, Any]:
    """The parts a caller passes, for case files."""
    return {k: env[k] for k in ("label", "variant", "attempt", "request") if k in env}


def env_from(inp: dict[str, Any]) -> dict[str, Any]:
    return ref.make_key(inp["request"], inp["attempt"], inp.get("label"), inp.get("variant"))


def mark_vectors(v: object) -> object:
    """Turn the case-file notation {"$test.vector": {...}} into ref.Vector."""
    if isinstance(v, dict):
        if set(v) == {"$test.vector"}:
            spec = v["$test.vector"]
            return ref.Vector(tuple(spec["values"]), spec["dtype"])
        return {k: mark_vectors(item) for k, item in v.items()}
    if isinstance(v, list):
        return [mark_vectors(item) for item in v]
    return v


# Keys -----------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are the clue-giver in a game of Concept.\n"
    "Place icons on the board to suggest the prompt.\n"
    "Never write words or letters.\n"
)

KEY_CASES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "minimal",
        "No label or variant. These two are absent, so they don't appear in the canonical form.",
        {"attempt": 0, "request": {"model": "anthropic/claude-haiku-4.5", "prompt": "Say hi."}},
    ),
    (
        "label-and-variant",
        "All envelope members present.",
        {
            "label": "clue-giver",
            "variant": "trial-3",
            "attempt": 2,
            "request": {
                "backend": "claude-cli",
                "model": "claude-haiku-4-5-20251001",
                "prompt": "Prompt: Lighthouse",
            },
        },
    ),
    (
        "request-is-a-string",
        "The request may be any JSON value.",
        {"attempt": 0, "request": "Say hi."},
    ),
    (
        "request-is-null",
        "The request may be null.",
        {"attempt": 0, "request": None},
    ),
    (
        "numbers",
        "ECMAScript number formatting. 1.0 is written 1, 1e-07 is written 1e-7, and 1e-06 is written 0.000001.",
        {
            "attempt": 0,
            "request": [
                0,
                1,
                -1,
                1.0,
                -1.5,
                0.5,
                0.1,
                0.30000000000000004,
                100,
                100.0,
                1e-06,
                1e-07,
                1.5e-05,
                123456789.125,
                9007199254740991,
                -9007199254740991,
                9007199254740990.0,
                4503599627370495.5,
                5e-324,
                2.2250738585072014e-308,
                1.7976931348623157e-300,
            ],
        },
    ),
    (
        "strings",
        "Only quote, backslash, and characters below U+0020 are escaped. "
        "Slash, U+007F, U+2028, and non-ASCII characters are written as UTF-8.",
        {
            "attempt": 0,
            "request": {
                "ascii": "Hello, world",
                "controls": "\u0000\u0001\b\t\n\u000b\f\r\u001e\u001f",
                "quote-backslash": '"\\',
                "slash-del": "/\u007f",
                "separators": "\u2028\u2029",
                "non-ascii": "café 日本 😀 ﬀ",
                "bom": "\ufeffstart",
                "empty": "",
            },
        },
    ),
    (
        "member-order",
        "Member names sort by UTF-16 code units. 😀 (D83D DE00) sorts before ﬀ (U+FB00), unlike "
        'code-point order. Integer-like names sort as strings: "10" before "2".',
        {
            "attempt": 0,
            "request": {
                "b": 1,
                "a": 2,
                "aa": 3,
                "B": 4,
                "": 5,
                "é": 6,
                "ﬀ": 7,
                "😀": 8,
                "10": 9,
                "2": 10,
                "\u007f": 11,
            },
        },
    ),
    (
        "nesting-and-empties",
        "Empty and nested containers, and literals.",
        {
            "attempt": 0,
            "request": {
                "o": {},
                "a": [],
                "aa": [[]],
                "ao": [{}],
                "lit": [True, False, None],
                "deep": {"x": {"y": {"z": [1, [2, [3]]]}}},
            },
        },
    ),
    (
        "absent-field",
        "Compare with present-null: an absent member and a null member give different IDs (K8).",
        {"attempt": 0, "request": {"model": "m", "prompt": "p"}},
    ),
    (
        "present-null",
        "Compare with absent-field.",
        {"attempt": 0, "request": {"model": "m", "prompt": "p", "seed": None}},
    ),
    (
        "reserved-member-names",
        "Names that need escaping in entry files (§4.3) are hashed as they are. Escaping is "
        "part of the stored form only.",
        {
            "attempt": 0,
            "request": {
                "$llmemo.text": "not a reference",
                "$$llmemo": 1,
                "$schema": "x",
                "$ref": "#/y",
            },
        },
    ),
    (
        "long-text",
        "The ID covers the full text, however it's stored.",
        {
            "attempt": 0,
            "label": "clue-giver",
            "request": {"system": SYSTEM_PROMPT, "prompt": "Prompt: Lighthouse"},
        },
    ),
    (
        "attachment",
        "The recommended attachment form (§3.3): the SHA-256 of the file's bytes, not the bytes.",
        {
            "attempt": 0,
            "request": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is shown?"},
                            {
                                "type": "image",
                                "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                                "mediaType": "image/png",
                            },
                        ],
                    }
                ]
            },
        },
    ),
]

# Each is either "input" (an envelope-level problem, valid JSON) or "requestJson" (JSON
# text to parse with the language's normal parser, then use as the request). The parse
# itself may already fail, which also counts as rejecting it.
INVALID_KEY_CASES: list[dict[str, Any]] = [
    {"name": "attempt-negative", "input": {"attempt": -1, "request": {}}, "error": "invalid-key"},
    {"name": "attempt-fraction", "input": {"attempt": 1.5, "request": {}}, "error": "invalid-key"},
    {"name": "attempt-boolean", "input": {"attempt": True, "request": {}}, "error": "invalid-key"},
    {"name": "attempt-string", "input": {"attempt": "0", "request": {}}, "error": "invalid-key"},
    {
        "name": "attempt-too-large",
        "input": {"attempt": 9007199254740992, "request": {}},
        "error": "invalid-key",
    },
    {
        "name": "label-empty",
        "input": {"attempt": 0, "label": "", "request": {}},
        "error": "invalid-key",
    },
    {
        "name": "variant-empty",
        "input": {"attempt": 0, "variant": "", "request": {}},
        "error": "invalid-key",
    },
    {
        "name": "label-not-string",
        "input": {"attempt": 0, "label": 3, "request": {}},
        "error": "invalid-key",
    },
    {"name": "nan", "requestJson": '{"temperature": NaN}', "error": "invalid-value"},
    {"name": "infinity", "requestJson": '{"temperature": Infinity}', "error": "invalid-value"},
    {
        "name": "overflow-to-infinity",
        "requestJson": '{"temperature": 1e400}',
        "error": "invalid-value",
    },
    {"name": "negative-zero", "requestJson": '{"temperature": -0.0}', "error": "invalid-value"},
    {
        "name": "integer-2-pow-53",
        "requestJson": '{"seed": 9007199254740992}',
        "error": "invalid-value",
    },
    {
        "name": "integer-2-pow-60",
        "requestJson": '{"seed": 1152921504606846976}',
        "error": "invalid-value",
    },
    {"name": "large-float", "requestJson": '{"x": 1e16}', "error": "invalid-value"},
    {"name": "huge-float", "requestJson": '{"x": 1.5e300}', "error": "invalid-value"},
    {
        "name": "lone-high-surrogate",
        "requestJson": '{"prompt": "a\\ud800b"}',
        "error": "invalid-value",
    },
    {"name": "lone-low-surrogate", "requestJson": '["\\udc00"]', "error": "invalid-value"},
    {"name": "lone-surrogate-in-name", "requestJson": '{"\\ud83d": 1}', "error": "invalid-value"},
]


def parse_native(text: str) -> object:
    """What Python's json.loads gives by default (it accepts NaN and Infinity)."""
    return json.loads(text)


def gen_keys() -> None:
    cases = []
    for name, description, inp in KEY_CASES:
        env = env_from(inp)
        canon = ref.canonical_bytes(env)
        cases.append(
            {
                "name": name,
                "description": description,
                "input": inp,
                "canonical": canon.decode(),
                "sha256": ref.sha256_hex(canon),
            }
        )
    ids = {c["name"]: c["sha256"] for c in cases}
    assert ids["absent-field"] != ids["present-null"]
    write_text(VECTORS / "keys.json", dump(cases))

    for case in INVALID_KEY_CASES:
        try:
            if "input" in case:
                env_from(case["input"])
            else:
                env_from({"attempt": 0, "request": parse_native(case["requestJson"])})
        except ref.FormatError as e:
            assert e.code == case["error"], (case["name"], e)
        else:
            raise AssertionError(f"{case['name']} was accepted")
    write_text(VECTORS / "invalid-keys.json", dump(INVALID_KEY_CASES))


# Entries --------------------------------------------------------------------------

LONG_LINE = "Reference: " + "the board has icons for colors, shapes, and objects; " * 11
FULL_META = {
    "recordedAt": "2026-11-20T22:15:00.123Z",
    "recordedBy": "developer",
    "latencyMs": 4120,
    "usage": {
        "inputTokens": 1830,
        "outputTokens": 64,
        "reasoningTokens": 0,
        "cacheReadTokens": 1700,
        "cacheWriteTokens": 0,
    },
    "costUsd": 0.0021,
    "servedModel": "claude-haiku-4-5-20251001",
    "servedBy": "anthropic",
    "toolVersion": "claude 2.1.3",
    "codeVersion": "5203929-dirty",
    "tags": {"run": "2026-11-20-a", "game": "17", "role": "clue-giver"},
    "extra": {"timeoutS": 120},
}
MIN_META = {"recordedAt": "2026-11-20T22:15:00.000Z"}

ENTRY_CASES: list[dict[str, Any]] = [
    {
        "name": "minimal",
        "description": "A short text reply and only the required metadata.",
        "key": {
            "attempt": 0,
            "request": {"model": "anthropic/claude-haiku-4.5", "prompt": "Say hi."},
        },
        "reply": "Hi!",
        "meta": MIN_META,
    },
    {
        "name": "full-meta",
        "description": "Every envelope member and every metadata member. The reply is an object, like claude -p's JSON output.",
        "key": {
            "label": "clue-giver",
            "variant": "trial-3",
            "attempt": 2,
            "request": {
                "backend": "claude-cli",
                "model": "claude-haiku-4-5-20251001",
                "prompt": "Prompt: Lighthouse",
                "settings": {"tools": [], "effort": "low"},
            },
        },
        "reply": {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"placements": ["A1", "C4"]}',
            "total_cost_usd": 0.0021,
            "duration_ms": 4120,
            "usage": {"input_tokens": 130, "cache_read_input_tokens": 1700, "output_tokens": 64},
        },
        "meta": FULL_META,
    },
    {
        "name": "stored-text",
        "description": "Strings with a newline or over 512 UTF-8 bytes go to .txt files. The system prompt appears "
        "in both the key and the reply but is stored once. 256 × é is exactly 512 bytes and stays inline; one more "
        "byte moves it to a file.",
        "key": {
            "attempt": 0,
            "label": "clue-giver",
            "request": {
                "system": SYSTEM_PROMPT,
                "prompt": "Prompt: Lighthouse",
                "reference": LONG_LINE,
            },
        },
        "reply": {
            "echo": SYSTEM_PROMPT,
            "text": "Placements:\nA1 (light)\nC4 (sea)\n",
            "inline-512-bytes": "é" * 256,
            "stored-513-bytes": "é" * 256 + "a",
            "short": "fine",
        },
        "meta": MIN_META,
    },
    {
        "name": "text-bytes",
        "description": "Text files keep exact bytes: CRLF line endings, a leading byte order mark, a NUL, characters "
        "outside the BMP, and no added final newline.",
        "key": {"attempt": 0, "request": {"prompt": "Edge cases"}},
        "reply": [
            "line 1\r\nline 2\r\n",
            "\ufeffstarts with a BOM\n",
            "a\u0000\nb",
            "😀\nﬀ",
            "no final newline\nhere",
            "\n",
        ],
        "meta": MIN_META,
    },
    {
        "name": "reserved-names",
        "description": "Member names starting with $…llmemo get one more $ in the stored form (§4.3). $schema and $ref don't.",
        "key": {
            "attempt": 0,
            "request": {
                "$llmemo.text": "not a reference",
                "$$llmemo": 1,
                "$llmemoX": 2,
                "$schema": "x",
                "$ref": "#/y",
                "llmemo": 3,
            },
        },
        "reply": {"$llmemo.f64": [1, 2], "x$llmemo": True},
        "meta": MIN_META,
    },
    {
        "name": "numbers-and-order",
        "description": "Number formatting and member order in the pretty-printed file. A JavaScript writer can't rely "
        'on object insertion order: it would put "2" and "10" first.',
        "key": {"attempt": 0, "request": {"prompt": "Return numbers"}},
        "reply": {
            "b": 1,
            "a": [1.0, 0.1, 1e-07, 1.5e-05, 0.30000000000000004, -0.5, 9007199254740991, 5e-324],
            "10": "ten",
            "2": "two",
            "😀": "emoji",
            "ﬀ": "ligature",
            "": "empty name",
            "nested": {"o": {}, "a": [], "aa": [[]], "lit": [True, False, None]},
        },
        "meta": MIN_META,
    },
    {
        "name": "vector-f64",
        "description": 'An embedding stored exactly as little-endian float64 (§4.5). {"$test.vector": …} in this '
        "file marks the array as a vector; it's test notation, not part of the format.",
        "key": {
            "attempt": 0,
            "request": {
                "backend": "openrouter",
                "model": "openai/text-embedding-3-small",
                "input": "lighthouse",
            },
        },
        "reply": {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": {
                        "$test.vector": {
                            "dtype": "f64",
                            "values": [
                                0.1,
                                -0.25,
                                1e-07,
                                3.141592653589793,
                                -2.5e-10,
                                0,
                                1,
                                0.30000000000000004,
                            ],
                        }
                    },
                }
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        },
        "meta": MIN_META,
    },
    {
        "name": "vector-f32",
        "description": "Two embeddings stored as float32 on request. Values are rounded to float32 and read back "
        "widened, so 0.1 reads back as 0.10000000149011612 (see expectedReply).",
        "key": {
            "attempt": 0,
            "request": {
                "backend": "openrouter",
                "model": "openai/text-embedding-3-small",
                "input": ["sea", "light"],
            },
        },
        "reply": {
            "data": [
                {
                    "index": 0,
                    "embedding": {
                        "$test.vector": {
                            "dtype": "f32",
                            "values": [0.1, -0.25, 1e-07, 3.141592653589793, 0.5],
                        }
                    },
                },
                {
                    "index": 1,
                    "embedding": {
                        "$test.vector": {"dtype": "f32", "values": [0.5, 0.5, 0.5, 0.5, 0.5]}
                    },
                },
            ]
        },
        "meta": MIN_META,
    },
    {
        "name": "error-entry",
        "description": "A failure saved at the entry's own path, to be replayed on purpose (§5.3). Reading it raises the error.",
        "key": {"attempt": 0, "label": "slow", "request": {"prompt": "Write a very long story."}},
        "error": {
            "kind": "timeout",
            "message": "No reply within 120 s",
            "details": {"timeoutS": 120},
        },
        "meta": {"recordedAt": "2026-11-20T22:17:00.500Z", "latencyMs": 120000},
    },
    {
        "name": "failure-record",
        "description": "A failure saved for diagnosis (§5.3). It isn't at the entry's path, so a lookup misses. "
        "The multi-line message goes to a .txt file.",
        "key": {"attempt": 0, "request": {"prompt": "Say hi."}},
        "error": {
            "kind": "http",
            "message": "503 Service Unavailable\nupstream overloaded",
            "httpStatus": 503,
        },
        "meta": {"recordedAt": "2026-11-20T22:15:00.123Z", "latencyMs": 812},
        "failureId": "20261120T221500123Z-1a2b3c4d",
    },
]


def tree_files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def gen_entries() -> None:
    for case in ENTRY_CASES:
        out = VECTORS / "entries" / case["name"]
        cache = out / "cache"
        ref.init_cache(cache)
        env = env_from(case["key"])
        reply = mark_vectors(case.get("reply"))
        ref.write_entry(
            cache,
            env,
            case["meta"],
            reply=reply,
            error=case.get("error"),
            failure_id=case.get("failureId"),
        )

        result = ref.read_entry(cache, env)
        record = dict(case)
        record["id"] = ref.entry_id(env)
        if "failureId" in case:
            assert result is None  # a failure record isn't a hit
            path = cache / record["id"][:3] / f"{record['id']}.{case['failureId']}.failure.json"
            result = ref.read_entry_file(cache, path, env, ref.Limits())
            record["expectedError"] = result.error
        else:
            assert result is not None
            if "error" in case:
                record["expectedError"] = result.error
            else:
                record["expectedReply"] = result.reply
        assert result.meta == case["meta"]
        write_text(out / "case.json", dump(record))


# Read vectors ---------------------------------------------------------------------

BASE_KEY = {"attempt": 0, "request": {"model": "anthropic/claude-haiku-4.5", "prompt": "Say hi."}}


def stored_json(entry: dict[str, Any], content: str = "reply") -> dict[str, Any]:
    """Recompute contentSha256 after editing an entry, unless the case is about it."""
    entry = dict(entry)
    entry["contentSha256"] = ref.sha256_hex(ref.canonical_bytes(entry[content]))
    return entry


class ReadCase:
    def __init__(
        self, kind: str, name: str, description: str, key: dict[str, Any] = BASE_KEY
    ) -> None:
        self.kind, self.name, self.description, self.key = kind, name, description, key
        self.dir = VECTORS / kind / name
        if self.dir.exists():
            raise AssertionError(f"duplicate case {name}")
        self.cache = self.dir / "cache"
        self.cache.mkdir(parents=True)
        # Read cases carry only llmemo.json, so the vectors' own .gitattributes keeps
        # every byte (the CRLF case depends on it).
        (self.cache / "llmemo.json").write_bytes(ref.json_file_bytes({"layoutFormat": 1}))
        self.env = env_from(key)
        self.id = ref.entry_id(self.env)
        self.limits: dict[str, int] = {}

    @property
    def entry_path(self) -> Path:
        return self.cache / self.id[:3] / f"{self.id}.json"

    def put(self, name: str, data: bytes) -> None:
        path = self.cache / name[:3] / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)

    def put_entry(self, data: bytes | dict[str, Any]) -> None:
        if isinstance(data, dict):
            data = ref.json_file_bytes(data)
        self.put(f"{self.id}.json", data)

    def put_text(self, text: str | bytes) -> str:
        data = text.encode() if isinstance(text, str) else text
        h = ref.sha256_hex(data)
        self.put(f"{h}.txt", data)
        return h

    def good_entry(self, reply: object = "Hi!", meta: dict[str, Any] = MIN_META) -> dict[str, Any]:
        """A valid stored entry for this case's key, with its files written."""
        entry, blobs = ref.build_entry(self.env, meta, reply=reply)
        for name, data in blobs.items():
            self.put(name, data)
        return entry

    def finish(self, expect: dict[str, Any]) -> None:
        limits = ref.Limits(
            entry_bytes=self.limits.get("entryBytes", ref.DEFAULT_ENTRY_LIMIT),
            blob_bytes=self.limits.get("blobBytes", ref.DEFAULT_BLOB_LIMIT),
        )
        record: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "key": self.key,
            "id": self.id,
        }
        if self.limits:
            record["limits"] = self.limits
        try:
            result = ref.read_entry(self.cache, self.env, limits)
        except ref.FormatError as e:
            if "error" not in expect:
                raise
            assert e.code == expect["error"], (self.name, e)
            record["expectedErrorCode"] = e.code
        else:
            assert "error" not in expect, f"{self.name}: expected {expect['error']}"
            assert result is not None, f"{self.name}: miss"
            assert ref.canonical(result.reply) == ref.canonical(expect["reply"]), self.name
            record["expectedReply"] = expect["reply"]
        write_text(self.dir / "case.json", dump(record))


def gen_read_ok() -> None:
    c = ReadCase(
        "read-ok",
        "crlf-and-layout",
        "Readers parse entries; they don't compare bytes (§6.2). This file has "
        "CRLF line endings, 4-space indentation, and members out of order.",
    )
    entry = c.good_entry(reply={"text": "Hi!", "n": 1})
    text = json.dumps({k: entry[k] for k in reversed(list(entry))}, indent=4, ensure_ascii=False)
    c.put_entry((text.replace("\n", "\r\n") + "\r\n").encode())
    c.finish({"reply": {"text": "Hi!", "n": 1}})

    c = ReadCase("read-ok", "compact", "A single-line entry with no final newline.")
    entry = c.good_entry()
    c.put_entry(ref.canonical(entry).encode())
    c.finish({"reply": "Hi!"})

    c = ReadCase(
        "read-ok",
        "inline-long-text",
        "A multi-line string stored inline. A writer would have used a .txt "
        "file, but readers accept either form (§4.4).",
    )
    entry = c.good_entry()
    entry["reply"] = "line 1\nline 2\n" + "x" * 600
    c.put_entry(stored_json(entry))
    c.finish({"reply": "line 1\nline 2\n" + "x" * 600})

    c = ReadCase(
        "read-ok", "short-text-in-file", "A short string stored as a reference. Readers accept it."
    )
    entry = c.good_entry()
    entry["reply"] = {"$llmemo.text": c.put_text("Hi!")}
    c.put_entry(stored_json(entry))
    c.finish({"reply": "Hi!"})

    c = ReadCase(
        "read-ok",
        "key-text-ref",
        "A key whose request string is stored as text. The requested key holds the "
        "full string, and they match.",
        key={"attempt": 0, "request": {"system": SYSTEM_PROMPT}},
    )
    c.put_entry(c.good_entry())
    c.finish({"reply": "Hi!"})

    c = ReadCase(
        "read-ok", "unknown-meta-members", "Readers ignore metadata members they don't know (§5.4)."
    )
    entry = c.good_entry(
        meta={**MIN_META, "futureField": [1, 2], "usage": {"inputTokens": 3, "futureTokens": 4}}
    )
    c.put_entry(entry)
    c.finish({"reply": "Hi!"})

    c = ReadCase(
        "read-ok",
        "escaped-names",
        "Stored names with two or more $ before llmemo lose one $ when read.",
    )
    entry = c.good_entry(reply={"$llmemo.text": "x", "$$llmemo": "y"})
    assert set(entry["reply"]) == {"$$llmemo.text", "$$$llmemo"}
    c.put_entry(entry)
    c.finish({"reply": {"$llmemo.text": "x", "$$llmemo": "y"}})


def gen_read_errors() -> None:
    def case(name: str, description: str, error: str, key: dict[str, Any] = BASE_KEY) -> ReadCase:
        c = ReadCase("read-errors", name, description, key)
        c.expected = error  # type: ignore[attr-defined]
        return c

    def done(c: ReadCase) -> None:
        c.finish({"error": c.expected})  # type: ignore[attr-defined]

    # Layout
    c = case("no-marker", "The folder has no llmemo.json.", "not-a-cache")
    (c.cache / "llmemo.json").unlink()
    c.put_entry(c.good_entry())
    done(c)

    c = case(
        "layout-format-2",
        "llmemo.json names a layout this reader doesn't know.",
        "unsupported-format",
    )
    (c.cache / "llmemo.json").write_bytes(ref.json_file_bytes({"layoutFormat": 2}))
    c.put_entry(c.good_entry())
    done(c)

    # Size
    c = case(
        "entry-too-large",
        "The entry is over the configured limit (200 bytes here; 1 MiB by default).",
        "too-large",
    )
    c.limits = {"entryBytes": 200}
    c.put_entry(c.good_entry(reply="x" * 300))
    done(c)

    c = case(
        "blob-too-large",
        "A text file is over the configured limit (100 bytes here; 16 MiB by default).",
        "too-large",
    )
    c.limits = {"blobBytes": 100}
    c.put_entry(c.good_entry(reply="y" * 600))
    done(c)

    # JSON syntax
    c = case("truncated", "The file ends partway through.", "invalid-json")
    c.put_entry(ref.json_file_bytes(c.good_entry())[:-20])
    done(c)

    c = case(
        "duplicate-member",
        "A member name appears twice. JavaScript's JSON.parse keeps the last one silently, "
        "so a reader needs its own check.",
        "invalid-json",
    )
    entry = c.good_entry()
    text = ref.json_file_bytes(entry).decode()
    c.put_entry(
        text.replace('  "reply": "Hi!"\n', '  "reply": "Hi!",\n  "reply": "Bye!"\n').encode()
    )
    done(c)

    c = case("byte-order-mark", "The file starts with a UTF-8 byte order mark.", "invalid-json")
    c.put_entry(b"\xef\xbb\xbf" + ref.json_file_bytes(c.good_entry()))
    done(c)

    c = case("not-utf8", "The file isn't valid UTF-8.", "invalid-json")
    c.put_entry(ref.json_file_bytes(c.good_entry()).replace(b"Hi!", b"Hi\xff"))
    done(c)

    c = case("nan", "NaN isn't JSON.", "invalid-json")
    c.put_entry(ref.json_file_bytes(c.good_entry(reply=1)).replace(b'"reply": 1', b'"reply": NaN'))
    done(c)

    # Values
    for name, literal, description in [
        ("negative-zero-int", b"-0", "The reply is -0."),
        ("negative-zero-float", b"-0.0", "The reply is -0.0."),
        ("big-integer", b"9007199254740992", "The reply is 2^53."),
        ("big-float", b"1e16", "The reply is 1e16, an integer above 2^53 - 1."),
        ("overflow", b"1e400", "The reply overflows a double."),
        ("lone-surrogate", b'"\\ud800"', "The reply contains a lone surrogate."),
    ]:
        c = case(name, description + " Values in files follow §1.", "invalid-value")
        c.put_entry(
            ref.json_file_bytes(c.good_entry(reply=7)).replace(
                b'"reply": 7', b'"reply": ' + literal
            )
        )
        done(c)

    # Schema
    def edited(c: ReadCase, edit: Any, content: str = "reply") -> None:
        entry = c.good_entry()
        edit(entry)
        c.put_entry(stored_json(entry, content) if content in entry else entry)

    c = case("entry-format-2", "An entryFormat this reader doesn't know.", "unsupported-format")
    edited(c, lambda e: e.update(entryFormat=2))
    done(c)

    c = case("no-entry-format", "No entryFormat member.", "schema")
    edited(c, lambda e: e.pop("entryFormat"))
    done(c)

    c = case("no-reply-or-error", "Neither reply nor error.", "schema")
    edited(c, lambda e: e.pop("reply"))
    done(c)

    c = case("reply-and-error", "Both reply and error.", "schema")
    edited(c, lambda e: e.update(error={"kind": "other", "message": "x"}))
    done(c)

    c = case("unknown-member", "An unknown top-level member.", "schema")
    edited(c, lambda e: e.update(notes="hand-edited"))
    done(c)

    c = case("no-recorded-at", "meta has no recordedAt.", "schema")
    edited(c, lambda e: e.update(meta={"latencyMs": 3}))
    done(c)

    c = case(
        "timestamp-without-ms", "recordedAt must have exactly 3 fractional digits and Z.", "schema"
    )
    edited(c, lambda e: e.update(meta={"recordedAt": "2026-11-20T22:15:00Z"}))
    done(c)

    c = case("timestamp-with-offset", "recordedAt must be UTC with Z.", "schema")
    edited(c, lambda e: e.update(meta={"recordedAt": "2026-11-20T22:15:00.000+00:00"}))
    done(c)

    c = case("tag-not-string", "Tag values must be strings.", "schema")
    edited(c, lambda e: e.update(meta={**MIN_META, "tags": {"game": 17}}))
    done(c)

    c = case("negative-latency", "latencyMs must be a non-negative integer.", "schema")
    edited(c, lambda e: e.update(meta={**MIN_META, "latencyMs": -1}))
    done(c)

    c = case("error-unknown-member", "An error object with an unknown member.", "schema")
    edited(
        c,
        lambda e: (e.pop("reply"), e.update(error={"kind": "http", "message": "x", "headers": {}})),
        "error",
    )
    done(c)

    c = case(
        "key-extra-member", "The stored key has a member the envelope doesn't allow.", "schema"
    )
    edited(c, lambda e: e["key"].update(note="x"))
    done(c)

    for name, reply, description in [
        (
            "ref-path-traversal",
            {"$llmemo.text": "../../../../etc/passwd"},
            "A reference must be 64 lowercase hex digits, "
            "so it can't name a file outside the cache (§4.2).",
        ),
        (
            "ref-uppercase-hex",
            {"$llmemo.text": "A" * 64},
            "Reference hex digits must be lowercase.",
        ),
        (
            "ref-extra-member",
            {"$llmemo.text": "0" * 64, "x": 1},
            "A reference has exactly one member.",
        ),
        (
            "ref-unknown-kind",
            {"$llmemo.png": "0" * 64},
            "An unescaped name starting with $llmemo must be a known reference.",
        ),
        ("ref-not-string", {"$llmemo.text": 5}, "A reference's value must be a string."),
    ]:
        c = case(name, description, "schema")
        edited(c, lambda e, r=reply: e.update(reply=r))
        done(c)

    c = case("vector-ref-in-key", "Vector references may appear only in the reply.", "schema")
    edited(c, lambda e: e["key"]["request"].update(prompt={"$llmemo.f64": "0" * 64}))
    done(c)

    # Hashes
    c = case(
        "content-edited",
        "The reply was edited by hand without updating contentSha256.",
        "content-mismatch",
    )
    entry = c.good_entry()
    entry["reply"] = "Bye!"
    c.put_entry(entry)
    done(c)

    c = case(
        "key-edited",
        "The stored key was edited, so it no longer matches the request (K10).",
        "key-mismatch",
    )
    edited(c, lambda e: e["key"]["request"].update(prompt="Say bye."))
    done(c)

    c = case("file-renamed", "The file holds a valid entry for a different key.", "key-mismatch")
    other = ref.make_key({"model": "anthropic/claude-haiku-4.5", "prompt": "Say bye."})
    entry, _ = ref.build_entry(other, MIN_META, reply="Bye!")
    c.put_entry(entry)
    done(c)

    c = case("text-missing", "A referenced text file doesn't exist.", "blob-missing")
    c.put_entry(c.good_entry(reply="two\nlines"))
    h = ref.sha256_hex(b"two\nlines")
    (c.cache / h[:3] / f"{h}.txt").unlink()
    done(c)

    c = case("text-edited", "A text file used by the reply was edited.", "blob-mismatch")
    c.put_entry(c.good_entry(reply="two\nlines"))
    h = ref.sha256_hex(b"two\nlines")
    c.put(f"{h}.txt", b"two\nLINES")
    done(c)

    c = case("text-not-utf8", "A text file has the right hash but isn't UTF-8.", "blob-invalid")
    entry = c.good_entry()
    entry["reply"] = {"$llmemo.text": c.put_text(b"caf\xe9\n")}
    c.put_entry(stored_json(entry))
    done(c)

    c = case(
        "text-encoded-surrogate",
        "A text file holds a UTF-8-encoded surrogate (CESU-8), which isn't valid UTF-8.",
        "blob-invalid",
    )
    entry = c.good_entry()
    entry["reply"] = {"$llmemo.text": c.put_text(b"\xed\xa0\x80\n")}
    c.put_entry(stored_json(entry))
    done(c)

    def vector_case(name: str, description: str, ext: str, data: bytes) -> None:
        c = case(name, description, "blob-invalid")
        h = ref.sha256_hex(data)
        c.put(f"{h}.{ext}", data)
        entry = c.good_entry()
        entry["reply"] = {"embedding": {f"$llmemo.{ext}": h}}
        c.put_entry(stored_json(entry))
        done(c)

    vector_case(
        "f64-bad-length", "An .f64 file whose length isn't a multiple of 8.", "f64", b"\x00" * 12
    )
    vector_case("f64-empty", "An empty .f64 file.", "f64", b"")
    vector_case(
        "f64-nan",
        "An .f64 file holding NaN.",
        "f64",
        b"\x00" * 8 + b"\x00\x00\x00\x00\x00\x00\xf8\x7f",
    )
    vector_case("f32-infinity", "An .f32 file holding infinity.", "f32", b"\x00\x00\x80\x7f")

    c = case("vector-missing", "A referenced vector file doesn't exist.", "blob-missing")
    entry = c.good_entry()
    entry["reply"] = {"embedding": {"$llmemo.f64": "0" * 64}}
    c.put_entry(stored_json(entry))
    done(c)


def main() -> None:
    for name in GENERATED:
        path = VECTORS / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    VECTORS.mkdir(exist_ok=True)
    gen_keys()
    gen_entries()
    gen_read_ok()
    gen_read_errors()
    # Determinism check: a second run into a temporary folder gives the same entry trees.
    with tempfile.TemporaryDirectory() as tmp:
        for case in ENTRY_CASES:
            root = Path(tmp) / case["name"]
            ref.init_cache(root)
            ref.write_entry(
                root,
                env_from(case["key"]),
                case["meta"],
                reply=mark_vectors(case.get("reply")),
                error=case.get("error"),
                failure_id=case.get("failureId"),
            )
            assert tree_files(root) == tree_files(VECTORS / "entries" / case["name"] / "cache"), (
                case["name"]
            )
    print(f"wrote {VECTORS}")


if __name__ == "__main__":
    main()
