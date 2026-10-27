# Foundation API (papi.core)

> Written by the foundation slice; authoritative reference for all slices.

The foundation is in place and passes all three acceptance checks:
- **Init:** `uv sync && uv run papi init && uv run papi init` works with a temp `PAPI_HOME`. The first run creates the dirs, config and database and seeds the rows. The second run only prints "already initialised … (revision 0001)".
- **Tests:** `uv run pytest tests/core -q` gives 81 passed.
- **Lint:** `uv run ruff check src tests alembic` is clean and `ruff format --check` passes.

The integrator still needs to commit everything (it is all untracked) and run the whole suite once the other slices land.

## Files created
- `pyproject.toml` (built with hatchling), `uv.lock`, `.python-version` (3.12), `alembic.ini`, `README.md`.
- `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_initial_schema.py` (all tables below plus the two FTS5 tables).
- `src/papi/__init__.py` (`__version__ = "0.1.0"`) and `src/papi/cli.py`.
- `src/papi/core/`: `config`, `db`, `schema`, `types`, `ids`, `timeutil`, `text`, `rawstore`, `events`, `jobs`, `registry`, `runtime`, `state`, `fts`, `metrics`, `logging`, `bootstrap`.
- Empty packages, each with a docstring naming its PRD doc: `papi.sources`, `.sources.kap`, `.sources.prices`, `.sources.legal`, `.kb`, `.triage`, `.llm`, `.verify`, `.sim`, `.portfolio`, `.notify`, `.telegram`, `.web`, `.run`, `.evals`.
- Tests: `tests/__init__.py`, plus `tests/{core,kap,prices,legal,kb,triage,llm,verify,sim,portfolio,notify,telegram,web,run,evals}/__init__.py`.
- `tests/conftest.py` and five test files in `tests/core/`.
- `contracts/README.md`, `contracts/events.md`, an empty `contracts/schemas/`, and `packs/tr/README.md`.

**Added beyond the brief:** the wheel bundles `alembic/`, `packs/` and `contracts/` under `papi/_resources/`, so papi also works when installed outside the repo (`uv tool install .`). `resource_dir()` picks the repo copy first and the bundled copy otherwise.

## Dependencies
- **Runtime:** sqlalchemy (2.1.6 resolved), alembic, pydantic 2, typer, rich, httpx, fastapi, uvicorn[standard], jinja2, python-multipart, sse-starlette, keyring, jsonschema, pyyaml, beautifulsoup4, lxml, pdfplumber, pypdf, openpyxl, xlrd, tzdata.
- **Dev:** pytest, pytest-asyncio, respx, ruff, time-machine.
- **Settings:** ruff line length 110; pytest uses `asyncio_mode=auto` and `testpaths=tests`.

## Conventions every slice needs
- **IDs** are UUIDv7 strings from `uuid7()`. There are no database defaults for `id`.
- **Timestamps** (columns marked `ts` below) use `UtcDateTime`. They are stored as fixed-width text `YYYY-MM-DDTHH:MM:SS.ffffffZ`, accept an aware datetime or an ISO string, return an aware UTC datetime, and **reject naive datetimes**.
- **Dates** use `Date` (`YYYY-MM-DD`).
- **Exact numbers** (`dec`) use `DecimalText`: stored as text, returned as `Decimal`. This covers portfolio money and quantities and corporate-action factors and amounts. Market data uses `float`.
- **JSON columns** (`json`) store Python `None` as SQL NULL.
- **Enumerations** are plain strings with no CHECK constraints. Allowed values are in comments in `schema.py`.
- **The PRD's `transaction` table is named `txn`.**
- **Validity dates:** `valid_from` is NOT NULL on `identifier`, `listing` and `index_membership`. Use `schema.OPEN_START` (1900-01-01) for "since forever" so the unique constraints hold.
- **Index names** follow `ix_<table>_<cols>`; unique constraints follow `uq_<table>_<cols>`.
- **`extra` JSON column** for stashing slice fields: issuer, instrument, disclosure, document, event, regulation.

## Tables
`!` = NOT NULL; `→t` = foreign key to table t.

