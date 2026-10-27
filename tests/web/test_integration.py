"""The dashboard against the real sibling slices (skipped when a slice is not installed)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from papi.web import integrations

REAL_POSITIONS = integrations.positions
REAL_PENDING = integrations.pending_actions
REAL_TAXONOMY = integrations._taxonomy


def test_real_portfolio_positions(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("papi.portfolio.service")
    monkeypatch.setattr(integrations, "positions", REAL_POSITIONS)
    monkeypatch.setattr(integrations, "pending_actions", REAL_PENDING)
    html = client.get("/portfolio").text
    assert 'href="/company/ASELS">ASELS</a>' in html and "Onay bekleyen kurumsal işlem yok" in html
    feed = client.get("/?scope=holdings").text
    assert 'badge hold">HOLDING' in feed and "ASELS temettü düzeltmesi" in feed


def test_real_taxonomy_labels(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    taxonomy = pytest.importorskip("papi.triage.taxonomy")
    REAL_TAXONOMY.cache_clear()
    monkeypatch.setattr(integrations, "_taxonomy", REAL_TAXONOMY)
    tax = taxonomy.load()
    html = client.get("/?scope=all").text
    assert tax.label("distribution.dividend.cash", "tr") in html
    fam = tax.family_of("distribution.dividend.cash")
    heads = client.get(f"/?scope=all&family={fam}").text
    assert "ASELS temettü düzeltmesi" in heads and "GARAN faaliyet raporu" not in heads
