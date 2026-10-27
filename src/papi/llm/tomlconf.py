"""Minimal in-place ``config.toml`` updater for ``[llm]`` scalar keys (used by calibration).

Keeps every comment and unrelated line: a key's existing line (or its commented-out default
``# key = …``) is replaced; otherwise the key is appended to the end of the ``[llm]`` table. The
result is re-validated as ``Settings`` before it atomically replaces the file.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

from papi.core.config import Settings, config_path, papi_home, reset_settings

_HEADER = re.compile(r"^\s*\[")


def toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"unsupported TOML scalar {type(value).__name__}")


def update_section(text: str, section: str, values: dict[str, Any]) -> str:
    """Return ``text`` with ``section``'s keys set to ``values`` (scalars only)."""
    lines = text.splitlines()
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(header)
        lines.extend(f"{k} = {toml_scalar(v)}" for k, v in values.items())
        return "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if _HEADER.match(lines[i])), len(lines))
    remaining = dict(values)
    for i in range(start + 1, end):
        for key in list(remaining):
            m = re.match(rf"^\s*#?\s*{re.escape(key)}\s*=(?P<rest>.*)$", lines[i])
            if not m:
                continue
            comment = ""
            if (pos := m.group("rest").find("  # ")) >= 0:
                comment = m.group("rest")[pos:]
            lines[i] = f"{key} = {toml_scalar(remaining.pop(key))}{comment}"
            break
    if remaining:
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = [f"{k} = {toml_scalar(v)}" for k, v in remaining.items()]
    return "\n".join(lines) + "\n"


def update_llm_config(values: dict[str, Any], home: Path | None = None) -> Path:
    """Set ``[llm]`` keys in ``$PAPI_HOME/config.toml`` atomically; validates before replacing."""
    home = home or papi_home()
    path = config_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = update_section(text, "llm", values)
    tomllib.loads(new_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        _validate(Path(tmp), home)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    reset_settings()
    return path


def _validate(candidate: Path, home: Path) -> None:
    """Validate a candidate config file as if it were ``home``'s config."""
    with candidate.open("rb") as fh:
        data = tomllib.load(fh)
    data["paths"] = {**(data.get("paths") or {}), "home": home}
    Settings.model_validate(data)
