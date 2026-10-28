"""Typed settings loaded from ``$PAPI_HOME/config.toml`` (default home ``~/.papi``).

Every key has a default, so an empty or missing file is valid. Unknown top-level sections are kept
(``Settings.model_extra``) so a slice can read ``get_settings().section("mysection")`` without a core
change. Secrets never live in the file: see ``get_telegram_token`` / ``get_web_token``.
"""

from __future__ import annotations

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_HOME = "PAPI_HOME"
ENV_TELEGRAM_TOKEN = "PAPI_TELEGRAM_TOKEN"
ENV_WEB_TOKEN = "PAPI_WEB_TOKEN"
KEYRING_SERVICE = "papi"
KEYRING_TELEGRAM = "telegram_token"
KEYRING_WEB = "web_token"

REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED = Path(__file__).resolve().parents[1] / "_resources"


def resource_dir(name: str) -> Path:
    """Location of a top-level resource dir (``alembic``, ``packs``, ``contracts``).

    Prefers the repository checkout; falls back to the copy bundled in the wheel
    (``papi/_resources/<name>``) when papi is installed outside the repo.
    """
    repo = REPO_ROOT / name
    if repo.is_dir():
        return repo
    bundled = _BUNDLED / name
    if bundled.is_dir():
        return bundled
    raise FileNotFoundError(f"resource directory {name!r} not found in {REPO_ROOT} or {_BUNDLED}")


def papi_home() -> Path:
    """``$PAPI_HOME`` or ``~/.papi`` (expanded, absolute)."""
    return Path(os.environ.get(ENV_HOME) or "~/.papi").expanduser().resolve()


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PathsConfig(_Section):
    """Filesystem layout. Relative values are resolved against the home dir."""

    home: Path = Field(default_factory=papi_home, description="Data home; set via $PAPI_HOME, not here")
    db: Path = Field(Path("papi.db"), description="SQLite database file")
    raw: Path = Field(Path("raw"), description="Content-addressed raw object store")
    logs: Path = Field(Path("logs"), description="Structured JSON logs")
    backups: Path = Field(Path("backups"), description="Nightly SQLite backups")
    claude_sandbox: Path = Field(Path("claude-sandbox"), description="Empty cwd for `claude -p` calls")

    @model_validator(mode="after")
    def _resolve(self) -> PathsConfig:
        home = self.home.expanduser().resolve()
        object.__setattr__(self, "home", home)
        for name in ("db", "raw", "logs", "backups", "claude_sandbox"):
            p = Path(getattr(self, name)).expanduser()
            object.__setattr__(self, name, p if p.is_absolute() else home / p)
        return self


class PacksConfig(_Section):
    jurisdiction: str = Field("TR", description="Active jurisdiction pack code")
    dir: Path = Field(
        default_factory=lambda: resource_dir("packs"),
        description="Directory containing jurisdiction packs (<dir>/tr/...)",
    )


class KapConfig(_Section):
    base_url: str = Field("https://www.kap.org.tr", description="KAP public web base URL")
    contact: str = Field("set-your-email@example.com", description="Contact put in the User-Agent")
    user_agent: str = Field(
        "papi/0.1 (personal research tool; contact: {contact})",
        description="User-Agent template; {contact} is replaced with kap.contact",
    )
    session_poll_seconds: int = Field(30, description="List poll cadence during session/evening peak")
    offhours_poll_seconds: int = Field(180, description="List poll cadence overnight and on weekends")
    request_timeout: float = Field(20.0, description="Per-request timeout (s)")
    max_retries: int = Field(5, description="Fetch retries before a failed-job record + gap marker")
    min_request_interval: float = Field(1.0, description="Minimum seconds between requests to KAP")
    backfill_start: date = Field(date(2015, 1, 1), description="Earliest date for history backfill")
    reconcile_window_hours: int = Field(48, description="Re-list window for corrections/late items")
    reconcile_every_hours: int = Field(4, description="Reconcile cadence")
    attachments_fetch_policy: Literal["all", "prioritized"] = Field(
        "all", description="Attachment fetching: all (live) or prioritized (backfill)"
    )

    @property
    def resolved_user_agent(self) -> str:
        return self.user_agent.replace("{contact}", self.contact)


