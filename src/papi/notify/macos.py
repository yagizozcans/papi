"""macOS desktop notifications (``terminal-notifier`` when installed — click opens the dashboard —
otherwise ``osascript``). No-ops unless ``alerts.macos_notifications`` is on and we run on macOS."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

from papi.core.config import get_settings

log = logging.getLogger("papi.notify.macos")


def enabled() -> bool:
    return sys.platform == "darwin" and get_settings().alerts.macos_notifications


def _applescript_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str, *, subtitle: str | None = None, url: str | None = None) -> bool:
    """Show a notification; returns True when it was handed to the OS."""
    if not enabled():
        return False
    message = message[:240]
    notifier = shutil.which("terminal-notifier")
    if notifier:
        cmd = [notifier, "-title", title, "-message", message, "-group", "papi"]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        if url:
            cmd += ["-open", url]
    else:
        osascript = shutil.which("osascript")
        if not osascript:
            return False
        script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
        if subtitle:
            script += f" subtitle {_applescript_str(subtitle)}"
        cmd = [osascript, "-e", script]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("macOS notification failed: %s", exc)
        return False
    return True
