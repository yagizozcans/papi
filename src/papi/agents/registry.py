"""Tool registry: every ``papi/agents/tools/<module>.py`` defines one tool.

Convention (as in Vorion Tool Studio): the first public function defined in the module is the tool; its
typed keyword arguments become the JSON schema, its docstring is the description the model reads. A
tool returns a dict and reports failure as ``{"success": false, "error": "..."}``. ``EFFECT = "read"``
(default) or ``"write"`` at module level declares the effect class; write tools are not bindable by
agents yet. Tool names are global; duplicates are an error.

``guard()`` wraps a tool for serving: exceptions become error dicts, oversized results are refused
(tools paginate themselves), and each call is appended to the run transcript (``PAPI_AGENT_TRANSCRIPT``).
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import os
import pkgutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import papi.agents.tools as tools_pkg

ENV_TRANSCRIPT = "PAPI_AGENT_TRANSCRIPT"
RESULT_MAX_CHARS = 20_000


class ToolError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDef:
    name: str
    fn: Callable[..., dict[str, Any]]
    description: str
    effect: str
    module: str
    max_chars: int = RESULT_MAX_CHARS


def _tool_function(mod: Any) -> Callable[..., Any] | None:
    funcs = [
        (fn.__code__.co_firstlineno, fn)
        for name, fn in vars(mod).items()
        if inspect.isfunction(fn) and fn.__module__ == mod.__name__ and not name.startswith("_")
    ]
    return min(funcs, key=lambda t: t[0])[1] if funcs else None


def discover() -> dict[str, ToolDef]:
    found: dict[str, ToolDef] = {}
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{tools_pkg.__name__}.{info.name}")
        fn = _tool_function(mod)
        if fn is None:
            raise ToolError(f"tools/{info.name}.py defines no public function")
        doc = inspect.getdoc(fn)
        if not doc:
            raise ToolError(f"{fn.__name__}: a docstring is required (it is the tool description)")
        effect = getattr(mod, "EFFECT", "read")
        if effect not in ("read", "write"):
            raise ToolError(f"{fn.__name__}: EFFECT must be 'read' or 'write'")
        if fn.__name__ in found:
            raise ToolError(
                f"duplicate tool name {fn.__name__!r} ({info.name} and {found[fn.__name__].module})"
            )
        cap = int(getattr(mod, "MAX_RESULT_CHARS", RESULT_MAX_CHARS))
        found[fn.__name__] = ToolDef(fn.__name__, fn, doc, effect, mod.__name__, cap)
    return found


def _log(entry: dict[str, Any]) -> None:
    path = os.environ.get(ENV_TRANSCRIPT)
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def guard(
    name: str, fn: Callable[..., Any], *, max_chars: int = RESULT_MAX_CHARS, log: bool = True
) -> Callable[..., Any]:
    """Serve-time wrapper; keeps ``fn``'s signature and docstring (``functools.wraps``)."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            if not isinstance(result, dict):
                raise TypeError(f"tool returned {type(result).__name__}, expected dict")
            size = len(json.dumps(result, ensure_ascii=False, default=str))
            if size > max_chars:
                result = {
                    "success": False,
                    "error": f"result is {size} chars (max {max_chars}); narrow the arguments or page",
                }
        except Exception as exc:
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        if log:
            _log(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "tool": name,
                    "args": kwargs,
                    "ms": int((time.monotonic() - started) * 1000),
                    "result": result,
                }
            )
        return result

    return run


# Per-run tool state. The runner points PAPI_AGENT_STATE at a JSON file next to the transcript, so
# state survives the MCP server restart when a run is resumed; without it state lives in memory.
ENV_STATE = "PAPI_AGENT_STATE"
_memory_state: dict[str, Any] = {}


def _state_path() -> Path | None:
    value = os.environ.get(ENV_STATE)
    return Path(value) if value else None


def state_get(key: str, default: Any = None) -> Any:
    path = _state_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else _memory_state
    return data.get(key, default)


def state_set(key: str, value: Any) -> None:
    path = _state_path()
    if path is None:
        _memory_state[key] = value
        return
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
