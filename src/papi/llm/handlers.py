"""LLM background work: periodic Claude environment check (B-23) with ops alerts."""

from __future__ import annotations

from papi.core.db import transaction
from papi.core.logging import get_logger
from papi.core.registry import periodic
from papi.llm.backend import check_environment
from papi.llm.call import notify_ops

log = get_logger("llm.handlers")

ENV_CHECK_SECONDS = 6 * 3600


@periodic("llm:env_check", ENV_CHECK_SECONDS, run_on_start=True)
def env_check(ctx: object) -> list[str]:
    problems = check_environment()
    if problems:
        log.warning("claude environment problems: %s", problems)
        with transaction() as conn:
            notify_ops(
                conn, "Claude ortam sorunu:\n" + "\n".join(f"• {p}" for p in problems), dedupe_key="llm:env"
            )
    return problems
