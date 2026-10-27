from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from papi.core import jobs
from papi.core.schema import job, llm_usage_ledger
from papi.core.timeutil import IST
from papi.llm import governor
from papi.llm.types import LlmResult

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)  # Wednesday 12:00 Istanbul


def result(cost: float = 0.1, kind=None, reset=None, rate=None) -> LlmResult:
    return LlmResult(
        structured_output={"ok": True} if kind is None else None,
        text=None,
        model_ids=["claude-sonnet-5-5"],
        cost_usd_est=cost,
        duration_ms=1000,
        exit_code=0 if kind is None else 1,
        raw_json=None,
        error_kind=kind,
        error=None if kind is None else "x",
        limit_reset_at=reset,
        rate_limit=rate,
    )


def spend(db, cost: float, ago: timedelta, priority: str = "P1", task: str = "analyst") -> None:
    governor.record_usage(db, result(cost), task=task, priority=priority, now=NOW - ago)


def admit(db, priority: str, now: datetime = NOW) -> governor.Admission:
    return governor.admit(db, "analyst", priority, now=now)


@pytest.fixture
def post_calibration(llm_config):
    """Week-2 profile: calibration off, caps 5h=2.0, week=20.0."""
    llm_config(calibration_mode=False, share_5h=2.0, share_week=20.0, share_chat_5h=1.0)


def test_record_usage_ledger_row(db):
    governor.record_usage(db, result(0.25), task="analyst", priority="P2", analysis_id=None, now=NOW)
    governor.record_usage(db, result(0.0, kind="schema"), task="checker", priority="CHAT", now=NOW)
    rows = db.execute(select(llm_usage_ledger).order_by(llm_usage_ledger.c.task)).mappings().all()
    assert [(r["task"], r["priority"], r["outcome"]) for r in rows] == [
        ("analyst", "P2", "ok"),
        ("checker", "chat", "schema_fail"),
    ]
    assert rows[0]["model_ids"] == ["claude-sonnet-5-5"]
    assert rows[0]["cost_usd_est"] == 0.25
    assert rows[1]["error"].startswith("schema")


def test_rolling_sums_and_windows(db):
    spend(db, 1.0, timedelta(hours=1))
    spend(db, 0.5, timedelta(hours=4, minutes=59))
    spend(db, 0.7, timedelta(hours=5, minutes=1))  # outside 5h, inside week
    spend(db, 0.3, timedelta(hours=2), priority="CHAT", task="chat")
    spend(db, 9.0, timedelta(days=7, minutes=1))  # outside week
    sums = governor.usage_sums(db, NOW)
    assert sums["used_5h"] == pytest.approx(1.5)
    assert sums["used_chat_5h"] == pytest.approx(0.3)
    assert sums["used_week"] == pytest.approx(2.5)


@pytest.mark.parametrize(
    ("used_5h", "expected"),
    [
        (0.0, {"P1": True, "P2": True, "P3": True}),
        (0.9, {"P1": True, "P2": True, "P3": True}),  # < 0.5 * 2.0 = 1.0
        (1.2, {"P1": True, "P2": True, "P3": False}),  # P2 threshold 1.6
        (1.7, {"P1": True, "P2": False, "P3": False}),
        (2.0, {"P1": False, "P2": False, "P3": False}),  # hard cap
    ],
)
def test_admission_matrix_5h(db, post_calibration, used_5h, expected):
    if used_5h:
        spend(db, used_5h, timedelta(hours=1))
    got = {p: admit(db, p).ok for p in expected}
    assert got == expected


def test_denied_retry_at_is_when_window_drains(db, post_calibration):
    spend(db, 1.0, timedelta(hours=4))
    spend(db, 1.0, timedelta(hours=1))
    a = admit(db, "P1")
    assert not a.ok and a.reason == "cap:5h:P1"
    # dropping the 4h-old row brings the sum to 1.0 < 2.0: 1h from now
    assert a.retry_at == NOW + timedelta(hours=1, seconds=1)
    p3 = admit(db, "P3")  # threshold 1.0 → needs both rows to age out
    assert p3.retry_at == NOW + timedelta(hours=4, seconds=1)