**Reference**
- **jurisdiction**: id, code! (unique), name!, default_tz!, default_currency!, languages:json!, pack_version, created_at!
- **market**: id, jurisdiction_id!→jurisdiction, code! (unique), name!, asset_class!, created_at!
- **venue**: id, market_id!→market, mic! (unique), name!, calendar_id, created_at!
- **trading_calendar**: id, calendar_id!, date!, session_type! (regular/half_day/closed), open_utc:ts, close_utc:ts, is_half_day:bool!, note. Unique (calendar_id, date, session_type).
- **issuer**: id, legal_name!, short_name, country, sector_code (indexed), status! (default active), kind, extra, created_at!, updated_at
- **instrument**: id, issuer_id→issuer, type!, currency! (default TRY), status!, extra, created_at!, updated_at
- **listing**: id, instrument_id!, venue_id!, segment, valid_from:date!, valid_to. Unique (instrument_id, venue_id, valid_from).
- **identifier**: id, entity_type! (issuer/instrument), entity_id!, scheme! (TICKER, ISIN, KAP_MEMBER_OID, KAP_COMPANY_OID, LEI, MERSIS, VKN), value!, valid_from:date!, valid_to, recorded_at:ts!, superseded_at:ts.
  - Unique (scheme, value, valid_from); indexes on (entity_type, entity_id) and (scheme, value).
- **index_membership**: id, index_code! (indexed), instrument_id!, weight:float, valid_from!, valid_to. Unique (index_code, instrument_id, valid_from).
- **relationship**: id, from_issuer_id!, to_issuer_id!, type!, source_claim_id→claim, valid_from, valid_to, recorded_at!

**Capture**
- **source**: id, code! (unique), jurisdiction_id, access_mode!, rate_limit, terms_note, status!, created_at!
- **raw_object**: sha256 (primary key), source_id!, url, http_status:int, request_meta:json, response_headers:json, fetched_at:ts!, content_type, byte_size:int!, storage_uri! (`raw/ab/<sha>`)
- **fetch_log**: id, source_id!, raw_sha256 (NULL when the fetch failed), url, http_status, fetcher_version, started_at!, finished_at, outcome! (ok/http_error/timeout/blocked/error), error
- **disclosure**: id, source_id!, source_ref!, source_uid (indexed), revision_of→disclosure, revision_kind! (original/correction/update/blocked), published_at:ts (indexed), published_at_raw, first_seen_at!, source_class, source_type, source_category, source_subject, title, summary, filer_issuer_id→issuer, related_issuer_ids:json, is_late:bool!, languages:json, body_document_id→document, attachment_document_ids:json, source_payload:json, ingest_status! (seen/fetched/parsed/normalized/`failed:<stage>`, indexed), ingest_error, extra, created_at!, updated_at.
  - Unique (source_id, source_ref).
- **document**: id, raw_sha256→raw_object, disclosure_id→disclosure, kind!, mime, language, text, structure:json, parser, parser_version, parsed_at!, extra
- **document_span**: id, document_id!, ordinal:int (the `[S12]` span marker), page, char_start, char_end, bbox:json, field_path, text

**Understanding**
- **event**: id, disclosure_ids:json!, primary_disclosure_id→disclosure, code! (indexed), taxonomy_version!, issuer_ids:json, instrument_ids:json, occurred_at, announced_at (indexed), payload:json, payload_source, payload_schema, materiality:int, materiality_band, priority (`P1`–`P4`, indexed), status! (default provisional), superseded_by→event, extra, created_at!, updated_at
- **analysis**: id, event_id!, agent!, agent_version, backend!, model_ids:json, prompt_version, schema_version, kb_snapshot→kb_snapshot, input_hash (indexed), output:json, raw_output:json, confidence, latency_ms, cost_usd_est, priority, created_at!, review_state! (default auto)
- **llm_usage_ledger**: id, analysis_id, task!, priority, model_ids:json, cost_usd_est:float!, duration_ms, outcome! (ok/schema_fail/limit_hit/error), error, at:ts! (indexed)
- **claim**: id, analysis_id!, text_tr, text_en, kind!, value:text, unit, formula_id, check_status, verify_status
- **citation**: id, claim_id!, target_type!, target_id!, quote. Index on (target_type, target_id).
- **summary**: id, event_id!, lang!, headline, body, analysis_id, created_at!. Unique (event_id, lang).

