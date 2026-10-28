"""launchd LaunchAgent for ``papi run`` (09 §4): ``~/Library/LaunchAgents/com.papi.run.plist``.

``PAPI_LAUNCH_AGENTS_DIR`` overrides the agents directory (tests). ``launchctl`` calls go through
``run_launchctl`` so tests can replace it.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from papi.core.config import REPO_ROOT, Settings, get_settings

LABEL = "com.papi.run"
AGENTS_DIR_ENV = "PAPI_LAUNCH_AGENTS_DIR"
EXIT_TIMEOUT_S = 40  # > [run].shutdown_grace_s so a graceful stop is not SIGKILLed
THROTTLE_S = 10


def agents_dir() -> Path:
    return Path(os.environ.get(AGENTS_DIR_ENV) or "~/Library/LaunchAgents").expanduser()


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain()}/{LABEL}"


def log_paths(settings: Settings | None = None) -> tuple[Path, Path]:
    logs = (settings or get_settings()).paths.logs
    return logs / "launchd.out.log", logs / "launchd.err.log"


def current_papi_executable() -> Path | None:
    """Absolute path of the running ``papi`` console script, else ``papi`` on PATH."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "papi" and argv0.exists():
        return argv0.resolve()
    found = shutil.which("papi")
    return Path(found).resolve() if found else None


def program_arguments(*, use_uv: bool = False, project: Path | None = None) -> list[str]:
    """``[<abs papi>, "run"]``; with ``use_uv`` (or when no ``papi`` executable is found)
    ``[<abs uv>, "run", "--project", <repo>, "papi", "run"]``."""
    exe = None if use_uv else current_papi_executable()
    if exe is not None:
        return [str(exe), "run"]
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("neither a `papi` executable nor `uv` was found on PATH")
    return [str(Path(uv).resolve()), "run", "--project", str(project or REPO_ROOT), "papi", "run"]


@dataclass
class PlistSpec:
    program_arguments: list[str]
    home: Path
    path_env: str
    stdout: Path
    stderr: Path


def build_spec(*, use_uv: bool = False, settings: Settings | None = None) -> PlistSpec:
    s = settings or get_settings()
    out, err = log_paths(s)
    return PlistSpec(
        program_arguments=program_arguments(use_uv=use_uv),
        home=s.paths.home,
        # launchd's default PATH lacks ~/.local/bin and Homebrew, where `claude` usually lives.
        path_env=os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        stdout=out,
        stderr=err,
    )


def render_plist(spec: PlistSpec) -> bytes:
    data = {
        "Label": LABEL,
        "ProgramArguments": spec.program_arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_S,
        "ExitTimeOut": EXIT_TIMEOUT_S,
        "WorkingDirectory": str(spec.home),
        "EnvironmentVariables": {"PAPI_HOME": str(spec.home), "PATH": spec.path_env},
        "StandardOutPath": str(spec.stdout),
        "StandardErrorPath": str(spec.stderr),
    }
    return plistlib.dumps(data, sort_keys=False)


def run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def is_loaded() -> bool:
    return run_launchctl(["print", service_target()]).returncode == 0


def install(*, use_uv: bool = False, settings: Settings | None = None) -> Path:
    """Write the plist and (re)load it with ``launchctl bootstrap``."""
    spec = build_spec(use_uv=use_uv, settings=settings)
    spec.stdout.parent.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_plist(spec))
    if is_loaded():
        run_launchctl(["bootout", service_target()])
    res = run_launchctl(["bootstrap", domain(), str(path)])
    if res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed ({res.returncode}): {res.stderr.strip()}")
    return path


def uninstall() -> bool:
    """``launchctl bootout`` and remove the plist. Returns whether anything was removed/unloaded."""
    changed = False
    if is_loaded():
        res = run_launchctl(["bootout", service_target()])
        if res.returncode != 0:
            raise RuntimeError(f"launchctl bootout failed ({res.returncode}): {res.stderr.strip()}")
        changed = True
    path = plist_path()
    if path.exists():
        path.unlink()
        changed = True
    return changed


def status() -> dict[str, object]:
    """``installed``, ``loaded``, and ``state``/``pid``/``last_exit_code`` from ``launchctl print``."""
    res = run_launchctl(["print", service_target()])
    info: dict[str, object] = {
        "installed": plist_path().exists(),
        "plist": str(plist_path()),
        "loaded": res.returncode == 0,
    }
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key in ("state", "pid", "last exit code"):
                info[key.replace(" ", "_")] = value.strip()
    return info


def tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block = min(size, max(lines, 1) * 400)
        while True:
            fh.seek(size - block)
            data = fh.read().decode("utf-8", errors="replace").splitlines()
            if len(data) > lines or block == size:
                return data[-lines:] if lines else []
            block = min(size, block * 2)
