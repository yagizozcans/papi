# Materiality (0–100)

Bands used by papi: 0–29 low, 30–59 medium, 60–79 high, 80–100 critical. The rules score is a baseline
(event-type prior × size relative to market cap × novelty/lateness); yours replaces it, so move away
from it only for a stated reason.

What raises it: size relative to the issuer's market cap (a value ≥ 5 % of market cap is large, ≥ 10 %
very large); first event of its kind in 365 days; control or ownership change; going-concern, litigation
or regulatory sanctions; guidance or results far from the run-rate; dilution (rights issue, share
issuance) or a distribution that changes shareholder return.

What lowers it: routine or periodic filings that repeat prior ones; corrections of earlier disclosures
(rules apply ×0.8); small amounts (< 0.1 % of market cap); no ticker or an instrument that is not equity.

Write `materiality_rationale` in Turkish, ≤ 1200 chars: the size ratio (only from numbers you have seen
in a span or tool result), novelty, and the user's position if held. Do not state a number you did not
read.
