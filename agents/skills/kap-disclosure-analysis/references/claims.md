# Writing claims

A claim is one checkable statement; one number or date per claim where possible.

- `fact` — what the text states. Needs ≥ 1 `span_ordinals` (the numbers N of the `[S<n>]` markers you
  read) or ≥ 1 `citation_ids` from `kb_search`. Every number and date in `text_tr`/`text_en` must appear
  in the cited span, verbatim. `value` holds the verbatim number, `unit` its unit (TL, %, lot, …).
- `computation` — a simple operation on two numbers found in spans. `formula_id` is one of `sum`,
  `difference`, `ratio`, `percent_of`, `percent_change`, `product`; cite both input spans; `value` is the
  result. The verifier recomputes it. No other arithmetic.
- `inference` — interpretation or expectation (e.g. "likely to be read as…"). No citation needed, but
  it may only use numbers present in the text or tool results, and it never gives advice.

Each sentence of `summary_*`, `headline_tr`, `materiality_rationale` and `watch_next` is checked the same
way: unverifiable sentences are dropped from what the user sees. Prefer fewer, verbatim claims.

Event codes: keep the rules code when it fits the text. Report an extra code only when the disclosure
contains a second distinct event (e.g. dividend + buyback). A code the taxonomy does not know is rejected.
