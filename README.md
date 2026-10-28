# papi

Personal, local BIST/KAP disclosure-intelligence tool. Spec: [PRD.md](PRD.md) and [docs/prd/](docs/prd/).
One Python process, one SQLite file, one directory of raw files, all under `$PAPI_HOME` (default `~/.papi`).

## Quickstart (owner setup on macOS)

Everything lives under `~/.papi` (override with `PAPI_HOME=/path`). Run the commands from the repo
directory; `uv run papi …` works without activating the venv (shown as `papi …` below).

**1. Install and initialise**

```bash
uv sync                          # Python 3.12 venv + deps
uv run papi init                 # ~/.papi: config.toml, papi.db (migrated + seeded), raw/, logs/, backups/
uv run papi config show          # effective config; secrets reported as set/unset
```

Edit `~/.papi/config.toml`: at least `[kap] contact = "<your e-mail>"` (sent in the KAP User-Agent).
Optional focus windows (e.g. weekdays 09:00–18:00 analyse P1 only): uncomment the `[[llm.focus]]`
example under `[llm.models]`.

**2. Companies and the legal knowledge base**

```bash
papi kap sync-companies          # KAP listed companies → issuers/tickers (~1 s)
papi kb fetch                    # official legal texts (sequential, ~1 req/s; ~45 s)
papi kb parse                    # structure into articles/paragraphs (~1.5 min)
papi kb publish --note "ilk yükleme"
papi kb review                   # low-confidence structure + obligations awaiting approval
papi kb approve-obligation --all-for business.contract.award      # repeat per event code you checked
papi kb approve-obligation <obligation-id>                         # or one at a time
```

Only approved obligations are cited by the analyst; review them before approving.

**3. Portfolio and watchlist**

```bash
papi portfolio template > portfoy.csv    # header + example rows; fill it from İşCep (Portföyüm)
papi portfolio import portfoy.csv --dry-run
papi portfolio import portfoy.csv        # preview + confirm; re-importing the same rows is a no-op
papi portfolio add BUY CEMAS 1000 @ 3,10 2026-10-15 --fees 1,5   # single trades from the CLI
papi portfolio watch SDTTR               # watchlist (also XU030 / endeks:XU030 / sektor:<kod>)
papi portfolio list                      # positions, cost, last close, weight
papi prices sync --tickers MEKAG,FROTO,CEMAS,XU100   # first price history (default: last 30 days)
papi kap capital --tickers MEKAG,FROTO,CEMAS         # share capital of held/watched companies
```

Use opening balances (`opening_balance`) for what you already hold. Holdings make their disclosures
P1 and watchlist names P2: they are fetched, alerted and analysed first.

**4. Telegram bot** (alerts, trade recording, commands)

1. In Telegram open **@BotFather** → `/newbot` → pick a name and a username ending in `bot` → copy the
   token.
2. `papi telegram setup` — paste the token when asked (stored in the macOS Keychain, service `papi`,
   never in `config.toml`), then send any message to your bot within 5 minutes; papi records your user
   and chat id, enables Telegram and installs the default alert rules.
3. `papi telegram test` — sends a test message.

Without Telegram papi still runs: alerts appear on the dashboard (and, if
`alerts.macos_notifications = true`, as macOS notifications).

**5. Claude (analysis) and the calibration week**

```bash
papi llm check                   # Claude Code CLI found, logged in, no ANTHROPIC_API_KEY, sandbox ok
papi llm status                  # papi's usage vs caps, pause state, focus window, queue
papi llm calibrate --start       # day 1: start the calibration marker
```

papi uses your Claude **Max 5x** subscription through the `claude` CLI and starts with the Max 5x
profile: during week 1 (`llm.calibration_mode = true`) only P1/P2 events are analysed. Note how much
the claude.ai → Settings → Usage bars moved because of papi during the week, then:

```bash
papi llm calibrate --bar-5h-pct 4 --bar-week-pct 3 --dry-run   # show the computed caps
papi llm calibrate --bar-5h-pct 4 --bar-week-pct 3             # write them; calibration mode off
```

This sets `share_5h` to 15 % of the 5-hour bar and `share_week` to 20 % of the weekly bar (the Max 5x
profile, PRD 04 §6) and enables P3 analysis. Night backlog analysis stays off until you set
`llm.backlog_enabled = true`. `papi llm pause` / `papi llm resume` stop and restart all Claude calls
(rule-based alerts continue).

**6. Start papi**

```bash
papi run                         # foreground; Ctrl-C stops cleanly (running jobs are handed back)
papi run service install --uv    # or: launchd LaunchAgent com.papi.run (starts at login, restarts on crash)
papi run service status          # launchd state; `papi run service logs` / `uninstall`
```

Dashboard: <http://127.0.0.1:8765> (feed `/`, `/portfolio`, `/company/<TICKER>`, `/events/<id>`,
`/ops`, `/ops/usage`). Details of the process, lanes, backups and launchd:
[src/papi/run/README.md](src/papi/run/README.md).

On the first start papi lists the last two days of KAP and works through that backlog (holdings and
watchlist first; new live disclosures always go ahead of the backlog). Expect about 30 minutes for
~700 disclosures at KAP's polite one-request-per-second pace.

**7. Daily use (Telegram)**

- Record trades in plain Turkish: `THYAO 100 lot 285,40'tan aldım`, `ASELS'in yarısını 62'den sattım`
  (papi asks for ✅ before writing anything); `/geri` undoes the last one.
- `/portfoy` positions · `/bugun` today's disclosures for your holdings + watchlist · `/bekleyen`
  pending corporate actions (dividends, bonus/rights issues) to confirm
