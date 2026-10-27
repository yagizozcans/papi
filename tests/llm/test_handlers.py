from __future__ import annotations

import importlib

from papi.llm import handlers


def test_env_check_periodic_registered_and_alerts(clean_registry, db, monkeypatch):
    mod = importlib.reload(handlers)
    assert "llm:env_check" in clean_registry.periodics
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "check_environment", lambda: ["sorun"])
    monkeypatch.setattr(mod, "notify_ops", lambda conn, text, *, dedupe_key: sent.append((dedupe_key, text)))
    assert mod.env_check(None) == ["sorun"]
    assert sent == [("llm:env", "Claude ortam sorunu:\n• sorun")]
    monkeypatch.setattr(mod, "check_environment", lambda: [])
    assert mod.env_check(None) == []
    assert len(sent) == 1
