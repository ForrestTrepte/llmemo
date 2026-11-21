"""Shared fixtures."""

from __future__ import annotations

import pytest

from llmemo import Ledger, SpendLimit


@pytest.fixture(scope="session")
def spend_limit() -> SpendLimit:
    """One limit for every live call in a test run: at most $0.10 (CLAUDE.md, M6)."""
    return SpendLimit(0.10)


@pytest.fixture(scope="session")
def ledger() -> Ledger:
    """llmemo's own development spending, in llm-spend/ at the repo root (M7). Rows are
    written only when a live test records, which CI never does."""
    return Ledger()
