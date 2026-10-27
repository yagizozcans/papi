"""Internal events: CloudEvents 1.0 envelope persisted in ``event_log`` + transactional outbox fan-out.

``publish(conn, type, subject, data, source)`` writes the ``event_log`` row and, in the **same
transaction**, one ``job`` per registered handler (kind ``event:<handler name>``, payload
``{"event_id", "type"}``, dedupe key ``<handler name>:<event id>``). Roll the transaction back and
neither exists. Delivery is at-least-once; handlers must be idempotent (dedupe on ``event.id``).

Type naming: ``<domain>.<entity>.<verb>.v<major>``. Data models below validate payloads for known
types (extra fields allowed, so producers can add context without a contract change).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, select

from papi.core.ids import uuid7
from papi.core.jobs import enqueue
from papi.core.registry import DEFAULT_PRIORITY, REGISTRY, handles, load_plugins
from papi.core.schema import event_log
from papi.core.timeutil import now_utc

__all__ = [
    "ALL_EVENT_TYPES",
    "EVENT_DATA_MODELS",
    "CloudEvent",
    "get_event",
    "handles",
    "publish",
]

# ---- event types (06 §9 + chat/portfolio) ----------------------------------------------------------
SOURCE_OBJECT_FETCHED = "source.object.fetched.v1"
DISCLOSURE_SEEN = "disclosure.seen.v1"
DISCLOSURE_NORMALIZED = "disclosure.normalized.v1"
DISCLOSURE_REVISED = "disclosure.revised.v1"
DISCLOSURE_BLOCKED = "disclosure.blocked.v1"
INGEST_GAP_DETECTED = "ingest.gap.detected.v1"
EVENT_CLASSIFIED = "event.classified.v1"
ANALYSIS_COMPLETED = "analysis.completed.v1"
FORECAST_CREATED = "forecast.created.v1"
OUTCOME_RECORDED = "outcome.recorded.v1"
ALERT_TRIGGERED = "alert.triggered.v1"
KB_VERSION_PUBLISHED = "kb.version.published.v1"
CHAT_MESSAGE_RECEIVED = "chat.message.received.v1"
PORTFOLIO_CHANGED = "portfolio.changed.v1"


class _Data(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourceObjectFetched(_Data):
    raw_sha256: str
    source_id: str
    url: str | None = None


class DisclosureSeen(_Data):
    source_ref: str
    published_at: datetime | None = None
    source_id: str | None = None


class DisclosureNormalized(_Data):
    disclosure_id: str
    source_ref: str | None = None
    filer_issuer_id: str | None = None
    related_issuer_ids: list[str] = Field(default_factory=list)
    source_subject: str | None = None
    is_catchup: bool = False


class DisclosureRevised(_Data):
    disclosure_id: str
    revision_of: str
    revision_kind: str


class DisclosureBlocked(_Data):
    disclosure_id: str


class IngestGapDetected(_Data):
    source_id: str
    missing_refs: list[str]


class EventClassified(_Data):
    event_id: str
    code: str
    materiality_band: str | None = None
    priority: str | None = None
    disclosure_id: str | None = None


class AnalysisCompleted(_Data):
    event_id: str
    analysis_ids: list[str]


class ForecastCreated(_Data):
    forecast_id: str
    event_id: str
    horizons: list[str] = Field(default_factory=list)


class OutcomeRecorded(_Data):
    forecast_id: str
    scores: dict[str, Any] = Field(default_factory=dict)


class AlertTriggered(_Data):
    alert_rule_id: str | None
    event_id: str | None
    notification_id: str | None = None


class KbVersionPublished(_Data):
    kb_snapshot: str


class ChatMessageReceived(_Data):
    chat_message_id: str
    telegram_chat_id: int
    telegram_message_id: int | None = None


class PortfolioChanged(_Data):
    portfolio_id: str
    instrument_ids: list[str] = Field(default_factory=list)
    transaction_ids: list[str] = Field(default_factory=list)
    reason: str | None = None  # trade | undo | import | reconcile | corporate_action


EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    SOURCE_OBJECT_FETCHED: SourceObjectFetched,
    DISCLOSURE_SEEN: DisclosureSeen,
    DISCLOSURE_NORMALIZED: DisclosureNormalized,
    DISCLOSURE_REVISED: DisclosureRevised,
    DISCLOSURE_BLOCKED: DisclosureBlocked,
    INGEST_GAP_DETECTED: IngestGapDetected,
    EVENT_CLASSIFIED: EventClassified,
    ANALYSIS_COMPLETED: AnalysisCompleted,
    FORECAST_CREATED: ForecastCreated,
    OUTCOME_RECORDED: OutcomeRecorded,
    ALERT_TRIGGERED: AlertTriggered,
    KB_VERSION_PUBLISHED: KbVersionPublished,
    CHAT_MESSAGE_RECEIVED: ChatMessageReceived,
    PORTFOLIO_CHANGED: PortfolioChanged,
}

ALL_EVENT_TYPES: tuple[str, ...] = tuple(EVENT_DATA_MODELS)


class CloudEvent(BaseModel):
    """CloudEvents 1.0 envelope as stored in ``event_log``."""

    specversion: str = "1.0"
    id: str
    source: str
    type: str
    subject: str | None = None
    time: datetime
    datacontenttype: str = "application/json"
    traceparent: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def data_as[M: BaseModel](self, model: type[M]) -> M:
        """Parse ``data`` into a pydantic model (e.g. ``ev.data_as(DisclosureNormalized)``)."""
        return model.model_validate(self.data)


def _normalise_data(event_type: str, data: BaseModel | dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        data = {}
    model = EVENT_DATA_MODELS.get(event_type)
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if model is not None:
        return model.model_validate(data).model_dump(mode="json")
    return dict(data)


def publish(
    conn: Connection,
    type: str,
    subject: str | None,
    data: BaseModel | dict[str, Any] | None,
    source: str,
    *,
    priority: int | None = None,
    traceparent: str | None = None,
    not_before: datetime | None = None,
) -> str:
    """Write the event + one job per registered handler in ``conn``'s transaction; returns the event id.

    ``priority`` is the publisher's hint used for handlers that don't fix their own priority.
    The caller commits (use ``papi.core.db.transaction()`` for the state change and the publish).
    """
    load_plugins()
    event_id = uuid7()
    now = now_utc()
    payload = _normalise_data(type, data)
    conn.execute(
        event_log.insert().values(
            id=event_id,
            specversion="1.0",
            source=source,
            type=type,
            subject=subject,
            time=now,
            datacontenttype="application/json",
            traceparent=traceparent,
            data=payload,
        )
    )
    handlers = REGISTRY.handlers_for(type)
    if handlers:
        envelope = CloudEvent(
            id=event_id,
            source=source,
            type=type,
            subject=subject,
            time=now,
            traceparent=traceparent,
            data=payload,
        )
        for spec in handlers:
            if callable(spec.priority):
                prio = int(spec.priority(envelope))
            elif spec.priority is not None:
                prio = spec.priority
            elif priority is not None:
                prio = priority
            else:
                prio = DEFAULT_PRIORITY
            enqueue(
                conn,
                spec.job_kind,
                {"event_id": event_id, "type": type},
                priority=prio,
                not_before=not_before,
                dedupe_key=f"{spec.name}:{event_id}",
                max_attempts=spec.max_attempts,
                event_id=event_id,
            )
    return event_id


def get_event(conn: Connection, event_id: str) -> CloudEvent | None:
    row = conn.execute(select(event_log).where(event_log.c.id == event_id)).first()
    if row is None:
        return None
    m = dict(row._mapping)
    m["data"] = m.get("data") or {}
    return CloudEvent.model_validate(m)
