"""Request/response models of the P2 "act" endpoints (``api_actions``; 14 §6 phase P2, slice A):
manual trades, pending corporate actions, watchlists, LLM governor controls and alert rules.

Same conventions as :mod:`papi.web.schemas` (floats for money/quantities, UTC datetimes, ``ErrorOut`` for
4xx bodies). Quantities and prices travel as JSON numbers and are converted to ``Decimal`` server-side.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from papi.portfolio.service import TxnType
from papi.web.schemas import TxnOut

# ------------------------------------------------------------------------------------------ trades


class TxnIn(BaseModel):
    """A manual ledger entry. ``dry_run`` previews the resulting position without writing anything."""

    type: TxnType
    ticker: str | None = Field(default=None, max_length=40)  # not needed for fee / tax rows
    quantity: float
    price: float | None = None
    fees: float = 0.0
    trade_date: date | None = None  # default: today (Istanbul)
    note: str | None = Field(default=None, max_length=500)
    dry_run: bool = False


class PositionSnap(BaseModel):
    quantity: float
    avg_cost: float | None
    cost_total: float
    market_value: float | None
    weight: float | None  # fraction of the priced portfolio value


class TxnResultOut(BaseModel):
    committed: bool  # false for a dry run
    txn_id: str | None
    type: TxnType
    ticker: str | None
    instrument_id: str | None
    name: str | None
    quantity: float
    price: float | None
    fees: float
    trade_date: date
    gross: float | None  # quantity x price
    warnings: list[str]  # Turkish; shown in the preview, the commit is the user's acknowledgement
    before: PositionSnap | None  # None for cash-only rows (fee / tax)
    after: PositionSnap | None


class UndoIn(BaseModel):
    txn_id: str | None = None  # None: the most recently entered transaction that is not yet undone
    reason: str | None = Field(default=None, max_length=200)


class UndoOut(BaseModel):
    undone: TxnOut
    reversal_id: str


# ------------------------------------------------------------------------------------------ pending actions


class PendingApplyIn(BaseModel):
    quantity_override: float | None = None  # shares received / subscribed / eligible; None = proposal


class PendingApplyOut(BaseModel):
    pending_id: str
    txn_id: str


class PendingDismissOut(BaseModel):
    pending_id: str


# ------------------------------------------------------------------------------------------ watchlist


class WatchIn(BaseModel):
    ref: str = Field(min_length=1, max_length=80)  # THYAO, XU030, endeks:XU030, sektor:<kod>
    watchlist: str = Field(default="Ana", min_length=1, max_length=80)


class WatchlistSummary(BaseModel):
    id: str
    name: str
    tickers: list[str]
    sectors: list[str]
    indices: list[str]


class WatchOut(BaseModel):
    ref: str
    changed: bool  # false when it was already on the list
    watchlist: WatchlistSummary


class UnwatchOut(BaseModel):
    ref: str
    removed: bool  # false when it was not on the list
    watchlist: WatchlistSummary | None


# ------------------------------------------------------------------------------------------ LLM


class FocusOut(BaseModel):
    max_priority: str
    until: datetime


class BarOut(BaseModel):
    utilization: float | None  # fraction of the claude.ai bar (1.0 = 100 %), all Claude use
    resets_at: datetime | None


class SubscriptionBarsOut(BaseModel):
    observed_at: datetime
    status: str | None
    five_hour: BarOut | None
    seven_day: BarOut | None


class TaskUsage(BaseModel):
    task: str
    calls: int
    cost: float
    avg_cost: float
    failures: int


class LlmQueue(BaseModel):
    deferred: dict[str, int]  # by priority label
    deferred_total: int


class LlmStatusOut(BaseModel):
    """Governor state (04 §6). Usage and caps are in cost-equivalent units (``total_cost_usd`` estimates)."""

    paused: bool
    paused_until: datetime | None  # None while paused = until resumed
    pause_reason: str | None
    used_5h: float
    cap_5h: float
    used_chat_5h: float
    cap_chat_5h: float
    used_week: float
    cap_week: float
    calibration_mode: bool
    focus: FocusOut | None
    night_window_active: bool
    concurrency: int
    subscription_bars: SubscriptionBarsOut | None
    by_task: list[TaskUsage]
    queue: LlmQueue


class LlmPauseIn(BaseModel):
    minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 14)  # None = until resumed
    reason: str = Field(default="manual", max_length=60)


# ------------------------------------------------------------------------------------------ alert rules

AlertChannel = Literal["telegram", "macos"]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class QuietHours(BaseModel):
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=HHMM)


class AlertRuleOut(BaseModel):
    id: str
    name: str
    filter: str
    channels: list[AlertChannel]
    quiet_hours: QuietHours | None  # None = the global quiet hours of ``config.toml``
    enabled: bool
    created_at: datetime
    updated_at: datetime | None


class AlertRulesOut(BaseModel):
    rules: list[AlertRuleOut]
    fields: list[str]  # fields of the filter language
    telegram_ready: bool


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filter: str = Field(min_length=1, max_length=2000)
    channels: list[AlertChannel] = Field(default_factory=lambda: ["telegram", "macos"], min_length=1)
    quiet_hours: QuietHours | None = None
    enabled: bool = True


class AlertRulePatch(BaseModel):
    """Partial update: only the fields present in the body change (``quiet_hours: null`` clears them)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    filter: str | None = Field(default=None, min_length=1, max_length=2000)
    channels: list[AlertChannel] | None = Field(default=None, min_length=1)
    quiet_hours: QuietHours | None = None
    enabled: bool | None = None


class RuleValidateIn(BaseModel):
    filter: str


class RuleValidateOut(BaseModel):
    ok: bool
    error: str | None  # Turkish syntax message


class AlertTestIn(BaseModel):
    channel: AlertChannel = "telegram"


class AlertTestOut(BaseModel):
    ok: bool
    channel: AlertChannel
    detail: str | None


class RuleDeletedOut(BaseModel):
    id: str