def test_week_cap_blocks_everything_including_chat(db, post_calibration):
    spend(db, 20.0, timedelta(days=3))
    for p in ("CHAT", "P1", "P2"):
        a = admit(db, p)
        assert not a.ok and a.reason == "cap:week"
        assert a.retry_at == NOW + timedelta(days=4, seconds=1)


def test_chat_has_own_bucket(db, post_calibration):
    spend(db, 2.0, timedelta(hours=1))  # pipeline 5h cap exhausted
    assert admit(db, "CHAT").ok
    assert not admit(db, "P1").ok
    spend(db, 1.0, timedelta(minutes=30), priority="CHAT", task="chat")
    a = admit(db, "CHAT")
    assert not a.ok and a.reason == "cap:chat_5h"


def test_calibration_mode_only_chat_p1_p2(db, llm_config):
    llm_config(calibration_mode=True, backlog_enabled=True)
    assert admit(db, "CHAT").ok and admit(db, "P1").ok and admit(db, "P2").ok
    for p in ("P3", "P4", "BACKLOG"):
        a = admit(db, p)
        assert not a.ok and a.reason == "calibration:p1_p2_only"


def test_calibration_mode_caps_at_ten_percent(db, llm_config):
    llm_config(
        calibration_mode=True,
        share_5h=2.0,
        share_week=20.0,
        calibration_cost_per_pct_5h=0.05,
        calibration_cost_per_pct_week=0.5,
    )
    caps = governor.caps()
    assert (caps.cap_5h, caps.cap_week) == (pytest.approx(0.5), pytest.approx(5.0))
    spend(db, 0.5, timedelta(hours=1))
    assert not admit(db, "P1").ok


def test_backlog_needs_enabled_night_window_and_week_fraction(db, post_calibration, llm_config):
    night = datetime(2026, 10, 28, 3, 0, tzinfo=IST)
    a = admit(db, "BACKLOG", now=night)
    assert not a.ok and a.reason == "backlog:disabled"
    llm_config(backlog_enabled=True)
    assert admit(db, "BACKLOG", now=night).ok
    assert admit(db, "P4", now=night).ok
    day = admit(db, "BACKLOG", now=NOW)
    assert not day.ok and day.reason == "backlog:outside_night_window"
    assert day.retry_at == datetime(2026, 10, 29, 1, 0, tzinfo=IST)
    spend(db, 10.0, timedelta(days=2))  # 10 >= 0.5 * 20
    a = admit(db, "BACKLOG", now=night)
    assert not a.ok and a.reason == "backlog:week_fraction"


def test_night_window_across_midnight(db, post_calibration, llm_config):
    llm_config(backlog_enabled=True, night_window="23:00-02:00")
    assert admit(db, "BACKLOG", now=datetime(2026, 10, 28, 23, 30, tzinfo=IST)).ok
    assert admit(db, "BACKLOG", now=datetime(2026, 10, 29, 1, 30, tzinfo=IST)).ok
    assert not admit(db, "BACKLOG", now=datetime(2026, 10, 29, 2, 30, tzinfo=IST)).ok


def test_focus_window_limits_priority(db, post_calibration, llm_config):
    llm_config(
        raw='[[llm.focus]]\ndays = ["mon", "tue", "wed", "thu", "fri"]\nstart = "09:00"\nend = "18:00"\n'
        'max_priority = "P1"'
    )
    assert admit(db, "P1").ok
    assert admit(db, "CHAT").ok
    a = admit(db, "P2")
    assert not a.ok and a.reason == "focus:max_P1"
    assert a.retry_at == datetime(2026, 10, 28, 18, 0, tzinfo=IST)
    evening = datetime(2026, 10, 28, 19, 0, tzinfo=IST)
    assert admit(db, "P2", now=evening).ok
    saturday = datetime(2026, 10, 31, 12, 0, tzinfo=IST)
    assert admit(db, "P3", now=saturday).ok


