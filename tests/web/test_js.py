"""Client-side logic (feed ordering, live-stream lifecycle) via ``bun test`` when bun is installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"


def test_client_js() -> None:
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("bun not installed")
    proc = subprocess.run(
        [bun, "test", str(JS_TESTS)], capture_output=True, text=True, timeout=120, cwd=JS_TESTS.parents[2]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
