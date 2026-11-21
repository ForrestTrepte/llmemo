"""The examples run, replaying their committed caches, so they can't go stale."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.mark.parametrize("script", sorted(EXAMPLES.glob("*.py")), ids=lambda p: p.name)
def test_example_replays(script: Path) -> None:
    # No API key, no `claude` on the PATH, and replay mode: a miss fails instead of calling.
    env = {"PATH": os.path.dirname(sys.executable), "LLMEMO_MODE": "replay"}
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert "cached: True" in done.stdout and "cached: False" not in done.stdout
    assert "Spent $0.000000" in done.stdout
