# papi.triage — rules triage (no LLM)

PRD [04 §3](../../../docs/prd/04-agents.md), taxonomy [06 §5.1–§5.3](../../../docs/prd/06-data-model.md),
contract C5 in `docs/dev/wave1-contracts.md`.

For every `disclosure.normalized.v1` / `disclosure.revised.v1` the handler runs `triage_disclosure()` in
one transaction:

1. **Taxonomy match** — `packs/tr/kap_subject_map.yaml` maps the KAP subject (+ class, form template,
   summary keywords, non-zero form fields/table columns) to one or more event codes of
   `packs/tr/taxonomy.yaml`. The summary is the KAP list summary, else the form's "Özet Bilgi". If no
   keyword rule fires on the summary, the head of the body text is tried. Unmapped → `other.unclassified`.
2. **Payload** — for structured KAP forms (`tbl_KP_Report_ID` dividend, `tbl_SA_Report_ID` capital
   increase, `tbl_GAI_Report_ID` buyback, `tbl_oda-10400_Shares-Transaction-Notification` /
   general-notification share transactions) fields are read from C1 `form_fields` and tables using
   `packs/tr/triage_payload.yaml`; `report.financial.periodic` takes year / period / consolidation from the
   KAP list item. The payload gets every key of its schema (`contracts/schemas/event.<code>.v1.json`,
   unknown = `null`) and is validated with `jsonschema`; failure → `payload = null`,
   `extra.payload_error = <reason>`. Per-field span ordinals go to `extra.payload_spans`.
3. **Multi-event** — several codes from the subject map (e.g. dividend + buyback, bonus + rights), plus
   derived events: an insider trade whose before/after capital (else voting) share crosses
   5/10/15/20/25/33/50/67/95 % also yields `ownership.threshold_crossing`.
4. **Materiality** (below), **priority** (below), Turkish `headline_tr`.
5. Insert `event` rows (`status=provisional`), publish `event.classified.v1` per new event with job priority =
   priority label. Re-delivery is a no-op.
6. Revisions (KAP `revision_kind`): a **correction** supersedes the events of `disclosure.revision_of`
   (same code → that event, else the first new one; also when the correction is triaged before its
   original, in which case the stale original events are not announced). An **update** (follow-up such
   as a daily buyback report linked to its programme) stands alone and supersedes nothing. **reparse /
   refresh** (`revision_of == disclosure_id`) re-classify the same disclosure and replace its events only
   when codes or payloads changed.

## Materiality (0–100)

```
score = clamp(round(prior × size × novelty × flags), 0, 100)
```

| Factor | Value |
|---|---|
| `prior` | `codes.<code>.prior` in `taxonomy.yaml`, else the family prior |
| `size` | 1.0 when no signal. **Value / market cap** (`business.contract.award`, `mna.*` `value`; `buyback.program.start` `max_fund_amount`; `buyback.execution` / `ownership.insider_trade` `total_amount`, else nominal × last close): < 0.1 % → 0.6, < 1 % → 0.85, < 5 % → 1.1, < 10 % → 1.3, ≥ 10 % → 1.5. **Dividend yield** (gross per share / last close): < 1 % → 0.8, < 3 % → 1.0, < 6 % → 1.2, ≥ 6 % → 1.4. **Rate** (bonus / rights / stock-dividend %): < 25 % → 0.9, < 100 % → 1.1, < 300 % → 1.3, ≥ 300 % → 1.5. Market cap = latest `price_bar` close (1d, on/before the announcement) × shares (`instrument.extra.shares_outstanding`, `issuer.extra.shares_outstanding` / `paid_in_capital`, else the capital payload's `current_capital`). Only TRY values are compared. |
| `novelty` | 1.15 when none of the event's issuers had an event of the same family in the 365 days before; 1.0 otherwise and for codes marked `recurring` |
| `flags` | × 1.10 if the disclosure is late (`is_late`); × 0.80 for a correction (`revision_kind = correction`, or the form's "Yapılan açıklama düzeltme mi? Evet") |
| catch-up | neutral (× 1.0); recorded in `extra.rules.materiality.catchup` |

Bands: `< 30 low`, `< 60 medium`, `< 80 high`, else `critical`. The full breakdown is stored in
`event.extra.rules.materiality`.

## Priority

1. `papi.portfolio.service.holding_priority(conn, instrument_ids)` (C3, current holdings): `P1` held,
   `P2` watched / recently sold / tracked sector or index.
2. Else `P3` when the code (or a listed prefix) is in `triage.always_tell_codes`, or materiality ≥
   `triage.p3_materiality_threshold` (default 60).
3. Else `P4`.

Past events are not re-triaged on `portfolio.changed.v1`.

## Subject map coverage

`packs/tr/kap_subject_frequencies.csv` is the subject × disclosureClass frequency table harvested from
the KAP list endpoint for 15 single-day windows between 2025-05-12 and 2026-10-27 (7 220 disclosures,
136 subject/class pairs; reporting-season days included). With the current map 99.46 % of that volume
maps to a non-`other` code (99.26 % of the KAP slice's recorded 2026-10-27/28 list fixture). Note that
≈ 42 % of the volume is routine reporting (`report.financial.periodic`, `report.attestation`,
`report.activity`). `papi triage subjects --from-csv` lists unmapped subjects.

## CLI

- `papi triage classify <disclosure_id> | --source-ref N [--json]` — dry run, nothing is written.
- `papi triage stats [--days 30]` — code distribution, `other.unclassified` share.
- `papi triage subjects [--days 90] [--from-csv]` — unmapped subjects by frequency.
