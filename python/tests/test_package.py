"""The package imports, exports what it says, and ships its type marker."""

from importlib.resources import files

import llmemo


def test_exports() -> None:
    for name in llmemo.__all__:
        assert hasattr(llmemo, name), name
    assert "Cache" in llmemo.__all__


def test_typed() -> None:
    # A6: callers' strict type checks see llmemo's hints only if py.typed is shipped.
    assert files("llmemo").joinpath("py.typed").is_file()