def test_pause_resume_and_expiry(db, post_calibration):
    governor.pause(db, NOW + timedelta(hours=3), reason="manual", now=NOW)
    for p in ("CHAT", "P1"):
        a = admit(db, p)
        assert not a.ok and a.reason == "paused:manual" and a.retry_at == NOW + timedelta(hours=3)
    assert admit(db, "P1", now=NOW + timedelta(hours=3, seconds=1)).ok
    governor.pause(db, None, now=NOW)
    a = admit(db, "P1", now=NOW + timedelta(days=30))
    assert not a.ok and a.retry_at == NOW + timedelta(days=30, hours=1)
    governor.resume(db)
    assert admit(db, "P1").ok


def test_limit_hit_auto_pauses(db, post_calibration):
    reset = NOW + timedelta(hours=2)
    governor.record_usage(db, result(0.0, kind="limit", reset=reset), task="analyst", priority="P1", now=NOW)
    a = admit(db, "P1")
    assert not a.ok and a.reason == "paused:limit" and a.retry_at == reset
    governor.resume(db)
    governor.record_usage(db, result(0.0, kind="limit"), task="analyst", priority="P1", now=NOW)
    assert admit(db, "P1").retry_at == NOW + timedelta(hours=1)
    row = db.execute(select(llm_usage_ledger.c.outcome).order_by(llm_usage_ledger.c.at.desc())).first()
    assert row.outcome == "limit_hit"


def test_unknown_priority_rejected(db):
    with pytest.raises(ValueError):
        admit(db, "P9")


def test_status(db, post_calibration, llm_config):
    llm_config(raw='[[llm.focus]]\ndays = ["wed"]\nstart = "09:00"\nend = "18:00"\nmax_priority = "P2"')
    spend(db, 0.4, timedelta(hours=1))
    spend(db, 0.2, timedelta(hours=2))
    spend(db, 0.1, timedelta(hours=1), priority="CHAT", task="chat")
    rate = {
        "status": "allowed",
        "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1793158200}},
    }
    governor.record_usage(db, result(0.0, rate=rate), task="analyst", priority="P1", now=NOW)
    jobs.enqueue(db, "analyst:run", {}, priority=jobs.PRIORITY_P2)
    db.execute(job.update().values(last_error="llm:cap:5h:P2"))
    governor.pause(db, NOW + timedelta(hours=1), reason="manual", now=NOW)
    st = governor.status(db, now=NOW)
    assert st["paused"] and st["pause_reason"] == "manual" and st["paused_until"] == NOW + timedelta(hours=1)
    assert st["used_5h"] == pytest.approx(0.6)
    assert st["used_chat_5h"] == pytest.approx(0.1)
    assert st["used_week"] == pytest.approx(0.7)
    assert (st["cap_5h"], st["cap_week"], st["cap_chat_5h"]) == (2.0, 20.0, 1.0)
    assert st["by_task"]["analyst"]["calls"] == 3
    assert st["by_task"]["chat"]["cost"] == pytest.approx(0.1)
    assert st["focus"]["max_priority"] == "P2"
    assert st["queue"] == {"llm_deferred": {"P2": 1}, "llm_deferred_total": 1}
    bars = st["subscription_bars"]
    assert bars["five_hour"]["utilization"] == 0.2
    assert bars["five_hour"]["resets_at"] == datetime.fromtimestamp(1793158200, UTC)


def test_concurrency_slot_cap(llm_config):
    llm_config(concurrency=1)
    with governor.concurrency_slot() as n:
        assert n == 0
        with pytest.raises(governor.SlotBusy), governor.concurrency_slot(wait_s=0.2):
            pass
    with governor.concurrency_slot() as n:
        assert n == 0


def test_concurrency_slot_two_and_waiting(llm_config):
    llm_config(concurrency=2)
    with governor.concurrency_slot() as a, governor.concurrency_slot() as b:
        assert {a, b} == {0, 1}
        with pytest.raises(governor.SlotBusy), governor.concurrency_slot(wait_s=0.1):
            pass

    llm_config(concurrency=1)
    released = threading.Event()

    def holder() -> None:
        with governor.concurrency_slot():
            released.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        time.sleep(0.2)
        threading.Timer(0.3, released.set).start()
        with governor.concurrency_slot(wait_s=3.0) as n:  # waits for the holder to release
            assert n == 0
    finally:
        released.set()
        t.join()
