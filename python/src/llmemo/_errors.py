"""Exceptions raised by llmemo."""

from __future__ import annotations

from typing import Any

# The codes of spec/format.md §8. Both packages and their tests agree on these.
FORMAT_CODES = frozenset(
    {
        "not-a-cache",
        "unsupported-format",
        "too-large",
        "invalid-json",
        "invalid-value",
        "schema",
        "content-mismatch",
        "key-mismatch",
        "blob-missing",
        "blob-mismatch",
        "blob-invalid",
        "invalid-key",
    }
)


class LlmemoError(Exception):
    """Base class for every error llmemo raises."""


class FormatError(LlmemoError, ValueError):
    """A key or value breaks the format, or the cache is damaged (spec §8).

    `code` is one of the spec's error codes, e.g. `invalid-value` or `key-mismatch`.
    """

    def __init__(self, code: str, message: str) -> None:
        assert code in FORMAT_CODES, code
        super().__init__(f"{code}: {message}")
        self.code = code


class CacheMiss(LlmemoError):
    """A lookup missed in replay mode (M2)."""

    def __init__(self, entry_id: str, label: str | None, variant: str | None, attempt: int) -> None:
        parts = [f"label={label!r}"] if label is not None else []
        if variant is not None:
            parts.append(f"variant={variant!r}")
        parts += [f"attempt={attempt}", f"entry {entry_id}"]
        super().__init__(
            "llmemo cache miss in replay mode: "
            + ", ".join(parts)
            + ". To record it, run with LLMEMO_MODE=record (this calls the backend and may cost"
            " money), then commit the new files in the cache folder."
        )
        self.entry_id = entry_id
        self.label = label
        self.variant = variant
        self.attempt = attempt


class SpendLimitExceeded(LlmemoError):
    """The run's spending limit was reached, so the backend wasn't called (M6)."""


class BackendError(LlmemoError):
    """A backend call failed without a reply: a timeout, network or HTTP error, or crash.

    Backends raise this so llmemo can save the failure (E5, spec §5.3). `kind` should be
    one of `timeout`, `network`, `http`, `process`, or `other`. Never put secrets in
    `message` or `details`.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        http_status: int | None = None,
        exit_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.http_status = http_status
        self.exit_code = exit_code
        self.details = details

    def to_json(self) -> dict[str, Any]:
        """The error object of spec §5.3."""
        out: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.http_status is not None:
            out["httpStatus"] = self.http_status
        if self.exit_code is not None:
            out["exitCode"] = self.exit_code
        if self.details is not None:
            out["details"] = self.details
        return out

    @classmethod
    def from_json(cls, error: dict[str, Any]) -> BackendError:
        return cls(
            error["kind"],
            error["message"],
            http_status=error.get("httpStatus"),
            exit_code=error.get("exitCode"),
            details=error.get("details"),
        )


class ReplayedError(BackendError):
    """A hit on an error entry (spec §5.3): the stored failure, raised again on purpose."""

    def __init__(self, entry_id: str, error: dict[str, Any]) -> None:
        super().__init__(
            error["kind"],
            error["message"],
            http_status=error.get("httpStatus"),
            exit_code=error.get("exitCode"),
            details=error.get("details"),
        )
        self.entry_id = entry_id
        self.error = error
