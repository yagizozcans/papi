"""Governed LLM call (contract C6): admit → complete → record_usage.

Transaction behaviour: ``call()`` is a commit boundary. It commits ``conn`` after admission (so no
snapshot or write lock is held while ``claude`` runs for up to minutes) and again after writing the
ledger row (the ledger and any auto-pause must survive the handler raising ``Deferred``). Callers
should not hold uncommitted work they would not want committed at that point.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import Connection

from papi.core.jobs import Deferred
from papi.core.logging import get_logger
from papi.core.timeutil import now_utc, to_ist
from papi.llm import governor
from papi.llm.backend import LlmBackend, get_backend
from papi.llm.types import LlmResult

log = get_logger("llm.call")

AUTH_PAUSE = timedelta(hours=1)
_RETRYABLE = ("schema", "error")


def repair_prompt(prompt: str, error: str | None) -> str:
    """The original prompt plus a repair hint naming the previous failure (04 §5.2)."""
    return (
        f"{prompt}\n\n---\nYour previous answer was rejected: {error or 'invalid output'}.\n"
        "Answer again. Return only output that validates against the provided JSON schema."
    )


def notify_ops(conn: Connection, text: str, *, dedupe_key: str) -> None:
    """Ops alert via the notify slice (C4); deduped for 1 h by ``dedupe_key``."""
    from papi.notify.ops import ops_alert

    ops_alert(conn, text, level="warning", dedupe_key=dedupe_key)


def call(
    conn: Connection,
    *,
    task: str,
    priority: str,
    prompt: str,
    system_prompt: str,
    schema: dict | None,
    analysis_id: str | None = None,
    backend: LlmBackend | None = None,
    repair_retry: bool = True,
    **kw: Any,
) -> LlmResult:
    """Run one governed backend call.

    Raises ``Deferred`` when the governor does not admit the call, all concurrency slots are busy,
    or the call hits a usage limit / auth failure (both pause the governor). ``schema``/``error``
    failures get one retry with a repair hint; the final failed result is returned to the caller.
    ``kw`` is passed to ``LlmBackend.complete`` (model, max_turns, timeout_s, mcp_config, allowed_tools).
    """
    backend = backend or get_backend()
    retries_left = 1 if repair_retry else 0
    current = prompt
    ledger_ids: list[str] = []
    while True:
        admission = governor.admit(conn, task, priority)
        if not admission.ok:
            raise Deferred(admission.retry_at or now_utc() + timedelta(minutes=15), f"llm:{admission.reason}")
        conn.commit()
        try:
            with governor.concurrency_slot():
                result = backend.complete(
                    task=task, prompt=current, system_prompt=system_prompt, schema=schema, **kw
                )
        except governor.SlotBusy as exc:
            raise Deferred(now_utc() + governor.SLOT_RETRY, f"llm:concurrency: {exc}") from exc
        ledger_ids.append(
            governor.record_usage(conn, result, task=task, priority=priority, analysis_id=analysis_id)
        )
        result.ledger_ids = list(ledger_ids)
        conn.commit()
        log.info(
            "llm call task=%s priority=%s outcome=%s cost=%.4f ms=%d models=%s",
            task,
            priority,
            result.error_kind or "ok",
            result.cost_usd_est,
            result.duration_ms,
            ",".join(result.model_ids),
        )

        if result.error_kind == "limit":
            paused = governor.pause_state(conn)
            until = (
                paused["until"] if paused and paused["until"] else now_utc() + governor.LIMIT_PAUSE_DEFAULT
            )
            notify_ops(
                conn,
                f"Claude kullanım limitine takıldı; papi Claude işlerini {to_ist(until):%d.%m %H:%M} "
                "saatine kadar durdurdu. P1 uyarıları kural tabanlı gitmeye devam ediyor.",
                dedupe_key="llm:limit",
            )
            conn.commit()
            raise Deferred(until, "llm:limit")
        if result.error_kind == "auth":
            until = now_utc() + AUTH_PAUSE
            governor.pause(conn, until, reason="auth")
            notify_ops(
                conn,
                f"Claude Code oturum/kimlik hatası: {(result.error or '')[:300]} — `claude` ile giriş yapın; "
                "papi Claude işlerini 1 saat durdurdu.",
                dedupe_key="llm:auth",
            )
            conn.commit()
            raise Deferred(until, "llm:auth")
        if result.error_kind in _RETRYABLE and retries_left:
            retries_left -= 1
            current = repair_prompt(prompt, result.error)
            continue
        return result
