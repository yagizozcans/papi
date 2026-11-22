# 14 — Frontend (papi terminal)

Status: **draft spec + plan**, 2026-11-22. Owner decisions recorded below; open questions at the end.

## 1. Goals

- A new React dashboard that runs **next to** the existing Jinja/htmx dashboard (served at `/app`; the old
  pages stay at `/` until the new one covers them, then they may be retired).
- **Everything papi can do is possible in the UI** (full CLI parity, §5), not only reading.
- Designed as a product, not a port of the old pages: new information architecture (§3).
- Works on the owner's phone (responsive + installable PWA).
- Leaves room for the paused roadmap: M3 simulation / event study, M4 portfolio intelligence, M5 sources.
- Primary user is the owner; anyone who clones the repo can run it too.

## 2. Decisions

| Topic | Decision |
|---|---|
| Framework | React 19 + TypeScript + Vite, in `frontend/` |
| Components | shadcn/ui (Radix primitives + Tailwind v4), code owned in repo |
| Data | TanStack Query (polling, `refetchInterval` 3–5 s on live views) |
| Tables | TanStack Table (+ virtualisation for long lists) |
| Charts | TradingView Lightweight Charts (price/candles + KAP event markers; attribution link in footer), Recharts via shadcn charts for small stats |
| Routing | TanStack Router (typed routes, search-param state for filters) |
| Forms | react-hook-form + zod |
| Command palette | cmdk (⌘K) |
| i18n | react-i18next, Turkish + English (TR default); disclosure content stays Turkish |
| API types | generated from FastAPI OpenAPI (`openapi-typescript`), checked in CI |
| Style | Mix of terminal + dashboard; density between terminal and Stripe |
| Theme | Dark and light toggle; **always starts dark** (choice remembered per browser) |
| Live updates | Short polling (a few seconds of delay is fine); no SSE/WebSocket |
| Logo / accent | Cici CV mark (`#2563EB` blue) as papi logo and accent colour |
| Serving | `npm run build` → `src/papi/web/ui/` (git-ignored); FastAPI serves it at `/app` with SPA fallback. If the bundle is missing, `/app` shows build instructions; the old dashboard keeps working without Node |
| Phone access | Home Wi-Fi: bind to the LAN address with the existing token auth for non-loopback binds (`papi/web/app.py`) |
| URL | `/app` during P0–P1; the new UI **takes over `/`** once P1 is done (old pages move to `/legacy`) |

## 3. Information architecture

Global shell: left sidebar (collapses to bottom tab bar on mobile), top bar with ⌘K search/commands,
TR/EN and theme toggles, and a terminal-style **status bar** at the bottom (KAP last poll and lag,
queue sizes, LLM governor state, failed jobs, backup age). Clicking a status item opens its System page.

1. **Bugün (Today)**: home. Market strip (XU100, USDTRY, portfolio day P/L); "needs attention" inbox
   (unread P1 disclosures, pending corporate actions, alerts, failed jobs, analyses waiting); live
   disclosure tape; running agent runs.
2. **Akış (Feed)**: terminal-style disclosure tape with filters (scope, priority, date, ticker, event code)
   and a **split view**: list left, reader right (j/k keys). The reader shows original text and
   attachments, parsed fields, triage reasoning, analysis/claims/citations, "Run agent" and feedback.
3. **Portföy (Portfolio)**: holdings table (weights, P/L, day change), allocation, performance chart
   with KAP event markers, transaction ledger with add/undo, CSV import with preview, İşCep reconcile
   wizard, pending corporate actions, watchlists.
4. **Enstrüman (Instrument / company)**: `/i/<TICKER>`. Price chart with disclosure markers, disclosures,
   share capital, positions, theses and agent runs on it. Reserved tabs: Financials, Event impact (M3),
   Risk (M4), shown as "coming soon".
5. **Araştırma (Research)**: theses (upload, edit, check, review results), agents (run, run history with
   live tool calls and cost, tool/skill catalogue), KB (search, obligation review/approve, publish).