class PricesConfig(_Section):
    primary: str = Field("isyatirim_prices", description="Primary EOD source code")
    secondary: str = Field("yahoo_prices", description="Cross-check source code ('' disables)")
    fetch_time_ist: str = Field("18:45", description="Daily EOD fetch time (Europe/Istanbul, HH:MM)")
    history_start: date = Field(date(2015, 1, 1), description="Earliest bar to backfill")
    request_timeout: float = Field(20.0, description="Per-request timeout (s)")
    mismatch_tolerance_pct: float = Field(0.5, description="Close mismatch tolerance between sources (%)")


class TelegramConfig(_Section):
    enabled: bool = Field(False, description="Enable Telegram alerts and chat")
    allowed_user_id: int | None = Field(None, description="Only this Telegram user id is answered")
    chat_id: int | None = Field(None, description="Private chat id for alerts")
    poll_timeout_s: int = Field(30, description="getUpdates long-poll timeout")
    max_messages_per_minute: int = Field(20, description="Outgoing rate limit; overflow is batched")
    price_warn_pct: float = Field(10.0, description="Warn on trade price this % away from last close")
    undo_hours: int = Field(24, description="Undo window for recorded trades")


class LlmModels(_Section):
    analyst: str = "sonnet"
    explain: str = "sonnet"
    checker: str = "sonnet"
    backlog: str = "sonnet"
    chat: str = "sonnet"


class FocusWindow(_Section):
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"]
    )
    start: str = Field("09:00", description="HH:MM Europe/Istanbul")
    end: str = Field("18:00", description="HH:MM Europe/Istanbul")
    max_priority: Literal["P1", "P2", "P3", "P4"] = Field(
        "P1", description="Admit only this priority or more urgent"
    )


class LlmConfig(_Section):
    backend: Literal["claude_code_cli"] = Field("claude_code_cli", description="LlmBackend implementation")
    claude_bin: str = Field("claude", description="Claude Code CLI executable")
    models: LlmModels = Field(default_factory=LlmModels, description="Model alias per task")
    max_turns: int = Field(3, description="--max-turns for pipeline calls")
    chat_max_turns: int = Field(8, description="--max-turns for Telegram chat calls")
    timeout_s: int = Field(180, description="Per-call timeout (SIGINT, then SIGTERM)")
    concurrency: int = Field(1, description="Parallel claude processes (1-2)")
    share_5h: float = Field(2.0, description="Cap on rolling 5h cost_usd_est sum for papi")
    share_week: float = Field(20.0, description="Cap on rolling 7d cost_usd_est sum for papi")
    share_chat_5h: float = Field(1.0, description="Separate rolling 5h bucket for Telegram chat")
    p2_cap_fraction: float = Field(0.8, description="P2 admitted below this fraction of the 5h cap")
    p3_cap_fraction: float = Field(0.5, description="P3 admitted below this fraction of the 5h cap")
    calibration_mode: bool = Field(True, description="Week-1 calibration: P1+P2 only")
    calibration_cost_per_pct_5h: float | None = Field(None, description="Ledger cost per 1% of the 5h bar")
    calibration_cost_per_pct_week: float | None = Field(None, description="Ledger cost per 1% of weekly bar")
    calibrated_at: date | None = Field(None, description="When the calibration was last done")
    backlog_enabled: bool = Field(False, description="Off-hours backlog extraction")
    night_window: str = Field("01:00-07:00", description="Backlog window HH:MM-HH:MM (Istanbul)")
    backlog_max_week_fraction: float = Field(0.5, description="Backlog only below this weekly usage")
    focus: list[FocusWindow] = Field(
        default_factory=list, description="Focus schedule windows ([[llm.focus]] tables)"
    )
    include_position: bool = Field(True, description="Send my position (shares, cost, weight) for P1")


