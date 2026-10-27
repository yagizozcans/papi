# Internal events

Envelope: CloudEvents 1.0, stored in table `event_log` (`id`, `specversion`, `source`, `type`, `subject`,
`time`, `datacontenttype`, `traceparent`, `data`). Published with
`papi.core.events.publish(conn, type, subject, data, source, *, priority=None, traceparent=None, not_before=None)`
in the same transaction as the state change; one `job` per registered handler is enqueued
(outbox). Delivery is at-least-once: handlers dedupe on the event `id`.

Constants live in `papi.core.events` (name in the first column); data models are pydantic classes
there (extra fields allowed). `source` is the producer path, e.g. `ingest/kap`, `triage`, `llm/analyst`.
`subject` is `<entity>/<id>`.

| Constant | `type` | Producer → consumers | `data` fields (model) |
|---|---|---|---|
| `SOURCE_OBJECT_FETCHED` | `source.object.fetched.v1` | fetchers → parsers | `raw_sha256`, `source_id`, `url?` (`SourceObjectFetched`) |
| `DISCLOSURE_SEEN` | `disclosure.seen.v1` | discovery → fetchers, latency monitor | `source_ref`, `published_at?`, `source_id?` (`DisclosureSeen`) |
| `DISCLOSURE_NORMALIZED` | `disclosure.normalized.v1` | normalizer → triage, feed, alerts | `disclosure_id`, `source_ref?`, `filer_issuer_id?`, `related_issuer_ids[]`, `source_subject?`, `is_catchup` (`DisclosureNormalized`) |
| `DISCLOSURE_REVISED` | `disclosure.revised.v1` | normalizer → agents, UI, alerts | `disclosure_id`, `revision_of`, `revision_kind` (`DisclosureRevised`) |
| `DISCLOSURE_BLOCKED` | `disclosure.blocked.v1` | normalizer → UI | `disclosure_id` (`DisclosureBlocked`) |
| `INGEST_GAP_DETECTED` | `ingest.gap.detected.v1` | gap detector → ops, backfill | `source_id`, `missing_refs[]` (`IngestGapDetected`) |
| `EVENT_CLASSIFIED` | `event.classified.v1` | triage → feed, alerts, LLM pipeline | `event_id`, `code`, `materiality_band?`, `priority?` (`P1`..`P4`), `disclosure_id?` (`EventClassified`) |
| `ANALYSIS_COMPLETED` | `analysis.completed.v1` | LLM orchestrator → UI, alerts | `event_id`, `analysis_ids[]` (`AnalysisCompleted`) |
| `FORECAST_CREATED` | `forecast.created.v1` | simulation → UI, outcome tracker | `forecast_id`, `event_id`, `horizons[]` (`ForecastCreated`) |
| `OUTCOME_RECORDED` | `outcome.recorded.v1` | outcome tracker → calibration, UI | `forecast_id`, `scores{}` (`OutcomeRecorded`) |
| `ALERT_TRIGGERED` | `alert.triggered.v1` | alert engine → notifier | `alert_rule_id?`, `event_id?`, `notification_id?` (`AlertTriggered`) |
| `KB_VERSION_PUBLISHED` | `kb.version.published.v1` | KB admin → agents (cache bust) | `kb_snapshot` (`KbVersionPublished`) |
| `CHAT_MESSAGE_RECEIVED` | `chat.message.received.v1` | telegram poller → chat/trade handlers | `chat_message_id`, `telegram_chat_id`, `telegram_message_id?` (`ChatMessageReceived`) |
| `PORTFOLIO_CHANGED` | `portfolio.changed.v1` | portfolio → triage priority cache, UI | `portfolio_id`, `instrument_ids[]`, `transaction_ids[]`, `reason?` (`trade`/`undo`/`import`/`reconcile`/`corporate_action`) (`PortfolioChanged`) |

## Job priorities

Lower number = more urgent (`papi.core.jobs`): `PRIORITY_CHAT=5`, `PRIORITY_P1=10`, `PRIORITY_P2=20`,
`PRIORITY_P3=30`, `PRIORITY_P4=40`, `PRIORITY_BACKFILL=80`; default 50 for handlers/periodics,
100 for `enqueue`. `priority_for("P1")` maps labels to numbers.

Handler job priority = handler's own `priority` (int or `callable(CloudEvent) -> int`) → else
`publish(priority=...)` → else 50.
