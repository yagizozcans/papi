# Choosing event codes

Rules triage maps the KAP subject and form to taxonomy codes. Keep them when the text agrees.

- `other.unclassified` means rules triage found no template match: search the taxonomy with
  `kap_event_schema(query="<keyword>")` and report the best-fitting code with confidence ≥ 0.6, else
  keep `other.unclassified` and say why in `materiality_rationale`.
- A rules code you believe is wrong: report the right code and explain in `materiality_rationale`; do
  not copy the rules payload under the wrong code.
- Payload for each reported code follows `kap_event_schema(code=...)`: nested object, no dots in keys,
  null for unknown, rules-derived values unchanged. `payload_spans` maps each filled dotted field path
  (e.g. `counterparty.name`) to the span numbers where its value was read.
- Trading-ban or suspension notices that SPK/BIST publish about a company are about that company only;
  do not report them under other tickers.