- `/mutabakat` step-by-step reconciliation against İşCep quantities (CLI: `papi portfolio reconcile`)
- `/durdur 3s` pauses Claude analysis (e.g. for 3 hours; no argument = until `/devam`) · `/kullanim` usage
- A daily digest arrives every morning and a weekly review on Sunday evening. `/yardim` lists everything.

**8. Backups and restore**

`papi run` writes `~/.papi/backups/papi-YYYYMMDD.db` every night after 03:00 (14 daily + 8 weekly
kept). `raw/` is write-once and covered by Time Machine.

```bash
papi run backup-now              # back up now
papi run backups                 # list backups
papi run restore --from 20261028 # stop papi run first; the current DB is saved as backups/pre-restore-<UTC>.db
```

**9. Troubleshooting**

```bash
papi run status                  # running? last scheduler tick, lanes, queue/dead jobs, last KAP poll, Claude governor, Telegram, last backup
papi kap stats                   # ingestion health: statuses, last poll, gaps, latency
papi llm status                  # Claude usage, caps, pause
tail -f ~/.papi/logs/papi.log    # structured JSON log (launchd: papi run service logs -f)
```

If KAP refuses requests papi's circuit breaker pauses KAP traffic (5 min, doubling up to 1 h) and
`papi run status` / `/ops` show it; it resumes by itself.

Secrets live in the macOS Keychain (service `papi`): `telegram_token` (or env `PAPI_TELEGRAM_TOKEN`),
optional `web_token` (or env `PAPI_WEB_TOKEN`).

## Layout

```
pyproject.toml, uv.lock, .python-version   uv project (Python >= 3.12)
alembic.ini, alembic/                      migrations (SQLite, batch mode); 0001 = full MVP schema + FTS5
contracts/                                 JSON Schemas (schemas/<name>.v<N>.json), events.md
packs/tr/                                  Türkiye pack: taxonomy, KAP subject map, legal manifest, calendar
src/papi/
  cli.py          `papi` Typer app; mounts papi.<slice>.cli:app groups when present
  core/           config, db, schema, types, ids, timeutil, text, rawstore, events, jobs, registry,
                  runtime, state, fts, metrics, logging, bootstrap
  sources/kap/    KAP capture: discovery, fetch, parse, normalize, gaps          (02)
  sources/prices/ EOD prices + cross-check                                       (01)
  sources/legal/  legal corpus fetch + structure parse                           (03)
  kb/             regulatory knowledge base                                      (03)
  triage/         rules triage: taxonomy, priority, materiality                  (04 §3)
  llm/            LlmBackend, Claude Code CLI backend, governor, prompts         (04 §4–6)
  verify/         deterministic verifier                                         (04, 11)
  sim/            L1 calculators                                                 (05)
  portfolio/      transactions, positions, CSV import, reconcile                 (12)
  notify/         alert rules, notifier, digests                                 (08 §3)
  telegram/       bot transport, trade recording                                 (08 §4)
  web/            FastAPI dashboard + /v1 API                                    (07, 08 §1)
  run/            long-running process (workers, scheduler, catch-up)            (09)
  evals/          evaluation harness                                             (11)
tests/            pytest; tests/<slice>/ per slice; conftest.py fixtures
```

## Ownership map

| Paths | Owner |
|---|---|
| `pyproject.toml`, `uv.lock`, `.python-version`, `alembic.ini`, `alembic/**`, `src/papi/__init__.py`, `src/papi/cli.py`, `src/papi/core/**`, `tests/conftest.py`, `tests/core/**`, `contracts/README.md`, `contracts/events.md`, `packs/tr/README.md`, `README.md` | foundation |
| `src/papi/<slice>/**`, `tests/<slice>/**` | that slice (`sources/kap` ↔ `tests/kap`, `sources/prices` ↔ `tests/prices`, `sources/legal` ↔ `tests/legal`) |
| `contracts/schemas/**`, `packs/tr/*` (other than README) | the slice producing the contract/pack file |

Slices extend the system without touching core:

- **CLI**: define `app = typer.Typer()` in `papi/<slice>/cli.py`; it is mounted as `papi <group>`
  (`portfolio`, `kap`, `prices`, `kb`, `triage`, `llm`, `telegram`, `web`, `run`, `eval`).
- **Events/jobs**: put `@handles(...)`, `@job_kind(...)`, `@periodic(...)` functions in
  `papi/<slice>/handlers.py`; `papi.core.registry.load_plugins()` imports them for publishers and workers.
- **Config**: unknown top-level TOML sections are kept; read them with `get_settings().section("name")`.
- **Schema**: stash extra fields in the JSON `extra` columns (issuer, instrument, disclosure, event,
  document, regulation); new columns/tables go through the foundation owner (new Alembic revision).

## Data model notes

- All tables of [06](docs/prd/06-data-model.md) §3–§8 exist; the PRD's `transaction` table is named `txn`.
- IDs are UUIDv7 strings (`papi.core.ids.uuid7`). Timestamps are UTC ISO text
  (`YYYY-MM-DDTHH:MM:SS.ffffffZ`) via `UtcDateTime`; dates are `YYYY-MM-DD`.
- Portfolio money/quantities use exact `DecimalText`; market data uses floats.
- FTS5: `disclosure_fts(id, text)` and `provision_fts(id, text)` hold `normalize_tr()` text
  (`papi.core.fts`).

## Development

```bash
uv run pytest -q                          # all tests (temp PAPI_HOME per test; never touches ~/.papi)
uv run pytest tests/core -q               # one slice
uv run ruff check src tests && uv run ruff format src tests
uv run papi db migrate                    # upgrade an existing DB
uv run alembic revision --autogenerate -m "..."   # new migration (foundation owner); review FTS/batch ops
```