**Market**
- **price_bar**: primary key (instrument_id, interval, ts:date, source_id); open, high, low, close, volume, value_traded, limit_up, limit_down, cross_check, recorded_at!, raw_sha256
- **corporate_action**: id, instrument_id!, type!, ex_date (indexed), factor:dec, cash_amount:dec, currency, source_event_id→event, status! (default announced), details:json, created_at!, updated_at
- **fx_rate** and **macro_series**: primary key (series, ts:date, vintage default ''); value:float!, source_id, recorded_at!
- **feature_snapshot**: id, instrument_id!, as_of:ts!, feature_set_version!, features:json!. Unique (instrument_id, as_of, feature_set_version).
- **forecast**: id, event_id!, instrument_id!, model!, model_version!, created_at!, horizon!, target!, distribution:json, scenarios:json, position_impact:json, frozen_hash!
- **outcome**: id, forecast_id! (unique), realised_value, realised_at, scores:json, created_at!

**Legal**
- **kb_snapshot**: id, status! (draft/published/superseded), content_hash, note, stats:json, created_at!, published_at
- **regulation**: id, jurisdiction_id!, kind!, official_code!, title!, issuer_authority, source_document_id→document, extra, created_at!. Unique (jurisdiction_id, kind, official_code).
- **provision**: id, regulation_id!, path!, citation_id! (unique), heading, ordinal
- **provision_version**: id, provision_id!, text!, effective_from:date, effective_to, amended_by, source_span_id→document_span, kb_snapshot_id→kb_snapshot, recorded_at!
- **obligation**: id, provision_id!, event_codes:json!, actor, deadline_rule, required_fields:json

**Portfolio and personal**
- **portfolio**: id, name! (unique), base_currency!, account_label, is_default:bool!, created_at!
- **txn**: id, portfolio_id!, instrument_id (nullable), type!, trade_date:date! (indexed), traded_at:ts, settle_date, quantity:dec!, price:dec, fees:dec! (default 0), tax:dec! (default 0), currency!, source!, chat_message_id→chat_message, reverses_id→txn, dedupe_key (unique), note, created_at!
- **position_snapshot**: primary key (portfolio_id, instrument_id, as_of:date); quantity:dec!, avg_cost_try:dec, cost_basis_usd:dec, market_value_try:dec, weight:float, computed_at!
- **pending_action**: id, portfolio_id!, instrument_id!, corporate_action_id, kind!, status! (default expected), proposal:json, transaction_id→txn, created_at!, resolved_at. Unique (portfolio_id, corporate_action_id).
- **watchlist**: id, name! (unique), instrument_ids:json!, sectors:json, indices:json, created_at!, updated_at
- **alert_rule**: id, name! (unique), filter!, channels:json!, quiet_hours:json, enabled:bool! (default 1), created_at!, updated_at
- **notification**: id, alert_rule_id, event_id, disclosure_id, kind!, channel!, text, sent_at, delivered:bool!, error, external_id (Telegram message id), reply_to_notification_id→notification, dedupe_key (unique), created_at!
- **feedback**: id, target_type!, target_id!, kind!, comment, created_at!
- **chat_message**: id, telegram_chat_id:int!, telegram_message_id:int, reply_to_id→chat_message, direction! (in/out), text, sent_at, processed_at, intent, related_event_id→event, proposed_transaction:json, status, analysis_id, raw_update:json, created_at!. Unique (telegram_chat_id, telegram_message_id, direction).

**Infrastructure**
- **event_log**: id (CloudEvents id), specversion!, source!, type!, subject, time:ts!, datacontenttype!, traceparent, data:json. Index on (type, time).
- **job**: id, kind! (indexed), payload:json, priority:int! (default 100), not_before:ts, attempts!, max_attempts! (default 5), lease_owner, lease_until:ts, status! (queued/running/done/dead/cancelled), last_error, dedupe_key (unique), event_id→event_log, created_at!, updated_at, finished_at. Index on (status, priority, created_at).
- **kv_state**: key (primary key), value:json, updated_at!
- **metric**: id (integer, auto-increment), name!, labels:json, value:float!, at:ts!. Index on (name, at).
- **FTS5 tables** (migration only, listed in `schema.FTS_TABLES`): `disclosure_fts(id UNINDEXED, text)` and `provision_fts(id UNINDEXED, text)`. Tokenizer: unicode61 with remove_diacritics 2. The `id` is `disclosure.id` or `provision_version.id`.

