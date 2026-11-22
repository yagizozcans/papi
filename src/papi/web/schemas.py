"""Pydantic response/request models of the UI API (``api_ui``; 14 §6 phase P1).

The React frontend generates its TypeScript types from these (``papi web openapi``), so every field is
precisely typed. Conventions:

- Money, quantities, prices and percentages are ``float`` (display grade, converted from the exact
  ``Decimal`` the ledger keeps); percentages are in percent (``2.5`` = 2.5 %), weights are fractions
  (``0.25`` = 25 %).
- Timestamps are timezone-aware UTC ``datetime`` (ISO-8601 with ``Z``); session dates are ``date``.
- ``Any`` appears only for genuinely free-form JSON (an event's parsed ``payload``, analysis ``output``,
  forecast distributions, pending-action proposals).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------------------ common


class ErrorOut(BaseModel):
    """Body of every 4xx/404 of this API (FastAPI's ``HTTPException`` shape)."""

    detail: str


Badge = Literal["HOLDING", "WATCH"]
InstrumentKind = Literal["stock", "fund", "index", "other"]


class FeedTicker(BaseModel):
    instrument_id: str | None
    ticker: str
    badge: Badge | None


class IssuerRef(BaseModel):
    id: str
    name: str


class FeedItem(BaseModel):
    """One classified disclosure (event) as shown in the tape; the raw ``payload`` is only in the detail."""

    id: str
    code: str
    code_label: str
    family: str
    materiality: int | None
    materiality_band: str | None
    priority: str | None  # P1 (most urgent) .. P4
    status: str
    stage: Literal["rules", "analysed"]
    announced_at: datetime
    published_key: str  # fixed-width UTC string of the publish time; sorts lexically; "" when unknown
    published_at: datetime | None
    first_seen_at: datetime | None
    latency_s: float | None
    catchup: bool
    is_late: bool
    revision_kind: str | None
    revision_label: str | None
    headline: str
    headline_en: str | None
    tickers: list[FeedTicker]
    holding: bool
    watch: bool
    issuers: list[IssuerRef]
    disclosure_id: str | None
    source_ref: str | None
    kap_url: str | None


# ------------------------------------------------------------------------------------------ feed


class FamilyOut(BaseModel):
    code: str
    label: str


class FeedFacets(BaseModel):
    families: list[FamilyOut]


class FeedOut(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None  # pass as ``before`` for the next (older) page; null = no more
    facets: FeedFacets


# ------------------------------------------------------------------------------------------ event detail


class EventMeta(BaseModel):
    id: str
    code: str
    taxonomy_version: str | None
    status: str
    superseded_by: str | None
    payload: dict[str, Any] | None  # free-form parsed fields of the disclosure (shape depends on code)
    payload_source: str | None
    payload_schema: str | None
    rules: dict[str, Any] | None  # free-form triage reasoning
    created_at: datetime


class DisclosureRef(BaseModel):
    id: str
    source_ref: str | None
    kap_url: str | None
    title: str | None
    subject: str | None
    published_at: datetime | None
    first_seen_at: datetime | None
    revision_kind: str | None
    is_late: bool


class RevisionLink(BaseModel):
    id: str
    source_ref: str | None
    kap_url: str | None
    revision_kind: str | None
    revision_label: str
    published_at: datetime | None
    title: str | None
    event_id: str | None
    current: bool


class SupersededEvent(BaseModel):
    id: str
    code: str
    created_at: datetime


class SummaryOut(BaseModel):
    headline: str | None
    body: str | None
    created_at: datetime


class SummariesOut(BaseModel):
    tr: SummaryOut | None
    en: SummaryOut | None


class CitationOut(BaseModel):
    target_type: str
    target_id: str
    quote: str | None
    anchor: str | None  # id of the highlighted span in the document ``html`` (``d<doc>s<ordinal>``)


class ClaimOut(BaseModel):
    id: str
    kind: str
    text_tr: str | None
    text_en: str | None
    value: str | None
    unit: str | None
    check_status: str | None
    verify_status: str | None
    citations: list[CitationOut]


class AnalysisOut(BaseModel):
    id: str
    agent: str
    backend: str
    model_ids: list[str]
    prompt_version: str | None
    created_at: datetime
    review_state: str
    confidence: float | None
    latency_ms: int | None
    cost_usd_est: float | None
    output: Any  # free-form structured analyst output
    claims: list[ClaimOut]


class KeyFact(BaseModel):
    path: str
    value: Any  # scalar or list from the payload
    anchors: list[str]
    provenance: str | None  # payload_spans | form_field | claim


class SpanOut(BaseModel):
    id: str
    ordinal: int
    anchor: str
    char_start: int | None
    char_end: int | None
    page: int | None
    field_path: str | None
    text: str | None


class DocumentOut(BaseModel):
    index: int
    id: str
    kind: str
    mime: str | None
    filename: str | None
    byte_size: int | None
    raw_sha256: str | None
    raw_url: str | None  # original source URL
    download_url: str | None  # ``/raw/<sha>`` on this server
    template_id: str | None
    text_length: int
    html: str  # server-rendered, already HTML-escaped text with every placed span wrapped in an anchor
    spans: list[SpanOut]
    unplaced_span_ids: list[str]  # spans that have no usable offsets (not wrapped in ``html``)


class ForecastOutcome(BaseModel):
    realised_value: float | None
    scores: Any


class ForecastOut(BaseModel):
    id: str
    ticker: str
    horizon: str
    target: str
    model: str
    distribution: Any
    scenarios: Any
    outcome: ForecastOutcome | None


class ForMePosition(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float | None
    last_close: float | None
    last_close_date: date | None
    market_value: float | None
    weight: float | None


class ForMeImpact(BaseModel):
    forecast_id: str
    instrument_id: str
    ticker: str
    horizon: str
    target: str
    model: str
    distribution: Any
    position_impact: Any


class ForMeOut(BaseModel):
    holding: bool
    positions: list[ForMePosition]
    impacts: list[ForMeImpact]
    reason: str | None


class EventDetailOut(BaseModel):
    item: FeedItem
    event: EventMeta
    disclosures: list[DisclosureRef]
    revision_chain: list[RevisionLink]
    superseded: list[SupersededEvent]
    summaries: SummariesOut
    analyses: list[AnalysisOut]
    key_facts: list[KeyFact]
    documents: list[DocumentOut]
    forecasts: list[ForecastOut]
    for_me: ForMeOut
    feedback_counts: dict[str, int]


FeedbackKind = Literal["wrong_fact", "wrong_type", "not_material", "helpful"]
FeedbackTarget = Literal["event", "analysis", "claim", "disclosure", "forecast"]


class FeedbackIn(BaseModel):
    kind: FeedbackKind
    comment: str | None = None
    target_type: FeedbackTarget = "event"
    target_id: str | None = None  # default: the event itself


class FeedbackOut(BaseModel):
    event_id: str
    feedback_counts: dict[str, int]


# ------------------------------------------------------------------------------------------ portfolio


class PositionOut(BaseModel):
    instrument_id: str
    ticker: str
    name: str | None
    kind: InstrumentKind
    quantity: float
    avg_cost: float | None
    cost_total: float
    last_close: float | None
    last_close_date: date | None
    prev_close: float | None
    prev_close_date: date | None
    market_value: float | None
    weight: float | None  # share of the priced portfolio value (0..1)
    pnl: float | None  # unrealised, market value - cost
    pnl_pct: float | None
    day_change: float | None  # quantity x (last close - previous close), TRY
    day_change_pct: float | None  # last close vs previous close
    badge: Badge | None


class PortfolioTotals(BaseModel):
    value: float | None
    cost: float | None
    pnl: float | None
    pnl_pct: float | None
    day_change: float | None
    day_change_pct: float | None  # day change vs the previous-close value of the positions that have one
    positions: int
    unvalued: list[str]  # tickers without any close (excluded from value/cost)


class AllocationSlice(BaseModel):
    key: str
    label: str
    value: float
    weight: float  # 0..1 of the priced value


class AllocationOut(BaseModel):
    by_instrument: list[AllocationSlice]
    by_kind: list[AllocationSlice]
    by_sector: list[AllocationSlice]


class TxnOut(BaseModel):
    id: str
    instrument_id: str | None
    ticker: str | None
    type: str
    trade_date: date
    quantity: float
    price: float | None
    fees: float
    tax: float
    currency: str
    source: str
    note: str | None
    reverses_id: str | None
    reversed: bool


class PendingActionOut(BaseModel):
    id: str
    instrument_id: str
    ticker: str | None
    kind: str
    status: str
    ex_date: date | None
    proposal: dict[str, Any] | None  # free-form computed quantities/costs
    corporate_action_id: str | None
    created_at: datetime


class WatchMember(BaseModel):
    instrument_id: str
    ticker: str
    name: str | None
    badge: Badge | None


class WatchlistOut(BaseModel):
    id: str
    name: str
    members: list[WatchMember]
    sectors: list[str]
    indices: list[str]


class PortfolioOverviewOut(BaseModel):
    available: bool  # false when the portfolio module is not installed
    positions: list[PositionOut]
    totals: PortfolioTotals
    allocation: AllocationOut
    transactions: list[TxnOut]
    pending_actions: list[PendingActionOut]
    watchlists: list[WatchlistOut]


PerformanceRange = Literal["1m", "3m", "6m", "1y", "ytd", "max"]


class PerformancePoint(BaseModel):
    date: date
    value: float  # sum of quantity x close over the positions held at the end of that day
    cost: float  # cost basis of those positions
    pnl: float
    return_index: float  # price-return index, 100 at the first point (flows excluded)


class BenchmarkPoint(BaseModel):
    date: date
    value: float  # XU100 close rebased to 100 at the first portfolio point


class PerformanceOut(BaseModel):
    range: PerformanceRange
    method: Literal["transactions_x_closes"]
    start: date | None
    end: date | None
    points: list[PerformancePoint]
    benchmark: list[BenchmarkPoint]
    unpriced: list[str]  # held tickers that had no close for part of the range (valued 0 there)


# ------------------------------------------------------------------------------------------ instrument


class InstrumentInfo(BaseModel):
    id: str
    type: str
    currency: str
    status: str


class IssuerInfo(BaseModel):
    id: str
    legal_name: str
    short_name: str | None
    sector_code: str | None
    status: str
    kind: str | None


class IdentifierOut(BaseModel):
    entity_type: str
    scheme: str
    value: str
    valid_from: date | None
    valid_to: date | None


class ShareCapitalOut(BaseModel):
    """From KAP ``sermaye`` pages; every field is null until ``papi kap capital`` has fetched the issuer."""

    paid_in_capital: float | None
    registered_capital_ceiling: float | None
    paid_in_capital_as_of: str | None
    shares_outstanding: float | None
    shares_as_of: str | None
    free_float_shares: float | None
    free_float_pct: float | None
    market_cap: float | None  # shares outstanding x last close


class LastClose(BaseModel):
    date: date
    close: float
    prev_close: float | None
    day_change: float | None
    day_change_pct: float | None


class ThesisRef(BaseModel):
    name: str
    date: date | None
    direction: str | None
    horizon: str | None
    runs: int
    last_run_status: str | None
    last_verdict: str | None


class AgentRunBrief(BaseModel):
    run_id: str
    agent: str
    subject: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    cost_usd: float | None
    turns: int | None
    error: str | None


class InstrumentOut(BaseModel):
    ticker: str
    name: str | None
    kind: InstrumentKind
    instrument: InstrumentInfo
    issuer: IssuerInfo | None
    badge: Badge | None
    watch: bool
    watchlists: list[str]
    indices: list[str]
    identifiers: list[IdentifierOut]
    share_capital: ShareCapitalOut
    last_close: LastClose | None
    position: PositionOut | None
    disclosures: list[FeedItem]
    theses: list[ThesisRef]
    agent_runs: list[AgentRunBrief]


class PriceBarOut(BaseModel):
    """Daily bar; ``open/high/low/volume`` are null where the source only provides a close (e.g. funds)."""

    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    source: str


class DisclosureMarker(BaseModel):
    date: date  # Istanbul date of publication
    event_id: str
    code_label: str
    priority: str | None
    materiality: int | None
    headline: str


class TxnMarker(BaseModel):
    date: date
    side: Literal["buy", "sell"]
    type: str
    quantity: float
    price: float | None


class PricesOut(BaseModel):
    ticker: str
    from_: date = Field(serialization_alias="from")
    to: date
    primary_source: str  # configured preferred source; a bar's own ``source`` says what was actually used
    bars: list[PriceBarOut]
    markers_disclosures: list[DisclosureMarker]
    markers_txns: list[TxnMarker]


# ------------------------------------------------------------------------------------------ today


class MarketQuote(BaseModel):
    code: str
    label: str
    last: float | None
    prev: float | None
    change: float | None
    change_pct: float | None
    as_of: date | None


class TodayPortfolio(BaseModel):
    available: bool
    value: float | None
    cost: float | None
    pnl: float | None
    day_change: float | None
    day_change_pct: float | None
    positions: int


class AttentionOut(BaseModel):
    """Recent P1/P2 events on holdings/watchlist. papi keeps no read/ack state, so the client tracks
    seen-ness (by ``id`` / ``published_key``)."""

    window_hours: int
    items: list[FeedItem]


class PendingSummary(BaseModel):
    count: int
    items: list[PendingActionOut]


class LaneJobs(BaseModel):
    lane: str
    dead: int
    queued: int
    running: int
    last_error: str | None
    last_error_at: datetime | None
    last_error_kind: str | None


class JobsSummary(BaseModel):
    dead_total: int
    lanes: list[LaneJobs]
    analyses_waiting: int  # queued jobs on the ``llm`` lane


class TodayOut(BaseModel):
    generated_at: datetime
    xu100: MarketQuote | None
    usdtry: MarketQuote | None
    portfolio: TodayPortfolio
    attention: AttentionOut
    pending_actions: PendingSummary
    jobs: JobsSummary
    feed: list[FeedItem]
    agent_runs: list[AgentRunBrief]


# ------------------------------------------------------------------------------------------ search


class InstrumentHit(BaseModel):
    instrument_id: str
    ticker: str
    name: str | None
    kind: InstrumentKind
    badge: Badge | None
    score: int


class DisclosureHit(BaseModel):
    disclosure_id: str
    event_id: str | None
    headline: str
    snippet: str
    source_ref: str | None
    kap_url: str | None
    published_at: datetime | None
    priority: str | None
    tickers: list[str]


class SearchOut(BaseModel):
    q: str
    instruments: list[InstrumentHit]
    disclosures: list[DisclosureHit]
