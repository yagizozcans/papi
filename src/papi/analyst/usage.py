"""Link ``llm_usage_ledger`` rows to the analysis row they paid for.

``papi.llm.call.call()`` writes (and commits) the ledger row before the analysis row exists, and
``llm_usage_ledger.analysis_id`` is a foreign key, so the call is made without ``analysis_id`` and the
rows of that call (incl. a repair retry) are linked once the analysis is inserted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, update

from papi.core.schema import llm_usage_ledger
from papi.llm.governor import ledger_label


def link_ledger(conn: Connection, analysis_id: str, *, task: str, priority: str, since: datetime) -> int:
    """Attach unlinked ledger rows of ``task``/``priority`` written since ``since``; returns the count."""
    res = conn.execute(
        update(llm_usage_ledger)
        .where(
            llm_usage_ledger.c.analysis_id.is_(None),
            llm_usage_ledger.c.task == task,
            llm_usage_ledger.c.priority == ledger_label(priority),
            llm_usage_ledger.c.at >= since,
        )
        .values(analysis_id=analysis_id)
    )
    return res.rowcount or 0