## Public API in `papi.core`
- **config**
  - `papi_home() -> Path`, `resource_dir(name) -> Path`, `REPO_ROOT`
  - `load_settings(home=None) -> Settings`, `get_settings() -> Settings` (cached), `reset_settings()`, `config_path(home=None)`
  - `get_telegram_token() -> str|None` reads env `PAPI_TELEGRAM_TOKEN`, then keyring service `papi` key `telegram_token`.
  - `get_web_token() -> str|None` reads env `PAPI_WEB_TOKEN`, then keyring `papi`/`web_token`, then `web.token`.
  - `set_secret(key, value)`, `render_default_config() -> str`, `write_default_config(home=None) -> bool`
  - `Settings.config_path`, `Settings.pack_dir`, and `Settings.section(name) -> dict` for unknown sections.
- **db**
  - `get_engine(path=None)`, `reset_engines()`
  - `connect()`: a context manager whose connection begins a deferred transaction automatically; call `commit()` after writes.
  - `transaction()`: a context manager that issues `BEGIN IMMEDIATE`, commits on success and rolls back on error.
  - `migrate(path=None, revision="head")`: turns foreign keys off during the migration and runs `foreign_key_check` afterwards.
  - `current_revision(path=None)`, `alembic_config(path=None)`
- **bootstrap**: `init_home() -> InitReport`, where `InitReport` has `.changed`, `.seeded`, `.revision_before/after`, `.created_dirs`, `.config_written`, `.db_created`. Also `seed(conn, settings=None) -> dict[str,int]`.
- **ids**: `uuid7() -> str` (monotonic within the process), `uuid7_time(id) -> datetime`.
- **timeutil**
  - `IST`, `now_utc()`, `now_ist()`
  - `to_utc(dt|str, *, assume_tz=IST)`: naive input is taken as Istanbul time. `to_ist(dt|str)`
  - `parse_iso(s, *, assume_tz=UTC)`, `iso_utc(dt)` (rejects naive), `ist_date(dt=None)`
  - `parse_kap_datetime(s) -> aware UTC` accepts `DD.MM.YYYY` or `YYYY.MM.DD`, with optional `HH:MM[:SS]`.
- **text**: `tr_lower`, `tr_upper`, `fold_diacritics`, `normalize_tr`, `parse_tr_number(s, *, decimal="auto"|"comma"|"dot") -> Decimal`.
  - When both `.` and `,` appear, the last one is the decimal mark.
  - A single comma is the decimal mark (`285,40`, and `1,234` → 1.234).
  - A single dot followed by exactly three digits is a thousands separator (`62.500` → 62500); otherwise the dot is decimal.
  - Multiple separators of one kind are thousands separators; bad grouping raises `ValueError`.
- **rawstore**
  - `put_bytes(data, *, source_code, url=None, http_status=None, content_type=None, request_meta=None, response_headers=None, fetcher_version=None, started_at=None, conn=None) -> sha`
  - `log_fetch_failure(*, source_code, url, outcome, error=None, http_status=None, fetcher_version=None, started_at=None, conn=None) -> id`
  - `get_bytes(sha, *, verify=False)`, `exists(sha)`, `path_for(sha)`, `storage_uri(sha)`, `sha256_hex(data)`, `source_id_for(conn, code)`, `UnknownSourceError`
