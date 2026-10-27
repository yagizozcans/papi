"""Registration of event handlers, job kinds and periodic tasks.

Slices register at import time with the decorators (re-exported as ``papi.core.events.handles``,
``papi.core.jobs.job_kind``, ``papi.core.runtime.periodic``). ``load_plugins()`` imports
``papi.<slice>.handlers`` for every slice in ``PLUGIN_MODULES`` so that publishing processes and workers
see the same registrations; ``publish()`` and the worker call it automatically.

Callables may be sync or ``async``. Signatures:
- event handler: ``fn(event: CloudEvent, ctx: JobContext) -> None``
- job kind:      ``fn(payload: dict, ctx: JobContext) -> None``
- periodic:      ``fn(ctx: JobContext) -> None``
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PLUGIN_MODULES: tuple[str, ...] = (
    "sources.kap",
    "sources.prices",
    "sources.legal",
    "kb",
    "triage",
    "llm",
    "verify",
    "sim",
    "portfolio",
    "notify",
    "telegram",
    "web",
    "run",
    "evals",
    "analyst",
)

EVENT_JOB_PREFIX = "event:"
PERIODIC_JOB_PREFIX = "periodic:"

DEFAULT_PRIORITY = 50
DEFAULT_MAX_ATTEMPTS = 5

PriorityLike = int | Callable[[Any], int] | None
IntervalLike = float | Callable[[datetime], float]


@dataclass(frozen=True)
class HandlerSpec:
    name: str
    event_type: str
    func: Callable[..., Any]
    priority: PriorityLike = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @property
    def job_kind(self) -> str:
        return f"{EVENT_JOB_PREFIX}{self.name}"


@dataclass(frozen=True)
class JobKindSpec:
    kind: str
    func: Callable[..., Any]
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


@dataclass(frozen=True)
class PeriodicSpec:
    name: str
    interval: IntervalLike
    func: Callable[..., Any]
    priority: int = DEFAULT_PRIORITY
    run_on_start: bool = True
    max_attempts: int = 1

    @property
    def job_kind(self) -> str:
        return f"{PERIODIC_JOB_PREFIX}{self.name}"

    def seconds(self, now: datetime) -> float:
        value = self.interval(now) if callable(self.interval) else self.interval
        return max(float(value), 1.0)


@dataclass
class Registry:
    handlers: dict[str, list[HandlerSpec]] = field(default_factory=dict)  # by event type
    job_kinds: dict[str, JobKindSpec] = field(default_factory=dict)
    periodics: dict[str, PeriodicSpec] = field(default_factory=dict)

    def handlers_for(self, event_type: str) -> list[HandlerSpec]:
        return list(self.handlers.get(event_type, ()))

    def handler_by_kind(self, kind: str) -> HandlerSpec | None:
        for specs in self.handlers.values():
            for spec in specs:
                if spec.job_kind == kind:
                    return spec
        return None

    def all_kinds(self) -> list[str]:
        kinds = [s.job_kind for specs in self.handlers.values() for s in specs]
        kinds += list(self.job_kinds)
        kinds += [p.job_kind for p in self.periodics.values()]
        return sorted(set(kinds))


REGISTRY = Registry()
_plugins_loaded = False
autoload_plugins = True  # tests may set False to keep the registry hermetic


def _default_name(func: Callable[..., Any]) -> str:
    return f"{func.__module__}.{func.__qualname__}"


def handles(
    event_type: str,
    *,
    priority: PriorityLike = None,
    name: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(event, ctx)`` for ``event_type``; ``publish`` enqueues one job per handler.

    Job priority: ``priority`` (int, or callable ``(CloudEvent) -> int``) if given, else the priority
    passed to ``publish``, else ``DEFAULT_PRIORITY`` (50). Handler names must be unique.
    """

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        spec = HandlerSpec(name or _default_name(func), event_type, func, priority, max_attempts)
        existing = REGISTRY.handler_by_kind(spec.job_kind)
        if existing is not None:
            same_origin = _default_name(existing.func) == _default_name(func)
            if not same_origin or existing.event_type != event_type:
                raise ValueError(f"duplicate handler name {spec.name!r}")
            REGISTRY.handlers[event_type].remove(existing)  # module re-import: replace
        REGISTRY.handlers.setdefault(event_type, []).append(spec)
        return func

    return deco


def job_kind(
    kind: str, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(payload, ctx)`` as the executor of jobs with ``kind`` (enqueue with ``jobs.enqueue``).

    ``max_attempts`` is the default used by ``enqueue`` when it is not passed explicitly.
    """
    if kind.startswith((EVENT_JOB_PREFIX, PERIODIC_JOB_PREFIX)):
        raise ValueError(f"job kind prefix reserved: {kind!r}")

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY.job_kinds[kind] = JobKindSpec(kind, func, max_attempts)
        return func

    return deco


def periodic(
    name: str,
    seconds: IntervalLike,
    *,
    priority: int = DEFAULT_PRIORITY,
    run_on_start: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(ctx)`` to run every ``seconds`` (number, or callable ``(now_utc) -> seconds``).

    The scheduler enqueues a ``periodic:<name>`` job when due (never while one is still queued or
    running); a worker executes it. Last run times persist in ``kv_state`` (``periodic:<name>``).
    """

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY.periodics[name] = PeriodicSpec(name, seconds, func, priority, run_on_start)
        return func

    return deco


def load_plugins() -> list[str]:
    """Import ``papi.<slice>.handlers`` for each slice once; returns the modules imported.

    Only ``ModuleNotFoundError`` for that exact module is ignored (slice has no handlers).
    """
    global _plugins_loaded
    if _plugins_loaded or not autoload_plugins:
        return []
    loaded: list[str] = []
    for mod in PLUGIN_MODULES:
        modname = f"papi.{mod}.handlers"
        try:
            importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            if exc.name != modname:
                raise
            continue
        loaded.append(modname)
    _plugins_loaded = True
    return loaded


def snapshot() -> Registry:
    """Copy of the registry (tests: pair with ``restore``)."""
    return Registry(
        handlers={k: list(v) for k, v in REGISTRY.handlers.items()},
        job_kinds=dict(REGISTRY.job_kinds),
        periodics=dict(REGISTRY.periodics),
    )


def restore(saved: Registry) -> None:
    REGISTRY.handlers = {k: list(v) for k, v in saved.handlers.items()}
    REGISTRY.job_kinds = dict(saved.job_kinds)
    REGISTRY.periodics = dict(saved.periodics)


def clear() -> None:
    REGISTRY.handlers.clear()
    REGISTRY.job_kinds.clear()
    REGISTRY.periodics.clear()
