# papi run — the long-running process (PRD 09 §1, §4–§6; 02 §3.1)

```bash
papi run [--no-web] [--host H] [--port P]   # same as `papi run start`; Ctrl-C / SIGTERM stops cleanly
papi run status [--json]                    # lock, heartbeat/last scheduler tick, lanes, jobs, KAP, governor, Telegram
papi run backup-now | backups               # SQLite online backup now / list backups
papi run restore --from papi-20261027.db    # or a path or 20261027; refused while papi run is running
papi run service install [--uv] | uninstall | status | logs [-n 40] [-f]
```

## What runs in the process

- Startup: logging → single-instance lock (`$PAPI_HOME/locks/run.lock`, `flock`) → `migrate()` →
  `load_plugins()` → Claude environment check (problems are logged and sent as an ops alert; papi
  refuses to start only when `ANTHROPIC_API_KEY` is set) → jobs leased by a dead earlier process on
  this host are re-queued at once.
- Scheduler (`schedule_due` every `scheduler_tick_s`) and worker lanes. Each registered job kind is on
  exactly one lane:
  - `telegram`: `periodic:telegram:poll` (long poll), ×1
  - `llm`: kinds matching `llm_kind_prefixes` or handlers in `llm_modules` (analyst/checker), ×`llm.concurrency`
  - `io`: KAP (`io_kind_prefixes`), ×`io_concurrency`
  - `general`: everything else, ×`general_concurrency`
- Web dashboard (uvicorn, same event loop) on `web.host:web.port` unless `--no-web`: the React UI at `/`,
  the old dashboard at `/legacy/`, the JSON API at `/v1`. If the port is
  taken papi keeps running without web and sends an ops alert.
- Monitor every `watchdog_interval_s`: writes the `run:heartbeat` kv row; detects sleep/wake
  (the wall clock moved more than `wake_jump_s` ahead of the monotonic clock). On wake it records
  metric `run.wake`, queues an immediate `periodic:kap:poll` (poll_once then lists the missed window in
  catch-up mode) and drops the Telegram client so the next long poll reconnects.
- Optional `caffeinate -s -w <pid>` while the XIST session is open (`trading_calendar`, else weekdays
  `session_start`–`session_end` Istanbul) if `prevent_sleep_on_ac = true`.
- Shutdown: stop leasing, wait up to `shutdown_grace_s` for running jobs (a second signal skips the
  wait), then hand the remaining leases back (`jobs.defer`, attempt refunded) so they re-run on the
  next start.

## Backups

- `periodic:run:backup` checks every 15 min. Once the Istanbul clock passes `backup_hour`, it writes today's
  `$PAPI_HOME/backups/papi-YYYYMMDD.db` with the SQLite online backup API. The copy passes `quick_check`
  and is a single-file journal. If the Mac slept through that hour, the backup is written on wake.
- Retention: the `backup.keep_daily` newest days, plus the newest file of each of the
  `backup.keep_weekly` newest ISO weeks. Metric `run.backup.ok` and kv row `run:backup:last` are
  recorded; a failure sends an ops alert.
- `restore` validates the file (SQLite, `quick_check`, `alembic_version`), takes the instance lock,
  copies the current DB to `backups/pre-restore-<UTC>.db`, then restores. The next `papi run`
  migrates the restored DB if it is older.
- `raw/` is not copied. It is write-once, content-addressed files, so Time Machine (or an rsync to an
  external disk) covers it completely. A restored DB can only point to raw objects that still exist.

## launchd

`service install` writes `~/Library/LaunchAgents/com.papi.run.plist` (`PAPI_LAUNCH_AGENTS_DIR`
overrides the directory) and loads it with `launchctl bootstrap gui/<uid>`. The plist sets RunAtLoad,
KeepAlive, ExitTimeOut 40 s and ThrottleInterval 10 s. Its ProgramArguments are the absolute path of the current
`papi` executable plus `run`, or `uv run --project <repo> papi run` with `--uv`. Symlinks such as
Homebrew's `/opt/homebrew/bin/uv` are kept as they are (not resolved into a versioned `Cellar/…`
directory that the next `brew upgrade` removes). EnvironmentVariables
holds `PAPI_HOME` and the installing shell's `PATH` (so `claude` is found). stdout/stderr go to
`$PAPI_HOME/logs/launchd.{out,err}.log`.

## `[run]` config (all optional)

| key | default |
|---|---|
| `general_concurrency` / `io_concurrency` | 2 / 1 |
| `io_kind_prefixes` | `["kap:", "periodic:kap:"]` |
| `llm_kind_prefixes` | `["event:analyst", "analyst:", "periodic:analyst", "event:verify", "verify:"]` |
| `llm_modules` | `["papi.analyst", "papi.verify"]` |
| `telegram_kinds` | `["periodic:telegram:poll"]` |
| `scheduler_tick_s` / `watchdog_interval_s` / `wake_jump_s` / `shutdown_grace_s` | 1.0 / 5.0 / 120 / 20 |
| `prevent_sleep_on_ac`, `session_start`, `session_end` | false, `"09:30"`, `"18:30"` |
| `backup_hour` | 3 (Istanbul) |
