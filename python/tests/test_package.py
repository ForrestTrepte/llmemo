"""Smoke test until the core exists: the package imports and ships its type marker."""

from importlib.resources import files

import llmemo


def test_imports() -> None:
    assert llmemo.__all__ == []


def test_typed() -> None:
    # A6: callers' strict type checks see llmemo's hints only if py.typed is shipped.
    assert files("llmemo").joinpath("py.typed").is_file()
