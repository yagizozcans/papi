---
name: kap-disclosure-analysis
description: Analyse one KAP (Borsa Istanbul) disclosure end to end — read its full text and attachments, map it to event codes and payload schemas, judge materiality against the issuer and the user's position, cite every claim. Use for any "analyse KAP disclosure <number>" request.
allowed-tools:
  - kap_get_disclosure
  - kap_read_document
  - kap_event_schema
  - kap_issuer_context
  - portfolio_position
  - kb_search
---

# KAP disclosure analysis

Work in this order; each step's result decides the next one.

1. `kap_get_disclosure(ref)` — subject, documents, and the events rules triage already derived. The
   rules payload (`payload_source = form_fields`) is authoritative: never change those values, only
   fill fields it left null from the text.
2. `kap_read_document(ref, document=i)` for every listed document, following `next_offset` until it is
   null. Body first, then each attachment. Do not analyse before the whole text has been read.
3. `kap_event_schema(code=...)` for every event code you will report, to get the nested payload shape
   and field notes. If the rules code looks wrong, search the taxonomy with `kap_event_schema(query=...)`
   and report the right code (see references/event-codes.md for how to decide).
4. `kap_issuer_context(ref)` — scale of the issuer (market cap, last close) and its recent events; needed
   for materiality. Skip when the disclosure has no issuer.
5. `portfolio_position(ticker)` only when the issuer has a ticker; mention the position only if held.
6. `kb_search(query)` only when the disclosure creates a regulatory obligation or deadline worth citing
   (e.g. buyback notification duty). Cite its `citation_id`; never cite what you did not retrieve.
7. Answer: one JSON object following the output schema. See references/materiality.md for the 0–100
   scale, references/claims.md for how to write claims and references/plain-turkish.md for the writing
   style (plain everyday Turkish; jargon glossary; how to fill `positives` / `negatives`).

Stop calling tools as soon as you can answer. Do not call a tool twice with the same arguments.