- **events**
  - `publish(conn, type, subject, data, source, *, priority=None, traceparent=None, not_before=None) -> event_id`
    - Writes the `event_log` row and one job per handler in the same transaction.
    - Each job has kind `event:<handler>`, payload `{"event_id","type"}` and dedupe key `<handler>:<event_id>`.
    - Data for known event types is validated against its pydantic model.
  - `get_event(conn, id) -> CloudEvent|None`; `CloudEvent.data_as(Model)`
  - `handles`, `EVENT_DATA_MODELS`, `ALL_EVENT_TYPES`
  - Event constants: `SOURCE_OBJECT_FETCHED`, `DISCLOSURE_SEEN`, `DISCLOSURE_NORMALIZED`, `DISCLOSURE_REVISED`, `DISCLOSURE_BLOCKED`, `INGEST_GAP_DETECTED`, `EVENT_CLASSIFIED`, `ANALYSIS_COMPLETED`, `FORECAST_CREATED`, `OUTCOME_RECORDED`, `ALERT_TRIGGERED`, `KB_VERSION_PUBLISHED`, `CHAT_MESSAGE_RECEIVED`, `PORTFOLIO_CHANGED`. The data fields for each are in `contracts/events.md`.
- **registry**
  - `handles(event_type, *, priority=None|int|callable(ev), name=None, max_attempts=5)`. Handler signature: `fn(event, ctx)`.
    - Job priority: the handler's own value, else `publish`'s `priority`, else 50.
  - `job_kind(kind, *, max_attempts=5)`. Signature: `fn(payload, ctx)`.
  - `periodic(name, seconds|callable(now)->s, *, priority=50, run_on_start=True)`. Signature: `fn(ctx)`.
  - Functions may be sync or async.
  - `load_plugins()` imports `papi.<slice>.handlers` for: sources.kap, sources.prices, sources.legal, kb, triage, llm, verify, sim, portfolio, notify, telegram, web, run, evals.
  - `snapshot()`, `restore()`, `clear()`, `REGISTRY`, `autoload_plugins`.
- **jobs**
  - Priority constants: `PRIORITY_CHAT=5`, `P1=10`, `P2=20`, `P3=30`, `P4=40`, `BACKFILL=80`; `priority_for(label, default=50)`.
  - `enqueue(conn, kind, payload=None, *, priority=100, not_before=None, dedupe_key=None, max_attempts=None, event_id=None) -> id`. A duplicate `dedupe_key` returns the existing job's id.
  - `lease(worker_id, kinds=None, lease_seconds=300) -> Job|None`
    - Orders by priority, then `created_at`; skips jobs whose `not_before` is in the future.
    - Reclaims expired leases; a job whose lease expired after its last attempt becomes dead.
  - `complete(id, *, worker_id=None, conn=None) -> bool`
  - `fail(id, error, *, retry_in=None, permanent=False, worker_id=None, conn=None) -> "queued"|"dead"|""`. Backoff is 10 s × 2^(attempts−1), capped at 1 h.
  - `defer(id, until, *, reason=None, worker_id=None, conn=None)`: re-queues without using up an attempt.
  - `extend_lease(id, worker_id, seconds)`, `cancel(conn, id)`, `get_job(conn, id)`, `counts(conn) -> {(status, priority): n}`, `backoff_seconds(n)`.
  - Exceptions handlers can raise: `RetryLater(seconds)`, `PermanentError`, `Deferred(until, reason)`.
- **runtime**
  - `JobContext(job, worker_id, lease_seconds)` with `.attempt` and `.heartbeat(seconds=None)`.
  - `execute_job(job, worker_id, *, lease_seconds=300) -> status`
  - `run_worker(stop_event, *, kinds=None, concurrency=1, worker_id=None, poll_interval=0.5, lease_seconds=300)`: `kinds=None` means all registered kinds.
  - `schedule_due(now=None) -> [names]`: enqueues `periodic:<name>` jobs, skipping any that already has a queued or running job; state is kept in `kv_state` under `periodic:<name>`.
  - `run_scheduler(stop_event, *, tick=1.0)`, `default_worker_id(suffix="")`.
- **state**: `get_state(conn, key, default=None)`, `set_state(conn, key, value)`, `delete_state(conn, key)`. Key format is `<slice>:<name>`.
- **fts**: `index_text(conn, table, row_id, content)` (replaces existing text, normalises it), `delete_text`, `build_query(q, *, prefix=True)`, `search(conn, table, query, *, limit=50, prefix=True) -> [FtsHit(id, snippet, rank)]`.
- **metrics**: `record(name, value, /, *, conn=None, at=None, **labels)`, `series(conn, name, *, since=None, limit=1000)`.
- **logging**: `setup_logging(settings=None, *, level=None, console=None)` writes JSON lines to `logs/papi.log` with rotation; `get_logger(name)` gives `papi.<name>`; `JsonFormatter`.

