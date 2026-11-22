"""P2 "act" endpoints of the React UI (14 §6, slice A): manual trades (preview + commit + undo), pending
corporate actions, watchlists, LLM governor controls and alert rules.

Thin typed wrappers over the CLI/Telegram service layers (``papi.portfolio.service``, ``papi.llm.governor``,
``papi.notify.store``). Domain errors (``ValueError`` with a Turkish message) become ``422`` / ``404`` /
``409`` bodies shaped like :class:`papi.web.schemas.ErrorOut`.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, select, update

from papi.core import db
from papi.core.schema import alert_rule, notification
from papi.core.timeutil import ist_date, now_utc
from papi.llm import governor
from papi.notify import macos
from papi.notify.rules import FIELDS, RuleSyntaxError, parse
from papi.notify.store import CHANNELS, add_rule, list_rules
from papi.portfolio import service
from papi.telegram import outbox
from papi.telegram.api import TelegramError
from papi.web import schemas
from papi.web import schemas_actions as sa
from papi.web.deps import get_conn
from papi.web.schemas import ErrorOut

router = APIRouter(prefix="/v1")
Conn = Annotated[Connection, Depends(get_conn)]

_ERR: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


class _DryRun(Exception):
    """Raised inside a write transaction to roll it back after the preview was computed."""


def _fail(status: int, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail=message)


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(float(value)))


# ------------------------------------------------------------------------------------------ trades


def _snap(conn: Connection, instrument_id: str | None) -> sa.PositionSnap | None:
    if instrument_id is None:
        return None
    pos = service.holding(conn, instrument_id)
    if pos is None:
        return sa.PositionSnap(quantity=0.0, avg_cost=None, cost_total=0.0, market_value=None, weight=None)
    return sa.PositionSnap(
        quantity=float(pos.quantity),
        avg_cost=None if pos.avg_cost is None else float(pos.avg_cost),
        cost_total=float(pos.cost_total),
        market_value=None if pos.market_value is None else float(pos.market_value),
        weight=pos.weight,
    )


def _proposal(conn: Connection, body: sa.TxnIn) -> service.ProposedTxn:
    fees = _dec(body.fees) or Decimal(0)
    if body.type in ("fee", "tax"):  # the amount of a cost-only row travels in ``fees`` (as in the CLI)
        fees = fees or abs(_dec(body.quantity) or Decimal(0))
    p = service.ProposedTxn(
        ticker=(body.ticker or "").strip().upper() or None,
        type=body.type,
        quantity=_dec(body.quantity) or Decimal(0),
        price=_dec(body.price),
        fees=fees,
        trade_date=body.trade_date or ist_date(),
        source="manual",
        note=(body.note or "").strip() or None,
    )
    checked = service.validate_proposal(conn, p)
    if checked.ticker and checked.instrument_id is None:
        # validate_proposal puts the "unknown ticker, did you mean ..." text first among the warnings
        _fail(422, next((w for w in checked.warnings if w.startswith("Bilinmeyen")), "Hisse bulunamadı."))
    return checked


@router.post("/portfolio/transactions", response_model=sa.TxnResultOut, responses=_ERR)
def post_transaction(body: sa.TxnIn) -> sa.TxnResultOut:
    """Record a manual transaction, or with ``dry_run`` only compute the position change (nothing is
    written: the insert runs in a transaction that is rolled back)."""
    result: sa.TxnResultOut | None = None
    try:
        with db.transaction() as conn:
            checked = _proposal(conn, body)
            before = _snap(conn, checked.instrument_id)
            try:
                tid = service.record_transaction(conn, checked)
            except ValueError as exc:
                _fail(422, str(exc))
            after = _snap(conn, checked.instrument_id)
            name = None
            if checked.ticker:
                m = service.resolve_instrument(conn, checked.ticker, at=checked.trade_date, limit=1)
                name = m[0].issuer_name if m else None
            result = sa.TxnResultOut(
                committed=not body.dry_run,
                txn_id=None if body.dry_run else tid,
                type=checked.type,
                ticker=checked.ticker,
                instrument_id=checked.instrument_id,
                name=name,
                quantity=float(checked.quantity),
                price=None if checked.price is None else float(checked.price),
                fees=float(checked.fees),
                trade_date=checked.trade_date,
                gross=None if checked.price is None else float(checked.quantity * checked.price),
                warnings=checked.warnings,
                before=before,
                after=after,
            )
            if body.dry_run:
                raise _DryRun
    except _DryRun:
        pass
    assert result is not None
    return result


def _txn_out(conn: Connection, txn_id: str) -> schemas.TxnOut:
    for r in service.transactions(conn, limit=None):
        if r["id"] == txn_id:
            return schemas.TxnOut.model_validate({**r, "reversed": r["reversed_by"] is not None})
    _fail(404, f"işlem bulunamadı: {txn_id}")


@router.post("/portfolio/transactions/undo", response_model=sa.UndoOut, responses=_ERR)
def undo_transaction(body: sa.UndoIn) -> sa.UndoOut:
    """Append the reversing entry of ``txn_id`` (default: the most recently entered live transaction)."""
    with db.transaction() as conn:
        target = body.txn_id
        if target is None:
            live = [
                r
                for r in service.transactions(conn, limit=None)
                if not r["reverses_id"] and not r["reversed_by"]
            ]
            if not live:
                _fail(404, "Geri alınacak işlem yok.")
            target = max(live, key=lambda r: (r["created_at"], r["id"]))["id"]
        try:
            rid = service.reverse_transaction(conn, target, reason=(body.reason or "").strip() or None)
        except ValueError as exc:
            _fail(404 if "bulunamadı" in str(exc) else 409, str(exc))
        return sa.UndoOut(undone=_txn_out(conn, target), reversal_id=rid)


# ------------------------------------------------------------------------------------------ pending actions


def _pending_error(exc: ValueError) -> NoReturn:
    msg = str(exc)
    _fail(404 if "bulunamadı" in msg else 409 if "zaten" in msg else 422, msg)


@router.post("/portfolio/pending/{pending_id}/apply", response_model=sa.PendingApplyOut, responses=_ERR)
def apply_pending(pending_id: str, body: sa.PendingApplyIn | None = None) -> sa.PendingApplyOut:
    override = _dec(body.quantity_override) if body else None
    try:
        with db.transaction() as conn:
            txn_id = service.apply_pending_action(conn, pending_id, quantity_override=override)
    except (ValueError, KeyError, ArithmeticError) as exc:
        _pending_error(exc if isinstance(exc, ValueError) else ValueError(f"uygulanamadı: {exc}"))
    return sa.PendingApplyOut(pending_id=pending_id, txn_id=txn_id)


@router.post("/portfolio/pending/{pending_id}/dismiss", response_model=sa.PendingDismissOut, responses=_ERR)
def dismiss_pending(pending_id: str) -> sa.PendingDismissOut:
    try:
        with db.transaction() as conn:
            service.dismiss_pending_action(conn, pending_id)
    except ValueError as exc:
        _pending_error(exc)
    return sa.PendingDismissOut(pending_id=pending_id)


# ------------------------------------------------------------------------------------------ watchlist


def _summary(conn: Connection, name: str) -> sa.WatchlistSummary | None:
    for w in service.watchlists(conn):
        if w["name"] == name:
            return sa.WatchlistSummary(
                id=w["id"], name=w["name"], tickers=w["tickers"], sectors=w["sectors"], indices=w["indices"]
            )
    return None


def _key(s: sa.WatchlistSummary | None) -> tuple[Any, ...]:
    return (tuple(s.tickers), tuple(s.sectors), tuple(s.indices)) if s else ()


@router.post("/watchlist", response_model=sa.WatchOut, responses=_ERR)
def watch(body: sa.WatchIn) -> sa.WatchOut:
    """Add a ticker, ``XU030`` / ``endeks:XU030`` or ``sektor:<kod>`` to a watchlist (created if missing)."""
    try:
        with db.transaction() as conn:
            before = _summary(conn, body.watchlist)
            service.watch(conn, body.ref, body.watchlist)
            after = _summary(conn, body.watchlist)
    except ValueError as exc:
        _fail(422, str(exc))
    assert after is not None
    return sa.WatchOut(ref=body.ref, changed=_key(before) != _key(after), watchlist=after)


@router.delete("/watchlist", response_model=sa.UnwatchOut, responses=_ERR)
def unwatch(
    ref: Annotated[str, Query(min_length=1, max_length=80)],
    watchlist: Annotated[str, Query(min_length=1, max_length=80)] = "Ana",
) -> sa.UnwatchOut:
    try:
        with db.transaction() as conn:
            removed = service.unwatch(conn, ref, watchlist)
            summary = _summary(conn, watchlist)
    except ValueError as exc:
        _fail(422, str(exc))
    return sa.UnwatchOut(ref=ref, removed=removed, watchlist=summary)


# ------------------------------------------------------------------------------------------ LLM


def _llm_status(conn: Connection) -> sa.LlmStatusOut:
    st = governor.status(conn)
    bars = st["subscription_bars"]

    def bar(b: dict[str, Any] | None) -> sa.BarOut | None:
        return sa.BarOut(utilization=b["utilization"], resets_at=b["resets_at"]) if b else None

    return sa.LlmStatusOut(
        paused=st["paused"],
        paused_until=st["paused_until"],
        pause_reason=st["pause_reason"],
        used_5h=st["used_5h"],
        cap_5h=st["cap_5h"],
        used_chat_5h=st["used_chat_5h"],
        cap_chat_5h=st["cap_chat_5h"],
        used_week=st["used_week"],
        cap_week=st["cap_week"],
        calibration_mode=st["calibration_mode"],
        focus=sa.FocusOut(**st["focus"]) if st["focus"] else None,
        night_window_active=st["night_window_active"],
        concurrency=st["concurrency"],
        subscription_bars=(
            sa.SubscriptionBarsOut(
                observed_at=bars["observed_at"],
                status=bars["status"],
                five_hour=bar(bars["five_hour"]),
                seven_day=bar(bars["seven_day"]),
            )
            if bars
            else None
        ),
        by_task=[sa.TaskUsage(task=k, **v) for k, v in sorted(st["by_task"].items())],
        queue=sa.LlmQueue(
            deferred=st["queue"]["llm_deferred"], deferred_total=st["queue"]["llm_deferred_total"]
        ),
    )


@router.get("/llm/status", response_model=sa.LlmStatusOut)
def llm_status(conn: Conn) -> sa.LlmStatusOut:
    return _llm_status(conn)


@router.post("/llm/governor/pause", response_model=sa.LlmStatusOut)
def llm_governor_pause(body: sa.LlmPauseIn | None = None) -> sa.LlmStatusOut:
    """Pause all Claude calls for ``minutes`` (default: until resumed)."""
    body = body or sa.LlmPauseIn()
    until = now_utc() + timedelta(minutes=body.minutes) if body.minutes else None
    with db.transaction() as conn:
        governor.pause(conn, until, reason=body.reason)
        return _llm_status(conn)


@router.post("/llm/governor/resume", response_model=sa.LlmStatusOut)
def llm_governor_resume() -> sa.LlmStatusOut:
    with db.transaction() as conn:
        governor.resume(conn)
        return _llm_status(conn)


# ------------------------------------------------------------------------------------------ alert rules


def _rule_out(row: dict[str, Any]) -> sa.AlertRuleOut:
    return sa.AlertRuleOut(
        id=row["id"],
        name=row["name"],
        filter=row["filter"],
        channels=[c for c in row["channels"] or [] if c in CHANNELS],
        quiet_hours=row["quiet_hours"] or None,
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


def _rule_row(conn: Connection, rule_id: str) -> dict[str, Any]:
    row = conn.execute(select(alert_rule).where(alert_rule.c.id == rule_id)).mappings().first()
    if row is None:
        _fail(404, "Kural bulunamadı.")
    return dict(row)


def _check_filter(text: str) -> None:
    try:
        parse(text)
    except RuleSyntaxError as exc:
        _fail(422, f"Filtre hatalı: {exc}")


@router.get("/alerts/rules", response_model=sa.AlertRulesOut)
def get_alert_rules(conn: Conn) -> sa.AlertRulesOut:
    return sa.AlertRulesOut(
        rules=[_rule_out(r) for r in list_rules(conn)],
        fields=sorted(FIELDS),
        telegram_ready=outbox.is_ready(),
    )


@router.post("/alerts/rules/validate", response_model=sa.RuleValidateOut)
def validate_alert_rule(body: sa.RuleValidateIn) -> sa.RuleValidateOut:
    try:
        parse(body.filter)
    except RuleSyntaxError as exc:
        return sa.RuleValidateOut(ok=False, error=str(exc))
    return sa.RuleValidateOut(ok=True, error=None)


@router.post("/alerts/rules", response_model=sa.AlertRuleOut, responses=_ERR)
def create_alert_rule(body: sa.AlertRuleIn) -> sa.AlertRuleOut:
    try:
        with db.transaction() as conn:
            rid = add_rule(
                conn,
                body.name.strip(),
                body.filter.strip(),
                channels=list(body.channels),
                quiet_hours=body.quiet_hours.model_dump() if body.quiet_hours else None,
                enabled=body.enabled,
            )
            return _rule_out(_rule_row(conn, rid))
    except RuleSyntaxError as exc:
        _fail(422, f"Filtre hatalı: {exc}")
    except ValueError as exc:
        _fail(409 if "zaten" in str(exc) else 422, str(exc))


@router.patch("/alerts/rules/{rule_id}", response_model=sa.AlertRuleOut, responses=_ERR)
def update_alert_rule(rule_id: str, body: sa.AlertRulePatch) -> sa.AlertRuleOut:
    sent = body.model_fields_set
    values: dict[str, Any] = {}
    if "name" in sent and body.name is not None:
        values["name"] = body.name.strip()
    if "filter" in sent and body.filter is not None:
        _check_filter(body.filter.strip())
        values["filter"] = body.filter.strip()
    if "channels" in sent and body.channels is not None:
        values["channels"] = list(body.channels)
    if "quiet_hours" in sent:
        values["quiet_hours"] = body.quiet_hours.model_dump() if body.quiet_hours else None
    if "enabled" in sent and body.enabled is not None:
        values["enabled"] = body.enabled
    with db.transaction() as conn:
        _rule_row(conn, rule_id)
        if (
            "name" in values
            and conn.execute(
                select(alert_rule.c.id).where(alert_rule.c.name == values["name"], alert_rule.c.id != rule_id)
            ).first()
        ):
            _fail(409, f"bu adla bir kural zaten var: {values['name']}")
        if values:
            conn.execute(
                update(alert_rule).where(alert_rule.c.id == rule_id).values(**values, updated_at=now_utc())
            )
        return _rule_out(_rule_row(conn, rule_id))


@router.delete("/alerts/rules/{rule_id}", response_model=sa.RuleDeletedOut, responses=_ERR)
def delete_alert_rule(rule_id: str) -> sa.RuleDeletedOut:
    """Delete a rule; notifications it already sent stay (their ``alert_rule_id`` is cleared)."""
    with db.transaction() as conn:
        _rule_row(conn, rule_id)
        conn.execute(
            update(notification).where(notification.c.alert_rule_id == rule_id).values(alert_rule_id=None)
        )
        conn.execute(alert_rule.delete().where(alert_rule.c.id == rule_id))
    return sa.RuleDeletedOut(id=rule_id)


@router.post("/alerts/test", response_model=sa.AlertTestOut)
def send_test_notification(body: sa.AlertTestIn | None = None) -> sa.AlertTestOut:
    """Send a test notification on one channel; failures are reported in the body (``ok: false``)."""
    channel = (body or sa.AlertTestIn()).channel
    if channel == "macos":
        ok = macos.notify("papi", "Test bildirimi")
        return sa.AlertTestOut(
            ok=ok, channel=channel, detail=None if ok else "macOS bildirimleri kapalı ya da kullanılamıyor"
        )
    try:
        with db.transaction() as conn:
            sent = outbox.send(conn, "🔔 papi test mesajı (arayüzden)")
    except (outbox.TelegramUnavailable, TelegramError) as exc:
        return sa.AlertTestOut(ok=False, channel=channel, detail=str(exc))
    return sa.AlertTestOut(ok=True, channel=channel, detail=f"mesaj #{sent.message_id}")
