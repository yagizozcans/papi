"""Worker lanes: every registered job kind goes to exactly one lane.

- ``telegram``: the Telegram long-poll periodic (blocks up to ``telegram.poll_timeout_s``), concurrency 1;
- ``llm``: jobs whose handlers call Claude (kind prefixes or handler modules from ``[run]``),
  concurrency ``llm.concurrency``;
- ``io``: KAP ingestion (``[run].io_kind_prefixes``), concurrency ``[run].io_concurrency``;
- ``general``: everything else, concurrency ``[run].general_concurrency``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from papi.core.config import Settings, get_settings
from papi.core.registry import EVENT_JOB_PREFIX, PERIODIC_JOB_PREFIX, REGISTRY, Registry
from papi.run.config import RunConfig, run_config

LANE_ORDER = ("telegram", "llm", "io", "general")


@dataclass
class Lane:
    name: str
    concurrency: int
    kinds: list[str] = field(default_factory=list)


def _handler_module(kind: str, registry: Registry) -> str | None:
    if kind.startswith(EVENT_JOB_PREFIX):
        spec = registry.handler_by_kind(kind)
        return spec.func.__module__ if spec else None
    if kind.startswith(PERIODIC_JOB_PREFIX):
        pspec = registry.periodics.get(kind.removeprefix(PERIODIC_JOB_PREFIX))
        return pspec.func.__module__ if pspec else None
    kspec = registry.job_kinds.get(kind)
    return kspec.func.__module__ if kspec else None


def _in_modules(module: str | None, prefixes: list[str]) -> bool:
    return module is not None and any(module == p or module.startswith(p + ".") for p in prefixes)


def lane_for(kind: str, cfg: RunConfig, registry: Registry = REGISTRY) -> str:
    if kind in cfg.telegram_kinds:
        return "telegram"
    if kind.startswith(tuple(cfg.llm_kind_prefixes)) or _in_modules(
        _handler_module(kind, registry), cfg.llm_modules
    ):
        return "llm"
    if kind.startswith(tuple(cfg.io_kind_prefixes)):
        return "io"
    return "general"


def partition(
    kinds: list[str] | None = None,
    *,
    settings: Settings | None = None,
    registry: Registry = REGISTRY,
) -> list[Lane]:
    """Lanes with their kinds (default: every registered kind). Lanes without kinds are omitted."""
    s = settings or get_settings()
    cfg = run_config(s)
    concurrency = {
        "telegram": 1,
        "llm": max(1, s.llm.concurrency),
        "io": cfg.io_concurrency,
        "general": cfg.general_concurrency,
    }
    lanes = {name: Lane(name, concurrency[name]) for name in LANE_ORDER}
    for kind in sorted(set(registry.all_kinds() if kinds is None else kinds)):
        lanes[lane_for(kind, cfg, registry)].kinds.append(kind)
    return [lane for lane in lanes.values() if lane.kinds]
