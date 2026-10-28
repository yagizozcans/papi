from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from papi.core.config import reset_settings


@pytest.fixture
def write_config(papi_home: Path) -> Callable[[str], None]:
    """Write ``config.toml`` in the test home and drop the settings cache."""

    def _write(text: str) -> None:
        (papi_home / "config.toml").write_text(text, encoding="utf-8")
        reset_settings()

    return _write
