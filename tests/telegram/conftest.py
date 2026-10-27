from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx

from papi.core.config import Settings
from papi.core.registry import Registry
from papi.telegram.api import reset_client
from tests.telegram.helpers import FakeTelegram, configure


@pytest.fixture(autouse=True)
def _hermetic(clean_registry: Registry) -> Iterator[None]:
    """No other slice's handlers; a fresh Telegram client per test."""
    reset_client()
    yield
    reset_client()


@pytest.fixture
def fake_tg(settings: Settings, monkeypatch: Any) -> Iterator[FakeTelegram]:
    with respx.mock(assert_all_called=False) as router:
        configure(settings, monkeypatch)
        yield FakeTelegram(router)
