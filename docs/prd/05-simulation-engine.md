# 05 — Simulation Engine

> Parent: [PRD.md](../../PRD.md) · Called from: [04 §2](04-agents.md#2-pipeline) · Portfolio: [12](12-portfolio.md) · Contracts: [06 §6](06-data-model.md#6-market--simulation-entities) · Quality: [11](11-evaluation-and-quality.md)

## 1. What "simulate" means

| Layer | Question | Nature | Milestone |
|---|---|---|---|
| **L1 Mechanical** | What *must* happen by rule? | Deterministic calculators | M3 |
| **L2 Regulatory timeline** | What steps follow, and roughly when? | KB obligations + historical step durations | M3 |
| **L3 Statistical reaction** | How did similar events move price/volume, and what range now? | Event study + calibrated quantile model | M3 |
| **L4 Position & portfolio impact** | What does this mean for *my* shares and TRY value? | L1–L3 applied to holdings | M4 |
| L5 Market simulation | Order-flow / contagion modelling | Research | Not planned |

Claude never produces L1–L4 numbers; it only narrates them ([04 §4.2](04-agents.md#42-explain-p1-only)).

## 2. L1 — Mechanical calculators

Pure, unit-tested, versioned (`formula_id@version`) Python functions, cited in claims.

| Calculator | Formula / logic (TR-pack conventions configurable) |
|---|---|
| Bonus issue (bedelsiz) theoretical price | `P_ex = P_cum / (1 + b)`, `b` = bonus shares per existing share |
| Rights issue (bedelli) theoretical price | `P_ex = (P_cum + r·S) / (1 + r)`, `r` = new shares per existing, `S` = subscription price |
| Combined bedelli + bedelsiz | Per BIST adjustment convention (pack rule) |
| Cash dividend | `P_ex = P_cum − D_gross`; net yield after withholding shown separately |
| Dilution | Ownership %, EPS dilution from new share count |
| **My share count after action** | Bonus: `n·(1+b)`; rights: `n·(1+r)` if I subscribe (cash needed `n·r·S`) or rights value if I don't |
| Buyback capacity | Programme amount / price → shares, % of free float, days of ADV |
| Tender-offer floor | Reference price per KB rule vs. current price → spread |
| Contract materiality | `value_TRY / TTM revenue`, `/ market cap` (FX at publish-date rate; VAT explicit) |
| Price-limit path | Sessions needed to reach a level under daily limits (limit % per segment from pack config) |

## 3. L2 — Regulatory timeline

- `kb.timeline(event_code, as_of)` gives the ordered expected steps.
- Step-duration distributions (median, P10–P90 days) come from papi's own event history, conditioned by segment/issuer where there is enough data.
- Open timelines are tracked per issuer. When a later disclosure shows the next step, the timeline advances and the "time-to-step" estimate is scored.
- Holdings with open timelines appear on the portfolio page ("XYZ bedelli: waiting for SPK approval, typical 18–80 days, day 23 now").

## 4. L3 — Statistical reaction model

### 4.1 Data

- Daily OHLCV from the public price adapters ([01 §3.3](01-data-sources.md#33-price-history-index-levels-financial-statements)), cross-checked; adjusted series computed by papi from raw closes + KAP corporate actions.
- Event set: **all historical KAP disclosures with rules-derived event codes** (cheap, full coverage). Payload-conditioned features (e.g. contract size) use deterministic form-field payloads where available, and Claude-extracted payloads accumulate slowly via the off-hours backlog ([04 §4.4](04-agents.md#44-off-hours-backlog-optional)). Models only use features with adequate coverage.

### 4.2 Targets

| Target | Definition |
|---|---|
| `CAR(t0, h)` | Cumulative abnormal return vs. market model (XU100 or sector index; β from a clean pre-window), `h ∈ {T+1, T+5, T+20}` sessions; `t0` = first session where the news was tradable (calendar-aware for after-close publication) |
| Abnormal volume | Volume / median pre-window volume |
| Limit-hit probability | P(close at upper/lower daily limit on T+1) |

Views: TRY, benchmark-relative, USD and inflation-adjusted, because nominal TRY returns are dominated by inflation/FX.

### 4.3 Features (point-in-time only)

Event code; payload values where available; L1 ratios; market-cap band, liquidity, free float, sector, index membership, segment, active market measures; pre-event run-up (leakage signal) and volatility; novelty (first vs. repeat); time of publication vs. session; late/correction flags; market regime (index volatility, proximity to TCMB decisions); validated categorical outputs from the analyst (e.g. `fundamental_direction`).

### 4.4 Models

| Stage | Model |
|---|---|
| Baseline | Unconditional empirical quantiles of CAR per event family. Must be beaten; it is the fallback where it isn't. |
| v1 | Quantile gradient-boosted trees per horizon, shrunk toward family priors |
| Analogues | k-NN over features, returning concrete past events with links ("10 most similar and what happened") |
| Calibration | Conformal adjustment per family |

### 4.5 BIST pitfalls handled explicitly

| Pitfall | Handling |
|---|---|
| Daily price limits censor returns | Limit-locked days treated as censored; multi-day path; limit-hit probability reported separately |
| Market measures change microstructure | Measure state is a feature; modelled separately when present |
| Inflation / TRY moves | Benchmark-relative, USD and real views |
| Corporate actions distort raw prices | Own adjustment factors from KAP; raw prices kept |
| Pre-announcement leakage | Run-up features; "already priced in" diagnostic |
| Thin liquidity | Wider intervals; abnormal volume emphasised |
| Several disclosures on the same day | Confounding flag; no multi-event attribution in v1 |
| Survivorship | Delisted instruments kept |

### 4.6 Point-in-time discipline

```mermaid
flowchart LR
  E[Event at t] --> Q[Features as_of t<br/>recorded_at ≤ t]
  Q --> M[Model trained on events < t − embargo]
  M --> F[Forecast frozen + hashed]
  F --> W[T+1 / T+5 / T+20]
  W --> O[Outcome + scores]
```

- Walk-forward training with an embargo; the backtest harness asserts that no feature has `recorded_at > as_of`.
- Forecasts are immutable (`frozen_hash`); new model versions create new forecasts.
- The backtest replays history through the same code path as live.

## 5. L4 — Position & portfolio impact

| Output | Method |
|---|---|
| Position mechanics | L1 applied to my lot(s): new share count, cash needed for rights, dividend cash (gross/net), adjusted cost basis |
| Position value range | Current position value × (1 + quantiles of total expected return = market-model expected + CAR quantiles), for T+1/T+5/T+20. Shown as TRY range plus % of portfolio. |
| Portfolio view | Sum of affected positions. Joint ranges for several simultaneous events use **historical bootstrap of joint daily returns** of my holdings, not by adding quantiles (quantiles don't add). |
| Concentration context | Position weight, sector weight, and how much of the portfolio has open regulatory timelines |

Everything is labelled as ranges from historical evidence. Decisions stay with me.

## 6. Scenario output contract

```json
{
  "event_id": "…",
  "instrument_id": "…",
  "as_of": "2026-10-28T07:10:35Z",
  "mechanical": [
    {"formula_id": "rights_issue_terp@1", "inputs": {"P_cum": 42.10, "r": 1.0, "S": 1.0}, "result": {"P_ex": 21.55}}
  ],
  "timeline": [
    {"step": "spk_approval", "expected_days_p50": 35, "p10": 18, "p90": 80, "based_on_n": 214}
  ],
  "reaction": {
    "T+1": {"car_q": {"p10": -0.041, "p50": 0.012, "p90": 0.078}, "p_limit_up": 0.18, "abn_volume_p50": 3.4},
    "T+5": {"car_q": {"p10": -0.083, "p50": 0.010, "p90": 0.112}}
  },
  "baseline": {"T+1": {"car_q": {"p10": -0.050, "p50": 0.006, "p90": 0.070}}},
  "analogues": [{"event_id": "…", "date": "2024-05-14", "car_T+5": 0.064, "similarity": 0.91}],
  "position": {
    "shares_before": 1200, "shares_after": 2400, "cash_needed_try": 1200.0,
    "value_range_try": {"T+5": {"p10": 46100, "p50": 50900, "p90": 56300}},
    "portfolio_weight": 0.14
  },
  "model": "car_qgbt@2026.11.1",
  "frozen_hash": "sha256:…"
}
```

All numbers are illustrative.

## 7. Outcome tracking & private track record

| ID | Requirement |
|---|---|
| SIM-O1 | After each session close, fill `outcome` for every forecast whose horizon matured |
| SIM-O2 | Scores: pinball loss per quantile, CRPS, p10–p90 coverage (~80 % expected), directional hit vs. baseline, limit-hit Brier |
| SIM-O3 | Track-record page per family/horizon/model version, including worst misses ([07](07-dashboard.md)) |
| SIM-O4 | Auto-fallback to baseline for (family × horizon) cells where the model under-performs over a rolling window |

## 8. Acceptance criteria

- [ ] Every L1 calculator has property-based tests and worked examples from real historical disclosures.
- [ ] Walk-forward backtest ≥ 3 years with zero knowledge-time violations.
- [ ] Model beats baseline on pinball loss for ≥ 70 % of cells with enough data; others fall back to baseline.
- [ ] p10–p90 coverage within ±5 pp of 80 % out of sample.
- [ ] Forecast rows cannot be updated (enforced in the repository layer + DB trigger).
- [ ] Position mechanics match the share count İşCep showed after at least one real bonus or rights issue in my history.
