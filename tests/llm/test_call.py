from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from papi.core.db import connect
from papi.core.jobs import Deferred
from papi.core.schema import llm_usage_ledger
from papi.core.timeutil import now_utc
from papi.llm import call as call_mod
from papi.llm import governor
from papi.llm.call import call
from papi.llm.types import LlmResult

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def res(kind=None, cost=0.1, reset=None) -> LlmResult:
    return LlmResult(
        structured_output={"ok": True} if kind is None else None,
        text=None,
        model_ids=["m"],
        cost_usd_est=cost,
        duration_ms=10,
        exit_code=0 if kind is None else 1,
        raw_json=None,
        error_kind=kind,
        error=None if kind is None else f"{kind} failure",
        limit_reset_at=reset,
    )


class ScriptedBackend:
    def __init__(self, *results: LlmResult) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def complete(self, **kw) -> LlmResult:
        self.calls.append(kw)
        return self.results.pop(0)


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        call_mod, "notify_ops", lambda conn, text, *, dedupe_key: sent.append((dedupe_key, text))
    )
    return sent


def _call(db, backend, priority="P1", **kw):
    return call(
        db,
        task="analyst",
        priority=priority,
        prompt="P",
        system_prompt="S",
        schema=SCHEMA,
        backend=backend,
        **kw,
    )


def _ledger_count() -> int:
    with connect() as c:
        return c.execute(select(func.count()).select_from(llm_usage_ledger)).scalar_one()


def test_call_success_records_and_commits(db, alerts):
    backend = ScriptedBackend(res())
    out = _call(db, backend, model="opus", timeout_s=5)
    assert out.ok and out.structured_output == {"ok": True}
    assert backend.calls[0]["model"] == "opus" and backend.calls[0]["timeout_s"] == 5
    assert backend.calls[0]["schema"] == SCHEMA
    assert _ledger_count() == 1  # visible to another connection → committed
    assert alerts == []


def test_call_not_admitted_defers_without_backend(db, alerts):
    until = now_utc() + timedelta(hours=2)
    governor.pause(db, until, reason="manual")
    backend = ScriptedBackend()
    with pytest.raises(Deferred) as exc:
        _call(db, backend)
    assert exc.value.until == until
    assert str(exc.value).startswith("llm:paused:manual")
    assert backend.calls == []


def test_call_limit_pauses_alerts_and_defers(db, alerts):
    reset = now_utc() + timedelta(hours=2)
    backend = ScriptedBackend(res("limit", cost=0.0, reset=reset))
    with pytest.raises(Deferred) as exc:
        _call(db, backend)
    assert exc.value.until == reset
    assert [k for k, _ in alerts] == ["llm:limit"]
    db.rollback()
    paused = governor.pause_state(db)
    assert paused["reason"] == "limit" and paused["until"] == reset
    assert _ledger_count() == 1
    with pytest.raises(Deferred):
        _call(db, ScriptedBackend(res()), priority="CHAT")


def test_call_auth_pauses_and_defers(db, alerts):
    backend = ScriptedBackend(res("auth", cost=0.0))
    with pytest.raises(Deferred) as exc:
        _call(db, backend)
    assert exc.value.until > datetime.now(UTC) + timedelta(minutes=59)
    assert [k for k, _ in alerts] == ["llm:auth"]
    assert governor.pause_state(db)["reason"] == "auth"


def test_call_schema_failure_retries_once_with_repair_hint(db, alerts):
    backend = ScriptedBackend(res("schema"), res())
    out = _call(db, backend)
    assert out.ok
    assert len(backend.calls) == 2
    assert backend.calls[1]["prompt"].startswith(
        "P\n\n---\nYour previous answer was rejected: schema failure"
    )
    assert _ledger_count() == 2


def test_call_returns_final_failure_after_retry(db, alerts):
    backend = ScriptedBackend(res("error"), res("schema"))
    out = _call(db, backend)
    assert out.error_kind == "schema" and len(backend.calls) == 2
    backend = ScriptedBackend(res("timeout"))
    assert _call(db, backend).error_kind == "timeout" and len(backend.calls) == 1
    backend = ScriptedBackend(res("error"))
    assert _call(db, backend, repair_retry=False).error_kind == "error" and len(backend.calls) == 1


def test_call_concurrency_busy_defers(db, alerts, llm_config, monkeypatch):
    llm_config(concurrency=1)
    real_slot = governor.concurrency_slot
    monkeypatch.setattr(governor, "concurrency_slot", lambda: real_slot(wait_s=0.1))
    backend = ScriptedBackend(res())
    with real_slot(), pytest.raises(Deferred) as exc:
        _call(db, backend)
    assert str(exc.value).startswith("llm:concurrency")
    assert backend.calls == []


def test_call_end_to_end_with_fake_claude(db, alerts, fake_claude):
    out = call(db, task="analyst", priority="P2", prompt="girdi", system_prompt="sistem", schema=SCHEMA)
    assert out.ok and out.structured_output == {"ok": True}
    assert fake_claude.calls()[0]["stdin"] == "girdi"
    st = governor.status(db)
    assert st["used_5h"] == pytest.approx(0.0018)
    assert st["subscription_bars"]["seven_day"]["utilization"] == 0.35

    fake_claude.mode("limit")
    with pytest.raises(Deferred) as exc:
        call(db, task="analyst", priority="P1", prompt="x", system_prompt="s", schema=SCHEMA)
    db.rollback()
    assert exc.value.until == governor.pause_state(db)["until"]
    assert governor.pause_state(db)["reason"] == "limit"
