"""Runner background work (auto-imported by ``papi.core.registry.load_plugins``).

- periodic ``run:backup`` (every 15 min, cheap check): nightly SQLite backup once the Istanbul clock
  passes ``[run].backup_hour`` and today's ``backups/papi-YYYYMMDD.db`` does not exist yet (a Mac asleep
  at that hour backs up on wake); failures raise an ops alert.
"""

from __future__ import annotations

import logging
from typing import Any

from papi.core import db
from papi.core.registry import periodic
from papi.core.timeutil import now_utc
from papi.run.backup import backup_due, backup_now
from papi.run.config import run_config

log = logging.getLogger("papi.run.handlers")

BACKUP_CHECK_S = 900.0
BACKUP_PRIORITY = 60


@periodic("run:backup", BACKUP_CHECK_S, priority=BACKUP_PRIORITY)
def nightly_backup(ctx: Any) -> None:
    now = now_utc()
    if not backup_due(now, run_config().backup_hour):
        return
    try:
        backup_now(now=now)
    except Exception as exc:
        log.exception("nightly backup failed")
        from papi.notify.ops import ops_alert

        with db.transaction() as conn:
            ops_alert(conn, f"Gece yedeği alınamadı: {exc}", level="error", dedupe_key="run:backup")
        raise
