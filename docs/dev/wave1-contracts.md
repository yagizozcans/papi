# Wave 1 — cross-slice contracts

Authoritative for all wave-1 slices. Foundation API: [`foundation-api.md`](foundation-api.md). PRD: `PRD.md`, `docs/prd/*.md`.
Slices are built **in parallel against these signatures**. If you consume another slice's API, code against the signature here; in your tests, seed DB rows directly or monkeypatch the other slice's function. Never edit another slice's files.

## Ownership

| Slice | Owns (write) |
|---|---|
| KAP | `src/papi/sources/kap/**`, `tests/kap/**`, `tests/fixtures/kap/**`, `packs/tr/kap/**` |
| Portfolio | `src/papi/portfolio/**`, `tests/portfolio/**` |
| Notify+Telegram | `src/papi/notify/**`, `src/papi/telegram/**`, `tests/notify/**`, `tests/telegram/**` |
| Triage | `src/papi/triage/**`, `tests/triage/**`, `packs/tr/taxonomy.yaml`, `packs/tr/kap_subject_map.yaml`, `contracts/schemas/event.*.json` |
| LLM | `src/papi/llm/**`, `tests/llm/**` |
| KB | `src/papi/sources/legal/**`, `src/papi/kb/**`, `tests/legal/**`, `tests/kb/**`, `packs/tr/legal/**` |
| Prices+Calc | `src/papi/sources/prices/**`, `src/papi/sim/**`, `tests/prices/**`, `tests/sim/**` |
| Web | `src/papi/web/**`, `tests/web/**` |

Each slice registers background work in `papi/<slice>/handlers.py` (auto-imported by `papi.core.registry.load_plugins()`), and CLI in `papi/<slice>/cli.py` exposing a Typer `app`.

## C1. Parsed document structure (KAP produces; Triage, LLM, Web consume)

`document.text`: full plain text (TR). `document.structure` JSON:

```json
{
  "template_id": "tbl_oda-10400_Shares-Transaction-Notification",
  "form_fields": [
    {"label": "Yapılan Açıklama Düzeltme mi?", "label_norm": "<normalize_tr(label)>", "value": "Hayır",
     "field_id": "oda_CorrectionAnnouncementFlag", "span_ordinal": 3}
  ],
  "tables": [
    {"caption": "…", "header": ["…"], "rows": [["…", "…"]], "span_ordinals": [[7, 8]]}
  ],
  "paragraphs": [{"text": "…", "span_ordinal": 12}]
}
```

`document_span` rows: `ordinal` 1..N per document (rendered to LLM as `[S<ordinal>]`), `text`, `char_start/char_end` into `document.text`, `page` (PDF), `field_path` (`form:<field_id or label_norm>`, `table:<t>:<r>:<c>`, `para:<i>`, `pdf:<page>:<line>`).
Document kinds: `disclosure_body`, `attachment`, `kap_pdf`.

## C2. Issuer master & identifiers (KAP produces; everyone reads)

KAP company sync creates per listed company: `issuer` (legal_name, short_name, kind `company`, `extra.kap` = raw), `instrument` (type `equity`, currency TRY), identifiers: instrument `TICKER` (value upper-case, valid_from `OPEN_START` or change date), issuer `KAP_MEMBER_OID`, issuer `KAP_COMPANY_OID` if present, instrument `ISIN` when known. Indices (prices slice) are `instrument` type `index` with `TICKER` like `XU100` and no issuer.
Resolution helper for humans' input lives in Portfolio (C3 `resolve_instrument`); KAP resolves disclosures itself.

## C3. Portfolio service — `papi.portfolio.service`

```python
@dataclass class InstrumentMatch: instrument_id: str; ticker: str; issuer_name: str | None; score: float
@dataclass class Position: instrument_id: str; ticker: str; quantity: Decimal; avg_cost: Decimal | None;
                           cost_total: Decimal; last_close: Decimal | None; last_close_date: date | None;
                           market_value: Decimal | None; weight: float | None
class ProposedTxn(BaseModel):  # pydantic
    portfolio_id: str | None = None; instrument_id: str | None = None; ticker: str | None = None
    type: Literal["buy","sell","dividend_cash","bonus_shares","rights_subscription","rights_sale","split",
                  "fee","tax","transfer_in","transfer_out","opening_balance","reconcile_adjustment"]
    quantity: Decimal; price: Decimal | None = None; fees: Decimal = 0; trade_date: date
    traded_at: datetime | None = None; source: str = "manual"; chat_message_id: str | None = None
    note: str | None = None; warnings: list[str] = []

def default_portfolio_id(conn) -> str
def resolve_instrument(conn, query: str, at: date | None = None, limit: int = 5) -> list[InstrumentMatch]
def ticker_for(conn, instrument_id: str, at: date | None = None) -> str | None
def validate_proposal(conn, p: ProposedTxn) -> ProposedTxn          # resolves ticker→instrument, fills warnings
def record_transaction(conn, p: ProposedTxn, *, dedupe_key: str | None = None) -> str   # txn id; publishes portfolio.changed.v1
def reverse_transaction(conn, txn_id: str, *, reason: str | None = None) -> str
def positions(conn, portfolio_id: str | None = None, as_of: date | None = None) -> list[Position]
def holding_priority(conn, instrument_ids: list[str], at: datetime | None = None) -> Literal["P1","P2"] | None
def watch(conn, ref: str, watchlist: str = "Ana") -> str ; def unwatch(conn, ref: str, watchlist: str = "Ana") -> bool
def reconcile(conn, observed: dict[str, Decimal], portfolio_id: str | None = None) -> list[ReconcileDiff]
def apply_reconcile(conn, diffs: list[ReconcileDiff]) -> list[str]
def pending_actions(conn, status: str = "expected") -> list[dict]
def apply_pending_action(conn, pending_action_id: str, *, quantity_override: Decimal | None = None) -> str
def dismiss_pending_action(conn, pending_action_id: str) -> None
def propose_for_corporate_action(conn, corporate_action_id: str) -> list[str]   # pending_action ids
```
`conn` = an open SQLAlchemy connection inside a transaction; functions do not commit. Valuation uses latest `price_bar.close` (interval `1d`) at or before `as_of`, if any.