class WebConfig(_Section):
    host: str = Field("127.0.0.1", description="Bind address (loopback only)")
    port: int = Field(8765, description="Dashboard/API port")
    token: str | None = Field(None, description="Optional bearer token (prefer keyring 'papi'/'web_token')")


class AlertsConfig(_Section):
    quiet_hours_enabled: bool = Field(True, description="Hold non-P1 alerts during quiet hours")
    quiet_start: str = Field("23:00", description="Quiet hours start HH:MM (Istanbul)")
    quiet_end: str = Field("07:30", description="Quiet hours end HH:MM (Istanbul)")
    holdings_override_quiet: bool = Field(True, description="P1 holdings alerts ignore quiet hours")
    catchup_batch_threshold: int = Field(5, description="More catch-up alerts than this are batched")
    macos_notifications: bool = Field(True, description="Also show macOS notifications")
    digest_time: str = Field("08:00", description="Daily digest time HH:MM (Istanbul)")
    weekly_review: str = Field("sun 18:00", description="Weekly review day + time (Istanbul)")


class PortfolioConfig(_Section):
    default_name: str = Field("Ana", description="Default portfolio name created by `papi init`")
    base_currency: str = Field("TRY", description="Default portfolio base currency")
    account_label: str | None = Field("İşCep", description="Default portfolio account label")
    lot_size: int = Field(1, description="Shares per 'lot' (BIST equity lot = 1 share)")
    recently_sold_days: int = Field(30, description="Previously held within N days → P2")


class TriageConfig(_Section):
    p3_materiality_threshold: int = Field(60, description="Rules materiality ≥ this → P3 (else P4)")
    always_tell_codes: list[str] = Field(
        default_factory=lambda: ["market.measure"], description="P3 codes that always alert"
    )


class LoggingConfig(_Section):
    level: str = Field("INFO", description="Log level")
    console: bool = Field(True, description="Also log to console")
    max_bytes: int = Field(10_000_000, description="Rotate papi.log at this size")
    backups: int = Field(5, description="Rotated log files kept")


class BackupConfig(_Section):
    keep_daily: int = 14
    keep_weekly: int = 8


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    paths: PathsConfig = Field(default_factory=PathsConfig)
    packs: PacksConfig = Field(default_factory=PacksConfig)
    kap: KapConfig = Field(default_factory=KapConfig)
    prices: PricesConfig = Field(default_factory=PricesConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)

    @property
    def config_path(self) -> Path:
        return self.paths.home / "config.toml"

    @property
    def pack_dir(self) -> Path:
        """Directory of the active jurisdiction pack, e.g. ``<repo>/packs/tr``."""
        return self.packs.dir / self.packs.jurisdiction.lower()

    def section(self, name: str) -> dict[str, Any]:
        """Raw dict of an unknown (slice-specific) top-level section; ``{}`` if absent."""
        value = (self.model_extra or {}).get(name, {})
        return value if isinstance(value, dict) else {}


def config_path(home: Path | None = None) -> Path:
    return (home or papi_home()) / "config.toml"


def load_settings(home: Path | None = None) -> Settings:
    """Read ``config.toml`` under ``home`` (default ``papi_home()``) and validate it."""
    home = home or papi_home()
    path = config_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    paths = dict(data.get("paths") or {})
    paths["home"] = home  # home always comes from the environment
    data["paths"] = paths
    return Settings.model_validate(data)


_cached: Settings | None = None


