from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from papi.core.schema import job, metric
from papi.run import watchdog
from papi.run.lock import AlreadyRunning, InstanceLock, holder, lock_path


def test_single_instance_lock(settings):
    path = lock_path()
    assert path == settings.paths.home / "locks" / "run.lock"
    assert holder() == (False, None)
    first = InstanceLock().acquire()
    try:
        assert holder() == (True, os.getpid())
        with pytest.raises(AlreadyRunning) as exc:
            InstanceLock().acquire()
        assert exc.value.pid == os.getpid()
    finally:
        first.release()
    assert holder() == (False, None)
    with InstanceLock() as again:
        assert again.held
    assert not again.held


class FakeClock:
    def __init__(self) -> None:
        self.wall = 1_000_000.0
        self.mono = 50.0

    def tick(self, seconds: float, *, asleep: bool = False) -> None:
        self.wall += seconds
        if not asleep:
            self.mono += seconds


def test_clock_watchdog_detects_sleep_only():
    clock = FakeClock()
    wd = watchdog.ClockWatchdog(120.0, wall=lambda: clock.wall, mono=lambda: clock.mono)
    assert wd.check() is None  # first sample
    clock.tick(5)
    assert wd.check() is None
    clock.tick(100, asleep=True)  # small jump below the threshold
    assert wd.check() is None
    clock.tick(5)
    clock.tick(8 * 3600, asleep=True)
    assert wd.check() == pytest.approx(8 * 3600)
    clock.tick(5)
    assert wd.check() is None


def test_on_wake_enqueues_one_kap_catchup_poll_and_resets_telegram(db, monkeypatch):
    resets: list[int] = []
    monkeypatch.setattr(watchdog, "reset_telegram", lambda: resets.append(1))
    first = watchdog.on_wake(3600.0)
    assert first is not None
    assert watchdog.on_wake(3700.0) is None  # one already queued
    db.rollback()
    rows = db.execute(select(job).where(job.c.kind == "periodic:kap:poll")).all()
    assert [r.id for r in rows] == [first]
    assert rows[0].status == "queued"
    assert rows[0].payload["reason"] == "wake:3600s"
    assert resets == [1, 1]
    wakes = db.execute(select(metric.c.value).where(metric.c.name == "run.wake")).scalars().all()
    assert wakes == [3600.0, 3700.0]


def test_on_wake_survives_telegram_reset_failure(db, monkeypatch):
    def boom() -> None:
        raise RuntimeError("no telegram")

    monkeypatch.setattr(watchdog, "reset_telegram", boom)
    assert watchdog.on_wake(500.0) is not None
