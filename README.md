# papi

Personal, local BIST/KAP disclosure-intelligence tool. Spec: [PRD.md](PRD.md) and [docs/prd/](docs/prd/).
One Python process, one SQLite file, one directory of raw files, all under `$PAPI_HOME` (default `~/.papi`).

## Quickstart

```bash
uv sync                  # Python 3.12 venv + all deps (uv fetches 3.12 if needed)
uv run papi init         # creates ~/.papi: config.toml, papi.db (migrated + seeded), raw/, logs/, ...
uv run papi init         # safe to re-run: no-op
uv run papi config show  # effective config (secrets shown as set/unset)
```

Use another data home with `PAPI_HOME=/path/to/home uv run papi init`.
Secrets go to the macOS Keychain (service `papi`): `telegram_token` (or env `PAPI_TELEGRAM_TOKEN`),
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
