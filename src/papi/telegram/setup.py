"""``papi telegram setup`` helpers: capture my user/chat id from the first private message and write
them into ``config.toml`` (comments and other sections are preserved)."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from papi.telegram.api import TelegramClient

_KEY_LINE = re.compile(r"^\s*#?\s*([A-Za-z_]\w*)\s*=")


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_config_text(text: str, section: str, values: dict[str, Any]) -> str:
    """Set ``values`` in ``[section]``: replaces existing (or commented-out) keys, adds missing ones."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"), None)
    if header is None:
        block = ["", f"[{section}]", *(f"{k} = {_toml(v)}" for k, v in values.items())]
        return "\n".join(lines + block) + "\n"
    end = next((j for j in range(header + 1, len(lines)) if lines[j].lstrip().startswith("[")), len(lines))
    remaining = dict(values)
    for j in range(header + 1, end):
        m = _KEY_LINE.match(lines[j])
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[j] = f"{key} = {_toml(remaining.pop(key))}"
    lines[header + 1 : header + 1] = [f"{k} = {_toml(v)}" for k, v in remaining.items()]
    return "\n".join(lines) + "\n"


def write_config_values(path: Path, section: str, values: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(update_config_text(text, section, values), encoding="utf-8")


def wait_for_first_message(
    client: TelegramClient, *, wait_s: float, poll_s: int = 25
) -> tuple[dict[str, Any] | None, int | None]:
    """Long-poll until a private-chat message arrives. Returns (message, next offset)."""
    deadline = time.monotonic() + wait_s
    offset: int | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, offset
        updates = client.get_updates(offset, timeout=max(0, min(poll_s, int(remaining))))
        for upd in sorted(updates, key=lambda u: u["update_id"]):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if msg and (msg.get("chat") or {}).get("type") == "private" and msg.get("from"):
                return msg, offset