6. **Lab** (reserved): simulation, event study, Monte Carlo/VaR, backtests. Behind a feature flag until
   M3/M4 exist.
7. **Sistem (System)**: health (lanes, jobs, gaps, latency), LLM usage and governor (pause/resume,
   calibration), data sources (sync companies, fetch/backfill, prices sync, add fund, cross-check),
   alerts and Telegram (setup, test, rules), settings (`config.toml` editor with validation), backups
   and restore, evals, database (migrate status).

Mobile: bottom tabs Bugün · Akış · Portföy · Araştırma · Daha fazla; split views stack; tables become
cards for key columns.

## 4. Visual design

- **Tokens** as CSS variables (light + dark): neutral zinc greys, accent `#2563EB`, semantic up/down
  (green/red, with a colour-blind safe alternative), priority colours P1/P2/P3, warning/error.
- **Type**: Inter (UI), JetBrains Mono for numbers, tickers, codes and the tape; tabular numerals
  everywhere numbers align.
- **Density**: 13 px base text, 32 px table rows, 8 px grid; compact toggle for terminal-dense tables.
- **Motion**: minimal; new tape rows flash once.
- Keyboard first: ⌘K, `g` then a letter for navigation, j/k in lists, `?` shows shortcuts.

## 5. CLI parity (backend work)

Each CLI command gets a `/v1` endpoint used by the UI. Long-running commands (backfill, kb fetch/parse,
prices sync, run agent) are **enqueued as jobs** in the existing job system; the UI shows progress.
Destructive ones (undo, restore, approve-all) need a confirm dialog.

| CLI group | Commands → UI |
|---|---|
| portfolio | add, list, txns, undo, import, template, reconcile, pending, apply, dismiss, watch, unwatch, watchlists → Portföy |
| kap | poll-once, sync-companies, capital, fetch, backfill, reparse, show, stats → Sistem/Data sources, reader |
| prices | sync, add-fund, show, check → Sistem/Data sources, Instrument |
| kb | fetch, parse, status, show, search, review, approve-obligation, publish → Araştırma/KB |
| triage | classify, stats, subjects → reader + Sistem |
| llm | status, pause, resume, check, test, calibrate → Sistem/LLM |
| telegram | setup, test, send, poll-once, rules → Sistem/Alerts |
| run | status, backup-now, backups, restore, service → Sistem |
| eval | review, snapshot, kb-build → Sistem/Evals |
| analyst | run, show, verify, pending → reader |
| agent | list, tools, run → Araştırma/Agents |
| thesis | new, import, check, list → Araştırma/Theses |
| db, config | migrate/current, show/path → Sistem/Settings |

Excluded from the UI: `init` (must run before the server exists) and `run start` (it is the server).
Secrets (Telegram token) are written to the Keychain by the backend, never returned to the UI.

## 6. Plan

| Phase | Scope | Check |
|---|---|---|
| P0 Foundation | `frontend/` scaffold, tokens, shell (sidebar, ⌘K, status bar, theme, i18n), OpenAPI type generation, FastAPI `/app` serving, CI (typecheck, lint, vitest, build) | `/app` loads in both themes and languages; CI green |
| P1 Read | Today, Feed + reader, Instrument, Portfolio (read), status bar data; missing read endpoints | Each page renders real `~/.papi` data; API tests |
| P2 Act | trades/import/reconcile/pending/watchlists, agent runs, theses, LLM controls, alert rules | Every action covered by an API test + one UI test |
| P3 System | health, data-source jobs, KB review, settings editor, backups/restore, evals, Telegram setup | CLI parity table §5 fully ticked |
| P4 Mobile | PWA manifest/icons, mobile layouts, phone access doc (home Wi-Fi + token) | Usable on an iPhone on home Wi-Fi |
| Later | Lab pages as M3/M4 land; decide whether to retire Jinja pages | — |

Backend API slices and frontend pages can be built in parallel once the endpoint contract for a phase
is fixed.

## 7. Resolved questions (2026-11-22)

- Default theme: always dark on first load.
- URL: new UI takes over `/` after P1.
- Phone access: home Wi-Fi with the existing token (no Tailscale).