def get_settings() -> Settings:
    """Process-wide cached settings. Call ``reset_settings()`` after changing env/config."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached


def reset_settings() -> None:
    global _cached
    _cached = None


def _keyring_get(key: str) -> str | None:
    try:
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(KEYRING_SERVICE, key)
        except KeyringError:
            return None
    except Exception:  # keyring backend import/initialisation problems → treat as absent
        return None


def get_telegram_token() -> str | None:
    """Bot token: ``$PAPI_TELEGRAM_TOKEN`` or keyring (service ``papi``, key ``telegram_token``)."""
    return os.environ.get(ENV_TELEGRAM_TOKEN) or _keyring_get(KEYRING_TELEGRAM)


def get_web_token() -> str | None:
    """API bearer token: ``$PAPI_WEB_TOKEN``, keyring ``papi``/``web_token``, then ``web.token``."""
    return os.environ.get(ENV_WEB_TOKEN) or _keyring_get(KEYRING_WEB) or get_settings().web.token


def set_secret(key: str, value: str) -> None:
    """Store a secret in the keyring under service ``papi``."""
    import keyring

    keyring.set_password(KEYRING_SERVICE, key, value)


# ---------------------------------------------------------------------------------------------
# Default config file rendering


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return _toml_value(str(value))
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot render {type(value).__name__} as TOML")


def _render_section(prefix: str, model: BaseModel, lines: list[str]) -> None:
    lines.append(f"[{prefix}]")
    nested: list[tuple[str, BaseModel]] = []
    # Array-of-tables examples go after the section and its sub-tables: uncommented in place, a
    # ``[[llm.focus]]`` header would capture every key that follows it.
    table_examples: list[str] = []
    for name, field in type(model).model_fields.items():
        if prefix == "paths" and name == "home":
            lines.append('# home = "~/.papi"  # not read from this file; set the PAPI_HOME env var')
            continue
        value = getattr(model, name)
        desc = f"  # {field.description}" if field.description else ""
        if isinstance(value, BaseModel):
            nested.append((name, value))
            continue
        if prefix == "paths":
            value = Path(value).relative_to(model.home) if Path(value).is_relative_to(model.home) else value
        if value is None:
            lines.append(f"# {name} ={desc}")
        elif isinstance(value, list) and value and isinstance(value[0], BaseModel):
            continue
        elif isinstance(value, list) and not value and name == "focus":
            example = FocusWindow()
            table_examples += [
                f"# Focus schedule example ({field.description}):",
                "# [[llm.focus]]",
                f"# days = {_toml_value(example.days)}",
                f"# start = {_toml_value(example.start)}",
                f"# end = {_toml_value(example.end)}",
                f"# max_priority = {_toml_value(example.max_priority)}",
                "",
            ]
        else:
            lines.append(f"{name} = {_toml_value(value)}{desc}")
    lines.append("")
    for name, sub in nested:
        _render_section(f"{prefix}.{name}", sub, lines)
    lines.extend(table_examples)


def render_default_config() -> str:
    """Commented default ``config.toml`` reflecting every default above."""
    defaults = Settings(paths=PathsConfig(home=Path("/PAPI_HOME")))
    lines = [
        "# papi configuration. Every key is optional; values shown are the defaults.",
        "# Secrets are NOT stored here: Telegram token → keyring service 'papi', key 'telegram_token'",
        "# (or env PAPI_TELEGRAM_TOKEN). Paths are relative to $PAPI_HOME unless absolute.",
        "",
    ]
    for name in type(defaults).model_fields:
        section = getattr(defaults, name)
        if name == "packs":
            lines.append("[packs]")
            lines.append(f"jurisdiction = {_toml_value(section.jurisdiction)}")
            lines.append('# dir = "<repo>/packs"  # directory containing jurisdiction packs')
            lines.append("")
            continue
        _render_section(name, section, lines)
    return "\n".join(lines).rstrip() + "\n"


def write_default_config(home: Path | None = None) -> bool:
    """Write the commented default config if absent. Returns True when a file was written."""
    path = config_path(home)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_default_config(), encoding="utf-8")
    return True
