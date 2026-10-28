"""SQLite backups (09 §5): online backup API → ``$PAPI_HOME/backups/papi-YYYYMMDD.db`` (Istanbul date),
retention ``backup.keep_daily`` newest days + the newest file of each of the last ``backup.keep_weekly``
ISO weeks, and a guarded restore.

The raw store (``raw/``) is write-once content-addressed files and is not copied: it is covered by
Time Machine (or an rsync to an external disk). A restored DB may reference raw objects newer than the
backup's state; those stay valid because raw files are never rewritten.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from papi.core import db, metrics
from papi.core.config import Settings, get_settings
from papi.core.state import set_state
from papi.core.timeutil import ist_date, now_utc, to_ist
from papi.run.lock import AlreadyRunning, InstanceLock

log = logging.getLogger("papi.run.backup")

BACKUP_RE = re.compile(r"^papi-(\d{8})\.db$")
LAST_BACKUP_KEY = "run:backup:last"


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    bytes: int
    duration_s: float
    pruned: list[Path]


def backup_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).paths.backups


def backup_path_for(day: date, settings: Settings | None = None) -> Path:
    return backup_dir(settings) / f"papi-{day:%Y%m%d}.db"


def list_backups(settings: Settings | None = None) -> list[tuple[date, Path]]:
    """Dated backups, newest first."""
    out: list[tuple[date, Path]] = []
    d = backup_dir(settings)
    if not d.is_dir():
        return out
    for p in d.iterdir():
        m = BACKUP_RE.match(p.name)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y%m%d").date(), p))
    return sorted(out, reverse=True)


def _copy(src: Path, dst: Path) -> None:
    """Consistent copy of ``src`` into ``dst`` with the online backup API, then verify ``dst``."""
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")  # self-contained single file
            ok = target.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise BackupError(f"backup copy failed quick_check: {ok}")
        finally:
            target.close()
    finally:
        source.close()
    os.replace(tmp, dst)


def retained(days: list[date], keep_daily: int, keep_weekly: int) -> set[date]:
    """Dates kept: the ``keep_daily`` newest, plus the newest date of each of the ``keep_weekly``
    newest ISO weeks."""
    ordered = sorted(set(days), reverse=True)
    keep = set(ordered[: max(keep_daily, 0)])
    weeks: dict[tuple[int, int], date] = {}
    for d in ordered:
        weeks.setdefault(d.isocalendar()[:2], d)
    for wk in sorted(weeks, reverse=True)[: max(keep_weekly, 0)]:
        keep.add(weeks[wk])
    return keep


def prune(settings: Settings | None = None) -> list[Path]:
    s = settings or get_settings()
    backups = list_backups(s)
    keep = retained([d for d, _ in backups], s.backup.keep_daily, s.backup.keep_weekly)
    removed = []
    for d, p in backups:
        if d not in keep:
            p.unlink(missing_ok=True)
            removed.append(p)
    if removed:
        log.info("pruned %d old backups", len(removed))
    return removed


def backup_now(*, now: datetime | None = None, settings: Settings | None = None) -> BackupResult:
    """Back up ``papi.db`` to today's file (replacing a same-day file), prune, record metric + state."""
    s = settings or get_settings()
    now = now or now_utc()
    src = s.paths.db
    if not src.exists():
        raise BackupError(f"database not found: {src}")
    dst = backup_path_for(ist_date(now), s)
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        _copy(src, dst)
    except Exception as exc:
        with contextlib.suppress(Exception):
            metrics.record("run.backup.fail", 1, error=type(exc).__name__)
        raise
    size = dst.stat().st_size
    duration = time.monotonic() - t0
    pruned = prune(s)
    with db.transaction() as conn:
        set_state(
            conn,
            LAST_BACKUP_KEY,
            {"path": str(dst), "at": now.isoformat(), "bytes": size, "duration_s": round(duration, 3)},
        )
        metrics.record("run.backup.ok", size, conn=conn)
    log.info("backup written: %s (%d bytes, %.1f s)", dst, size, duration)
    return BackupResult(dst, size, duration, pruned)


def backup_due(now: datetime, hour: int, settings: Settings | None = None) -> bool:
    """Nightly rule: at/after ``hour`` (Istanbul) and no backup file for today's Istanbul date yet."""
    ist = to_ist(now)
    return ist.hour >= hour and not backup_path_for(ist.date(), settings).exists()


def resolve_backup(ref: str, settings: Settings | None = None) -> Path:
    """``ref``: a path, a file name in the backups dir, or a date ``YYYYMMDD``/``YYYY-MM-DD``."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p.resolve()
    d = backup_dir(settings)
    for cand in (d / ref, d / f"papi-{ref.replace('-', '')}.db"):
        if cand.is_file():
            return cand
    raise BackupError(f"backup not found: {ref}")


def _validate(path: Path) -> None:
    with path.open("rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            raise BackupError(f"not a SQLite database: {path}")
    conn = sqlite3.connect(path)
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            raise BackupError(f"backup failed quick_check: {ok}")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'alembic_version'").fetchone() is None:
            raise BackupError(f"not a papi database (no alembic_version): {path}")
    finally:
        conn.close()


@dataclass
class RestoreResult:
    restored_from: Path
    safety_copy: Path | None


def restore(ref: str, *, settings: Settings | None = None) -> RestoreResult:
    """Replace ``papi.db`` with a backup. Refuses while ``papi run`` holds the instance lock (and holds
    it itself meanwhile); copies the current DB to ``backups/pre-restore-<UTC ts>.db`` first."""
    s = settings or get_settings()
    src = resolve_backup(ref, s)
    _validate(src)
    guard = InstanceLock()
    try:
        guard.acquire()
    except AlreadyRunning as exc:
        raise BackupError(f"{exc}; önce durdurun (Ctrl-C ya da `papi run service uninstall`)") from exc
    try:
        dst = s.paths.db
        safety: Path | None = None
        if dst.exists():
            safety = backup_dir(s) / f"pre-restore-{now_utc():%Y%m%dT%H%M%SZ}.db"
            safety.parent.mkdir(parents=True, exist_ok=True)
            _copy(dst, safety)
        db.reset_engines()
        source = sqlite3.connect(src)
        try:
            target = sqlite3.connect(dst)
            try:
                source.backup(target)
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                target.close()
        finally:
            source.close()
        log.warning("database restored from %s (safety copy %s)", src, safety)
        return RestoreResult(src, safety)
    finally:
        guard.release()