## C4. Notify — `papi.notify`

```python
# papi.notify.ops
def ops_alert(conn, text: str, *, level: str = "warning", dedupe_key: str | None = None) -> None
    # enqueues job kind "notify:ops" (sent to Telegram/macOS by the notify slice); dedupe_key suppresses repeats for 1 h
```
Alert-rule DSL: `papi.notify.rules.parse(text) -> Rule`, `Rule.matches(ctx) -> bool` (08 §3.1). Notify seeds the default alert rules (idempotent) via `papi telegram setup` and on first worker start.

## C5. Triage outputs (Triage produces; Notify, LLM, Web consume)

For each `disclosure.normalized` / `disclosure.revised`: ≥1 `event` row with `code`, `taxonomy_version`, `issuer_ids`, `instrument_ids`, `announced_at`, `priority` (`P1`–`P4`), `materiality` 0–100, `materiality_band`, `payload` (deterministic form-field payload or null), `payload_source` (`form_fields` or null), `status` `provisional`, and `extra = {"headline_tr": str, "tickers": [str], "rules": {...why}}`. Then publish `event.classified.v1` (data: event_id, code, materiality_band, priority, disclosure_id) with job priority = priority label.
Revisions: previous events of the revised disclosure get `superseded_by`.
`papi.triage.taxonomy.load() -> Taxonomy` (codes, families, labels TR/EN, payload schema refs).

## C6. LLM backend & governor — `papi.llm`

```python
@dataclass class LlmResult: structured_output: dict | None; text: str | None; model_ids: list[str];
    cost_usd_est: float; duration_ms: int; exit_code: int; raw_json: dict | None;
    error_kind: Literal[None,"limit","schema","timeout","auth","error"]; error: str | None
# papi.llm.backend
def get_backend() -> LlmBackend
class LlmBackend(Protocol):
    def complete(self, *, task: str, prompt: str, system_prompt: str, schema: dict | None,
                 model: str | None = None, max_turns: int | None = None, timeout_s: float | None = None,
                 mcp_config: dict | None = None, allowed_tools: list[str] | None = None) -> LlmResult
# papi.llm.governor
@dataclass class Admission: ok: bool; retry_at: datetime | None; reason: str
def admit(conn, task: str, priority: str) -> Admission      # priority: "CHAT"|"P1".."P4"|"BACKLOG"
def record_usage(conn, result: LlmResult, *, task: str, priority: str, analysis_id: str | None = None) -> None
def pause(conn, until: datetime | None = None, reason: str = "manual") -> None
def resume(conn) -> None
def status(conn) -> dict   # keys: paused, paused_until, used_5h, used_week, cap_5h, cap_week, cap_chat_5h, by_task, queue
# papi.llm.call
def call(conn, *, task: str, priority: str, prompt: str, system_prompt: str, schema: dict | None, **kw) -> LlmResult
    # admit → complete → record_usage; raises papi.core.jobs.Deferred(retry_at) when not admitted or on limit
```

## C7. Prices — `papi.sources.prices`

```python
def last_close(conn, instrument_id: str, on_or_before: date | None = None) -> tuple[date, float] | None
def sync_prices(conn, instrument_ids: list[str], start: date, end: date | None = None) -> dict
```
L1 calculators in `papi.sim.calculators` (pure functions, 05 §2).

## C8. KB — `papi.kb`

`lookup(conn, citation_id, as_of) -> ProvisionVersion | None`, `search(conn, query, as_of=None, k=5, filters=None) -> list[ProvisionHit]`, `obligations(conn, event_code, as_of=None, approved_only=True) -> list[Obligation]`, `timeline(conn, event_code, as_of=None) -> list[ExpectedStep]`, `current_snapshot(conn) -> str | None`.

## C9. Misc conventions

- Ops/health problems: call `papi.notify.ops.ops_alert`. Metrics: `papi.core.metrics.record`.
- KAP links: `https://www.kap.org.tr/tr/Bildirim/<source_ref>`.
- All user-facing text TR first.
- Network in tests: forbidden (use respx/recorded fixtures). Live calls only in explicit CLI commands / smoke steps, at low volume, honest User-Agent.
