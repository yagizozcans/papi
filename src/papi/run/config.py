"""``[run]`` config section (read via ``Settings.section("run")``; every key has a default).

```toml
[run]
general_concurrency = 2            # lane for everything not matched below
io_concurrency = 1                 # KAP/IO lane (KAP allows one request at a time anyway)
io_kind_prefixes = ["kap:", "periodic:kap:"]
llm_kind_prefixes = ["event:analyst", "analyst:", "periodic:analyst", "event:verify", "verify:"]
llm_modules = ["papi.analyst", "papi.verify"]   # handlers defined in these modules → LLM lane
telegram_kinds = ["periodic:telegram:poll"]      # dedicated long-poll lane
scheduler_tick_s = 1.0
watchdog_interval_s = 5.0
wake_jump_s = 120.0                # wall clock ahead of monotonic by more than this → wake
shutdown_grace_s = 20.0            # wait this long for running jobs, then release their leases
prevent_sleep_on_ac = false        # run `caffeinate -s` while the market session is open
session_start = "09:30"            # Istanbul time; used when trading_calendar has no row for the day
session_end = "18:30"
backup_hour = 3                    # nightly backup at/after this Istanbul hour
```
The LLM lane's concurrency is ``llm.concurrency``.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from papi.core.config import Settings, get_settings


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general_concurrency: int = Field(2, ge=1)
    io_concurrency: int = Field(1, ge=1)
    io_kind_prefixes: list[str] = Field(default_factory=lambda: ["kap:", "periodic:kap:"])
    llm_kind_prefixes: list[str] = Field(
        default_factory=lambda: ["event:analyst", "analyst:", "periodic:analyst", "event:verify", "verify:"]
    )
    llm_modules: list[str] = Field(default_factory=lambda: ["papi.analyst", "papi.verify"])
    telegram_kinds: list[str] = Field(default_factory=lambda: ["periodic:telegram:poll"])
    scheduler_tick_s: float = Field(1.0, gt=0)
    watchdog_interval_s: float = Field(5.0, gt=0)
    wake_jump_s: float = Field(120.0, gt=0)
    shutdown_grace_s: float = Field(20.0, ge=0)
    prevent_sleep_on_ac: bool = False
    session_start: time = time(9, 30)
    session_end: time = time(18, 30)
    backup_hour: int = Field(3, ge=0, le=23)

    @field_validator("session_start", "session_end", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> object:
        if isinstance(value, str):
            hh, _, mm = value.partition(":")
            return time(int(hh), int(mm or 0))
        return value


def run_config(settings: Settings | None = None) -> RunConfig:
    return RunConfig.model_validate((settings or get_settings()).section("run"))