## CLI
- Core commands: `papi init`, `papi version`, `papi db migrate`, `papi db current`, `papi config show [--json]`, `papi config path`.
- **Mounting:** each slice's `papi.<module>.cli:app` is mounted as a command group:

| Module | Group |
|---|---|
| portfolio | portfolio |
| sources.kap | kap |
| sources.prices | prices |
| kb | kb |
| triage | triage |
| llm | llm |
| telegram | telegram |
| web | web |
| run | run |
| evals | eval |

- Only a `ModuleNotFoundError` whose name is exactly `papi.<module>.cli` is ignored; any other import error surfaces. A test covers this.

## Config keys (`$PAPI_HOME/config.toml`)
`papi init` writes this file with comments and every default.
- **paths**: db `papi.db`, raw `raw`, logs `logs`, backups `backups`, claude_sandbox `claude-sandbox`. These are relative to the home dir, and the home comes only from `$PAPI_HOME`.
- **packs**: jurisdiction `TR`; dir defaults to `resource_dir("packs")`.
- **kap**: base_url, contact, user_agent (`{contact}` is substituted; use `.resolved_user_agent`), session_poll_seconds 30, offhours_poll_seconds 180, request_timeout 20.0, max_retries 5, min_request_interval 1.0, backfill_start 2015-01-01, reconcile_window_hours 48, reconcile_every_hours 4, attachments_fetch_policy all|prioritized.
- **prices**: primary `isyatirim_prices`, secondary `yahoo_prices`, fetch_time_ist `18:45`, history_start, request_timeout, mismatch_tolerance_pct 0.5.
- **telegram**: enabled false, allowed_user_id, chat_id, poll_timeout_s 30, max_messages_per_minute 20, price_warn_pct 10, undo_hours 24.
- **llm**:
  - backend `claude_code_cli`, claude_bin `claude`
  - models {analyst, explain, checker, backlog, chat}, all `sonnet`
  - max_turns 3, chat_max_turns 8, timeout_s 180, concurrency 1
  - share_5h 2.0, share_week 20.0, share_chat_5h 1.0, p2_cap_fraction 0.8, p3_cap_fraction 0.5
  - calibration_mode true, calibration_cost_per_pct_5h, calibration_cost_per_pct_week, calibrated_at
  - backlog_enabled false, night_window `01:00-07:00`, backlog_max_week_fraction 0.5
  - focus: list of [[llm.focus]] windows {days, start, end, max_priority}
  - include_position true
- **web**: host 127.0.0.1, port 8765, token.
- **alerts**: quiet_hours_enabled, quiet_start 23:00, quiet_end 07:30, holdings_override_quiet, catchup_batch_threshold 5, macos_notifications, digest_time 08:00, weekly_review `sun 18:00`.
- **portfolio**: default_name `Ana`, base_currency TRY, account_label `İşCep`, lot_size 1, recently_sold_days 30.
- **triage**: p3_materiality_threshold 60, always_tell_codes [`market.measure`].
- **logging**: level, console, max_bytes, backups.
- **backup**: keep_daily 14, keep_weekly 8.

## Test fixtures (`tests/conftest.py`)
- **`papi_home`** (applies to every test automatically): a temp home, with `PAPI_HOME` set, the keyring disabled, and the settings and engine caches reset.
- **`settings`**: the `Settings` for that temp home.
- **`db`**: a migrated, seeded database copied from a template built once per session. It yields an open connection that begins a deferred transaction on first use. Call `commit()` after writes, and `rollback()` before re-reading data written by other connections.
- **`clean_registry`**: an empty registry with plugin autoloading off; the original is restored afterwards.

## Known gaps
- `papi init` does not seed the default alert rules from 08 §3.1. Their filter text belongs to the notify slice's alert-rule language, which doesn't exist yet. The notify slice should seed them, or ask for an init hook.
- `disclosure_fts` and `provision_fts` are not kept in sync automatically. Writers must call `fts.index_text()`.

## Integration requests
None.