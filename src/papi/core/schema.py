"""SQLAlchemy Core metadata for every entity in docs/prd/06-data-model.md §3–§8 plus infra tables.

Conventions
- ``id`` columns are UUIDv7 strings (``papi.core.ids.uuid7``); there are no DB defaults for them.
- Timestamps: ``UtcDateTime`` (UTC ISO text). Calendar dates: ``Date`` (``YYYY-MM-DD``).
- Arrays / objects: JSON columns (``Json()``; Python ``None`` → SQL NULL).
- Portfolio money/quantities: ``DecimalText`` (exact). Market data: ``Float``.
- Enumerations are plain strings; allowed values are documented per column (no CHECK constraints so
  enums can grow without a table rebuild).
- The PRD's ``transaction`` entity is the table ``txn`` (``transaction`` is an SQL keyword).
- ``identifier.valid_from`` / ``listing.valid_from`` / ``index_membership.valid_from`` are NOT NULL;
  use ``OPEN_START`` (1900-01-01) for "since forever" so the uniqueness constraints hold.
- FTS5 tables ``disclosure_fts`` and ``provision_fts`` exist only in migrations (see ``papi.core.fts``).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from papi.core.types import DecimalText, Json, UtcDateTime

OPEN_START = date(1900, 1, 1)

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING)


def _id() -> Column:
    return Column("id", String(36), primary_key=True)


def _fk(name: str, target: str, *, nullable: bool = True, index: bool = True) -> Column:
    return Column(name, String(36), ForeignKey(target), nullable=nullable, index=index)


def _created() -> Column:
    return Column("created_at", UtcDateTime(), nullable=False)


def _updated() -> Column:
    return Column("updated_at", UtcDateTime(), nullable=True)


def _extra() -> Column:
    return Column("extra", Json(), nullable=True)


# --------------------------------------------------------------------------------------------- §3 reference

jurisdiction = Table(
    "jurisdiction",
    metadata,
    _id(),
    Column("code", String(16), nullable=False, unique=True),  # TR
    Column("name", Text, nullable=False),
    Column("default_tz", String(64), nullable=False),
    Column("default_currency", String(3), nullable=False),
    Column("languages", Json(), nullable=False),  # ["tr","en"]
    Column("pack_version", String(32), nullable=True),
    _created(),
)

market = Table(
    "market",
    metadata,
    _id(),
    _fk("jurisdiction_id", "jurisdiction.id", nullable=False),
    Column("code", String(32), nullable=False, unique=True),  # BIST_EQ
    Column("name", Text, nullable=False),
    Column("asset_class", String(32), nullable=False),  # equity, fixed_income, derivative, fund
    _created(),
)

venue = Table(
    "venue",
    metadata,
    _id(),
    _fk("market_id", "market.id", nullable=False),
    Column("mic", String(8), nullable=False, unique=True),  # XIST
    Column("name", Text, nullable=False),
    Column("calendar_id", String(32), nullable=True),  # trading_calendar.calendar_id, e.g. XIST
    _created(),
)

trading_calendar = Table(
    "trading_calendar",
    metadata,
    _id(),
    Column("calendar_id", String(32), nullable=False),  # e.g. XIST (venue.calendar_id)
    Column("date", Date, nullable=False),
    Column("session_type", String(16), nullable=False),  # regular | half_day | closed
    Column("open_utc", UtcDateTime(), nullable=True),
    Column("close_utc", UtcDateTime(), nullable=True),
    Column("is_half_day", Boolean, nullable=False, server_default=text("0")),
    Column("note", Text, nullable=True),  # holiday name etc.
    UniqueConstraint("calendar_id", "date", "session_type"),
)

issuer = Table(
    "issuer",
    metadata,
    _id(),
    Column("legal_name", Text, nullable=False),
    Column("short_name", Text, nullable=True),
    Column("country", String(2), nullable=True),
    Column("sector_code", String(64), nullable=True, index=True),
    Column("status", String(16), nullable=False, server_default="active"),  # active | delisted | inactive
    Column("kind", String(32), nullable=True),  # company | fund | bank | intermediary | regulator | exchange
    _extra(),
    _created(),
    _updated(),
)

instrument = Table(
    "instrument",
    metadata,
    _id(),
    _fk("issuer_id", "issuer.id"),
    Column("type", String(16), nullable=False),  # equity | warrant | bond | fund | etf | ...
    Column("currency", String(3), nullable=False, server_default="TRY"),
    Column("status", String(16), nullable=False, server_default="active"),
    _extra(),
    _created(),
    _updated(),
)

listing = Table(
    "listing",
    metadata,
    _id(),
    _fk("instrument_id", "instrument.id", nullable=False),
    _fk("venue_id", "venue.id", nullable=False),
    Column("segment", String(64), nullable=True),  # pack-defined (Yıldız Pazar, Ana Pazar, ...)
    Column("valid_from", Date, nullable=False),
    Column("valid_to", Date, nullable=True),
    UniqueConstraint("instrument_id", "venue_id", "valid_from"),
)

identifier = Table(
    "identifier",
    metadata,
    _id(),
    Column("entity_type", String(16), nullable=False),  # issuer | instrument
    Column("entity_id", String(36), nullable=False),
    # TICKER | ISIN | KAP_MEMBER_OID | KAP_COMPANY_OID | LEI | MERSIS | VKN
    Column("scheme", String(32), nullable=False),
    Column("value", Text, nullable=False),
    Column("valid_from", Date, nullable=False),  # OPEN_START if unknown
    Column("valid_to", Date, nullable=True),  # NULL = still valid
    Column("recorded_at", UtcDateTime(), nullable=False),
    Column("superseded_at", UtcDateTime(), nullable=True),
    UniqueConstraint("scheme", "value", "valid_from"),
    Index(None, "entity_type", "entity_id"),
    Index(None, "scheme", "value"),
)

index_membership = Table(
    "index_membership",
    metadata,
    _id(),
    Column("index_code", String(16), nullable=False, index=True),  # XU100, XU030, ...
    _fk("instrument_id", "instrument.id", nullable=False),
    Column("weight", Float, nullable=True),
    Column("valid_from", Date, nullable=False),
    Column("valid_to", Date, nullable=True),
    UniqueConstraint("index_code", "instrument_id", "valid_from"),
)

relationship = Table(
    "relationship",
    metadata,
    _id(),
    _fk("from_issuer_id", "issuer.id", nullable=False),
    _fk("to_issuer_id", "issuer.id", nullable=False),
    Column("type", String(16), nullable=False),  # subsidiary|parent|supplier|customer|competitor|jv
    _fk("source_claim_id", "claim.id"),
    Column("valid_from", Date, nullable=True),
    Column("valid_to", Date, nullable=True),
    Column("recorded_at", UtcDateTime(), nullable=False),
)

# --------------------------------------------------------------------------------------------- §4 capture

source = Table(
    "source",
    metadata,
    _id(),
    # kap | legal_corpus | isyatirim_prices | yahoo_prices | tefas_prices | spk_bulletin | bist_notices |
    # resmi_gazete | tcmb | tuik
    Column("code", String(32), nullable=False, unique=True),
    _fk("jurisdiction_id", "jurisdiction.id"),
    Column("access_mode", String(16), nullable=False),  # public_web | licensed_api | file_drop
    Column("rate_limit", Text, nullable=True),
    Column("terms_note", Text, nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),  # active | paused | disabled
    _created(),
)

raw_object = Table(
    "raw_object",
    metadata,
    Column("sha256", String(64), primary_key=True),
    _fk("source_id", "source.id", nullable=False),
    Column("url", Text, nullable=True),
    Column("http_status", Integer, nullable=True),
    Column("request_meta", Json(), nullable=True),
    Column("response_headers", Json(), nullable=True),
    Column("fetched_at", UtcDateTime(), nullable=False),
    Column("content_type", Text, nullable=True),
    Column("byte_size", Integer, nullable=False),
    Column("storage_uri", Text, nullable=False),  # raw/{sha[0:2]}/{sha} relative to $PAPI_HOME
)

fetch_log = Table(
    "fetch_log",
    metadata,
    _id(),
    _fk("source_id", "source.id", nullable=False),
    _fk("raw_sha256", "raw_object.sha256"),  # NULL when the fetch failed
    Column("url", Text, nullable=True),
    Column("http_status", Integer, nullable=True),
    Column("fetcher_version", String(64), nullable=True),
    Column("started_at", UtcDateTime(), nullable=False),
    Column("finished_at", UtcDateTime(), nullable=True),
    Column("outcome", String(32), nullable=False),  # ok | http_error | timeout | blocked | error
    Column("error", Text, nullable=True),
    Index(None, "started_at"),
)

disclosure = Table(
    "disclosure",
    metadata,
    _id(),
    _fk("source_id", "source.id", nullable=False, index=False),
    Column("source_ref", String(64), nullable=False),  # KAP disclosureIndex (as text)
    Column("source_uid", String(128), nullable=True, index=True),  # KAP disclosureId (hex)
    _fk("revision_of", "disclosure.id"),
    Column(
        "revision_kind", String(16), nullable=False, server_default="original"
    ),  # original|correction|update|blocked
    Column("published_at", UtcDateTime(), nullable=True, index=True),
    Column("published_at_raw", Text, nullable=True),
    Column("first_seen_at", UtcDateTime(), nullable=False),
    Column("source_class", String(32), nullable=True),
    Column("source_type", String(32), nullable=True),
    Column("source_category", String(32), nullable=True),
    Column("source_subject", Text, nullable=True),
    Column("title", Text, nullable=True),
    Column("summary", Text, nullable=True),
    _fk("filer_issuer_id", "issuer.id"),
    Column("related_issuer_ids", Json(), nullable=True),  # [issuer.id]
    Column("is_late", Boolean, nullable=False, server_default=text("0")),
    Column("languages", Json(), nullable=True),  # ["tr","en"]
    Column("body_document_id", String(36), ForeignKey("document.id", use_alter=True), nullable=True),
    Column("attachment_document_ids", Json(), nullable=True),  # [document.id]
    Column("source_payload", Json(), nullable=True),  # verbatim list + detail JSON
    # seen | fetched | parsed | normalized | failed:<stage>
    Column("ingest_status", String(32), nullable=False, server_default="seen", index=True),
    Column("ingest_error", Text, nullable=True),
    _extra(),
    _created(),
    _updated(),
    UniqueConstraint("source_id", "source_ref"),
)

document = Table(
    "document",
    metadata,
    _id(),
    _fk("raw_sha256", "raw_object.sha256"),
    _fk("disclosure_id", "disclosure.id"),  # owning disclosure (body/attachment), if any
    Column("kind", String(32), nullable=False),  # disclosure_body | attachment | regulation | bulletin
    Column("mime", String(128), nullable=True),
    Column("language", String(8), nullable=True),  # tr | en
    Column("text", Text, nullable=True),
    Column("structure", Json(), nullable=True),  # tables, form fields
    Column("parser", String(64), nullable=True),
    Column("parser_version", String(32), nullable=True),
    Column("parsed_at", UtcDateTime(), nullable=False),
    _extra(),
)

document_span = Table(
    "document_span",
    metadata,
    _id(),
    _fk("document_id", "document.id", nullable=False),
    Column("ordinal", Integer, nullable=True),  # span marker number in context packs ([S12])
    Column("page", Integer, nullable=True),
    Column("char_start", Integer, nullable=True),
    Column("char_end", Integer, nullable=True),
    Column("bbox", Json(), nullable=True),  # [x0, y0, x1, y1]
    Column("field_path", Text, nullable=True),  # form field id
    Column("text", Text, nullable=True),  # span text copy for quick citation rendering
)

# ------------------------------------------------------------------------------------------ §5 understanding

event = Table(
    "event",
    metadata,
    _id(),
    Column("disclosure_ids", Json(), nullable=False),  # [disclosure.id]
    _fk("primary_disclosure_id", "disclosure.id"),  # first of disclosure_ids, for indexed lookups
    Column("code", String(96), nullable=False, index=True),
    Column("taxonomy_version", String(32), nullable=False),
    Column("issuer_ids", Json(), nullable=True),
    Column("instrument_ids", Json(), nullable=True),
    Column("occurred_at", UtcDateTime(), nullable=True),
    Column("announced_at", UtcDateTime(), nullable=True, index=True),
    Column("payload", Json(), nullable=True),
    Column("payload_source", String(16), nullable=True),  # form_fields | analyst
    Column("payload_schema", String(128), nullable=True),
    Column("materiality", Integer, nullable=True),  # 0..100
    Column("materiality_band", String(16), nullable=True),  # low | medium | high | critical
    Column("priority", String(2), nullable=True, index=True),  # P1..P4
    Column("status", String(16), nullable=False, server_default="provisional"),  # provisional|final|retracted
    _fk("superseded_by", "event.id"),
    _extra(),
    _created(),
    _updated(),
)

analysis = Table(
    "analysis",
    metadata,
    _id(),
    _fk("event_id", "event.id", nullable=False),
    Column("agent", String(16), nullable=False),  # analyst | explain | checker | backlog
    Column("agent_version", String(64), nullable=True),
    Column("backend", String(32), nullable=False),  # claude_code_cli
    Column("model_ids", Json(), nullable=True),
    Column("prompt_version", String(32), nullable=True),
    Column("schema_version", String(32), nullable=True),
    _fk("kb_snapshot", "kb_snapshot.id"),
    Column("input_hash", String(64), nullable=True, index=True),
    Column("output", Json(), nullable=True),
    Column("raw_output", Json(), nullable=True),
    Column("confidence", Float, nullable=True),
    Column("latency_ms", Integer, nullable=True),
    Column("cost_usd_est", Float, nullable=True),
    Column("priority", String(2), nullable=True),
    _created(),
    Column(
        "review_state", String(16), nullable=False, server_default="auto"
    ),  # auto|flagged|approved|rejected
)

llm_usage_ledger = Table(
    "llm_usage_ledger",
    metadata,
    _id(),
    _fk("analysis_id", "analysis.id"),
    Column("task", String(16), nullable=False),  # analyst | explain | checker | backlog | chat
    Column("priority", String(8), nullable=True),  # P1..P4 | chat
    Column("model_ids", Json(), nullable=True),
    Column("cost_usd_est", Float, nullable=False, server_default=text("0")),
    Column("duration_ms", Integer, nullable=True),
    Column("outcome", String(16), nullable=False),  # ok | schema_fail | limit_hit | error
    Column("error", Text, nullable=True),
    Column("at", UtcDateTime(), nullable=False, index=True),
)

claim = Table(
    "claim",
    metadata,
    _id(),
    _fk("analysis_id", "analysis.id", nullable=False),
    Column("text_tr", Text, nullable=True),
    Column("text_en", Text, nullable=True),
    Column("kind", String(16), nullable=False),  # fact | computation | inference
    Column("value", Text, nullable=True),  # verbatim number/text; parse with parse_tr_number
    Column("unit", String(32), nullable=True),
    Column("formula_id", String(64), nullable=True),  # required for kind=computation
    Column("check_status", String(16), nullable=True),  # supported | unsupported | partial (checker)
    Column("verify_status", String(16), nullable=True),  # pass | fail (deterministic verifier)
)

citation = Table(
    "citation",
    metadata,
    _id(),
    _fk("claim_id", "claim.id", nullable=False),
    Column(
        "target_type", String(24), nullable=False
    ),  # document_span|provision_version|price_series|forecast
    Column("target_id", String(64), nullable=False),
    Column("quote", Text, nullable=True),
    Index(None, "target_type", "target_id"),
)

summary = Table(
    "summary",
    metadata,
    _id(),
    _fk("event_id", "event.id", nullable=False, index=False),
    Column("lang", String(8), nullable=False),
    Column("headline", Text, nullable=True),
    Column("body", Text, nullable=True),
    _fk("analysis_id", "analysis.id"),
    _created(),
    UniqueConstraint("event_id", "lang"),
)

# ------------------------------------------------------------------------------------------ §6 market & sim

price_bar = Table(
    "price_bar",
    metadata,
    _fk("instrument_id", "instrument.id", nullable=False, index=False),
    Column("ts", Date, nullable=False),  # session date (Istanbul) for interval 1d
    Column("interval", String(8), nullable=False, server_default="1d"),
    _fk("source_id", "source.id", nullable=False, index=False),
    Column("open", Float, nullable=True),
    Column("high", Float, nullable=True),
    Column("low", Float, nullable=True),
    Column("close", Float, nullable=True),
    Column("volume", Float, nullable=True),
    Column("value_traded", Float, nullable=True),
    Column("limit_up", Float, nullable=True),
    Column("limit_down", Float, nullable=True),
    Column("cross_check", String(16), nullable=True),  # ok | mismatch | single_source
    Column("recorded_at", UtcDateTime(), nullable=False),
    Column("raw_sha256", String(64), ForeignKey("raw_object.sha256"), nullable=True),  # provenance
    PrimaryKeyConstraint("instrument_id", "interval", "ts", "source_id"),
)

corporate_action = Table(
    "corporate_action",
    metadata,
    _id(),
    _fk("instrument_id", "instrument.id", nullable=False),
    # bonus | rights | dividend_cash | dividend_stock | split | reverse_split | merger | ticker_change
    Column("type", String(32), nullable=False),
    Column("ex_date", Date, nullable=True, index=True),
    Column("factor", DecimalText(), nullable=True),  # e.g. bonus 100% → 1.0 (quantity × (1 + factor))
    Column("cash_amount", DecimalText(), nullable=True),  # per share, gross
    Column("currency", String(3), nullable=True),
    _fk("source_event_id", "event.id"),
    Column(
        "status", String(16), nullable=False, server_default="announced"
    ),  # announced|approved|executed|cancelled
    Column("details", Json(), nullable=True),  # dates (record/payment/rights period), withholding, ratios
    _created(),
    _updated(),
)

fx_rate = Table(
    "fx_rate",
    metadata,
    Column("series", String(32), nullable=False),  # e.g. USDTRY_TCMB_FOREX_BUYING
    Column("ts", Date, nullable=False),
    Column("value", Float, nullable=False),
    Column("vintage", String(32), nullable=False, server_default=""),  # release version; '' = only
    _fk("source_id", "source.id", index=False),
    Column("recorded_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("series", "ts", "vintage"),
)

macro_series = Table(
    "macro_series",
    metadata,
    Column("series", String(64), nullable=False),  # e.g. TR_CPI_INDEX
    Column("ts", Date, nullable=False),
    Column("value", Float, nullable=False),
    Column("vintage", String(32), nullable=False, server_default=""),
    _fk("source_id", "source.id", index=False),
    Column("recorded_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("series", "ts", "vintage"),
)

feature_snapshot = Table(
    "feature_snapshot",
    metadata,
    _id(),
    _fk("instrument_id", "instrument.id", nullable=False, index=False),
    Column("as_of", UtcDateTime(), nullable=False),  # knowledge time
    Column("feature_set_version", String(32), nullable=False),
    Column("features", Json(), nullable=False),
    UniqueConstraint("instrument_id", "as_of", "feature_set_version"),
)

forecast = Table(
    "forecast",
    metadata,
    _id(),
    _fk("event_id", "event.id", nullable=False),
    _fk("instrument_id", "instrument.id", nullable=False),
    Column("model", String(64), nullable=False),
    Column("model_version", String(32), nullable=False),
    _created(),
    Column("horizon", String(8), nullable=False),  # T+1 | T+5 | T+20
    Column("target", String(32), nullable=False),  # abnormal_return | volume_ratio | volatility_ratio
    Column("distribution", Json(), nullable=True),  # {"q05":..,"q50":..,"q95":..}
    Column("scenarios", Json(), nullable=True),
    Column("position_impact", Json(), nullable=True),  # holdings only
    Column("frozen_hash", String(64), nullable=False),  # immutable after creation
)

outcome = Table(
    "outcome",
    metadata,
    _id(),
    _fk("forecast_id", "forecast.id", nullable=False, index=False),
    Column("realised_value", Float, nullable=True),
    Column("realised_at", UtcDateTime(), nullable=True),
    Column("scores", Json(), nullable=True),  # pinball, crps, in_interval, direction_hit
    _created(),
    UniqueConstraint("forecast_id"),
)

# --------------------------------------------------------------------------------------------- §7 legal

kb_snapshot = Table(
    "kb_snapshot",
    metadata,
    _id(),
    Column("status", String(16), nullable=False, server_default="draft"),  # draft | published | superseded
    Column("content_hash", String(64), nullable=True),
    Column("note", Text, nullable=True),
    Column("stats", Json(), nullable=True),  # counts per regulation etc.
    _created(),
    Column("published_at", UtcDateTime(), nullable=True),
)

regulation = Table(
    "regulation",
    metadata,
    _id(),
    _fk("jurisdiction_id", "jurisdiction.id", nullable=False),
    # law | communique | guideline | directive | board_decision | exchange_rule
    Column("kind", String(32), nullable=False),
    Column("official_code", String(64), nullable=False),  # 6362, II-15.1
    Column("title", Text, nullable=False),
    Column("issuer_authority", String(64), nullable=True),  # SPK, BIST, TBMM, ...
    _fk("source_document_id", "document.id"),
    _extra(),
    _created(),
    UniqueConstraint("jurisdiction_id", "kind", "official_code"),
)

provision = Table(
    "provision",
    metadata,
    _id(),
    _fk("regulation_id", "regulation.id", nullable=False),
    Column("path", String(128), nullable=False),  # m5/f1/a
    Column("citation_id", String(160), nullable=False, unique=True),  # tr:spk:II-15.1:m5:f1:a
    Column("heading", Text, nullable=True),
    Column("ordinal", Integer, nullable=True),  # document order
)

provision_version = Table(
    "provision_version",
    metadata,
    _id(),
    _fk("provision_id", "provision.id", nullable=False),
    Column("text", Text, nullable=False),
    Column("effective_from", Date, nullable=True),
    Column("effective_to", Date, nullable=True),
    Column("amended_by", Text, nullable=True),  # gazette ref
    _fk("source_span_id", "document_span.id"),  # provenance (KB-6)
    _fk("kb_snapshot_id", "kb_snapshot.id"),  # snapshot that introduced this version
    Column("recorded_at", UtcDateTime(), nullable=False),
)

obligation = Table(
    "obligation",
    metadata,
    _id(),
    _fk("provision_id", "provision.id", nullable=False),
    Column("event_codes", Json(), nullable=False),  # ["capital.increase.rights", ...]
    Column("actor", String(64), nullable=True),  # issuer | insider | board | ...
    Column("deadline_rule", Text, nullable=True),
    Column("required_fields", Json(), nullable=True),
)

# --------------------------------------------------------------------------------------------- §8 portfolio

portfolio = Table(
    "portfolio",
    metadata,
    _id(),
    Column("name", Text, nullable=False, unique=True),
    Column("base_currency", String(3), nullable=False, server_default="TRY"),
    Column("account_label", Text, nullable=True),
    Column("is_default", Boolean, nullable=False, server_default=text("0")),
    _created(),
)

chat_message = Table(
    "chat_message",
    metadata,
    _id(),
    Column("telegram_chat_id", Integer, nullable=False),
    Column("telegram_message_id", Integer, nullable=True),  # NULL until an outgoing message is sent
    _fk("reply_to_id", "chat_message.id"),
    Column("direction", String(4), nullable=False),  # in | out
    Column("text", Text, nullable=True),
    Column("sent_at", UtcDateTime(), nullable=True),  # Telegram time
    Column("processed_at", UtcDateTime(), nullable=True),
    Column("intent", String(16), nullable=True),  # trade | what_if | question | alert_reply | command
    _fk("related_event_id", "event.id"),
    Column("proposed_transaction", Json(), nullable=True),
    Column("status", String(16), nullable=True),  # proposed|confirmed|cancelled|answered|pending_limit
    _fk("analysis_id", "analysis.id"),
    Column("raw_update", Json(), nullable=True),  # verbatim Telegram update/message JSON
    _created(),
    UniqueConstraint("telegram_chat_id", "telegram_message_id", "direction"),
)

txn = Table(
    "txn",  # PRD entity `transaction`
    metadata,
    _id(),
    _fk("portfolio_id", "portfolio.id", nullable=False),
    _fk("instrument_id", "instrument.id"),  # NULL for cash-only fee/tax rows
    # buy | sell | dividend_cash | bonus_shares | rights_subscription | rights_sale | split | fee | tax |
    # transfer_in | transfer_out | opening_balance | reconcile_adjustment
    Column("type", String(32), nullable=False),
    Column("trade_date", Date, nullable=False, index=True),
    Column("traded_at", UtcDateTime(), nullable=True),  # exact time if known (e.g. Telegram message time)
    Column("settle_date", Date, nullable=True),
    Column("quantity", DecimalText(), nullable=False),  # signed per type semantics defined by portfolio slice
    Column("price", DecimalText(), nullable=True),
    Column("fees", DecimalText(), nullable=False, server_default="0"),
    Column("tax", DecimalText(), nullable=False, server_default="0"),
    Column("currency", String(3), nullable=False, server_default="TRY"),
    # manual | telegram | csv_template | reconcile | derived:<corporate_action_id>
    Column("source", String(64), nullable=False),
    _fk("chat_message_id", "chat_message.id"),
    _fk("reverses_id", "txn.id"),  # set on reversing (correction/undo) entries
    Column("dedupe_key", String(128), nullable=True, unique=True),  # e.g. CSV row hash → idempotent import
    Column("note", Text, nullable=True),
    _created(),
)

position_snapshot = Table(
    "position_snapshot",
    metadata,
    _fk("portfolio_id", "portfolio.id", nullable=False, index=False),
    _fk("instrument_id", "instrument.id", nullable=False),
    Column("as_of", Date, nullable=False),
    Column("quantity", DecimalText(), nullable=False),
    Column("avg_cost_try", DecimalText(), nullable=True),
    Column("cost_basis_usd", DecimalText(), nullable=True),
    Column("market_value_try", DecimalText(), nullable=True),
    Column("weight", Float, nullable=True),
    Column("computed_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("portfolio_id", "instrument_id", "as_of"),
)

pending_action = Table(
    "pending_action",
    metadata,
    _id(),
    _fk("portfolio_id", "portfolio.id", nullable=False),
    _fk("instrument_id", "instrument.id", nullable=False),
    _fk("corporate_action_id", "corporate_action.id"),
    Column("kind", String(32), nullable=False),  # bonus | rights | dividend_cash | split | ...
    Column("status", String(16), nullable=False, server_default="expected"),  # expected|applied|dismissed
    Column("proposal", Json(), nullable=True),  # computed quantities/costs shown to me
    _fk("transaction_id", "txn.id"),  # derived transaction once applied
    _created(),
    Column("resolved_at", UtcDateTime(), nullable=True),
    UniqueConstraint("portfolio_id", "corporate_action_id"),
)

watchlist = Table(
    "watchlist",
    metadata,
    _id(),
    Column("name", Text, nullable=False, unique=True),
    Column("instrument_ids", Json(), nullable=False),
    Column("sectors", Json(), nullable=True),
    Column("indices", Json(), nullable=True),
    _created(),
    _updated(),
)

alert_rule = Table(
    "alert_rule",
    metadata,
    _id(),
    Column("name", Text, nullable=False, unique=True),
    Column("filter", Text, nullable=False),  # DSL, 08 §3.1
    Column("channels", Json(), nullable=False),  # ["telegram", "macos"]
    Column("quiet_hours", Json(), nullable=True),  # {"start":"23:00","end":"07:30"} or null = global
    Column("enabled", Boolean, nullable=False, server_default=text("1")),
    _created(),
    _updated(),
)

notification = Table(
    "notification",
    metadata,
    _id(),
    _fk("alert_rule_id", "alert_rule.id"),
    _fk("event_id", "event.id"),
    _fk("disclosure_id", "disclosure.id"),
    Column("kind", String(16), nullable=False),  # instant | follow_up | revision | digest | ops
    Column("channel", String(16), nullable=False),  # telegram | macos
    Column("text", Text, nullable=True),
    Column("sent_at", UtcDateTime(), nullable=True),
    Column("delivered", Boolean, nullable=False, server_default=text("0")),
    Column("error", Text, nullable=True),
    Column("external_id", String(64), nullable=True),  # e.g. Telegram message_id (for reply threading)
    _fk("reply_to_notification_id", "notification.id"),
    Column("dedupe_key", String(160), nullable=True, unique=True),
    _created(),
)

feedback = Table(
    "feedback",
    metadata,
    _id(),
    Column("target_type", String(32), nullable=False),  # event | analysis | claim | disclosure | forecast
    Column("target_id", String(64), nullable=False),
    Column("kind", String(32), nullable=False),  # wrong_fact | wrong_type | not_material | helpful
    Column("comment", Text, nullable=True),
    _created(),
    Index(None, "target_type", "target_id"),
)

# --------------------------------------------------------------------------------------------- infra

event_log = Table(
    "event_log",
    metadata,
    Column("id", String(36), primary_key=True),  # CloudEvents id (UUIDv7)
    Column("specversion", String(8), nullable=False, server_default="1.0"),
    Column("source", Text, nullable=False),  # e.g. ingest/kap
    Column("type", String(96), nullable=False),  # <domain>.<entity>.<verb>.v<major>
    Column("subject", Text, nullable=True),  # e.g. disclosure/<id>
    Column("time", UtcDateTime(), nullable=False),
    Column("datacontenttype", String(64), nullable=False, server_default="application/json"),
    Column("traceparent", String(64), nullable=True),
    Column("data", Json(), nullable=True),
    Index(None, "type", "time"),
)

job = Table(
    "job",
    metadata,
    _id(),
    Column("kind", String(160), nullable=False, index=True),
    Column("payload", Json(), nullable=True),
    Column("priority", Integer, nullable=False, server_default=text("100")),  # lower = more urgent
    Column("not_before", UtcDateTime(), nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_until", UtcDateTime(), nullable=True),
    Column(
        "status", String(16), nullable=False, server_default="queued"
    ),  # queued|running|done|dead|cancelled
    Column("last_error", Text, nullable=True),
    Column("dedupe_key", String(255), nullable=True, unique=True),
    _fk("event_id", "event_log.id"),  # set for outbox fan-out jobs
    _created(),
    _updated(),
    Column("finished_at", UtcDateTime(), nullable=True),
    Index(None, "status", "priority", "created_at"),
)

kv_state = Table(
    "kv_state",
    metadata,
    Column("key", String(255), primary_key=True),
    Column("value", Json(), nullable=True),
    Column("updated_at", UtcDateTime(), nullable=False),
)

metric = Table(
    "metric",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(128), nullable=False),
    Column("labels", Json(), nullable=True),
    Column("value", Float, nullable=False),
    Column("at", UtcDateTime(), nullable=False),
    Index(None, "name", "at"),
)

# Names of FTS5 virtual tables created by the initial migration (not part of `metadata`).
FTS_TABLES = ("disclosure_fts", "provision_fts")
