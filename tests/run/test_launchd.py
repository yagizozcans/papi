from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from papi.run import launchd


class FakeLaunchctl:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        cmd = args[0]
        rc, out = 0, ""
        if cmd == "print":
            rc = 0 if self.loaded else 113
            out = (
                "com.papi.run = {\n\tstate = running\n\tpid = 4242\n\tlast exit code = 0\n}\n"
                if self.loaded
                else ""
            )
        elif cmd == "bootstrap":
            self.loaded = True
        elif cmd == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(["launchctl", *args], rc, out, "")


@pytest.fixture
def agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "LaunchAgents"
    monkeypatch.setenv(launchd.AGENTS_DIR_ENV, str(d))
    return d


@pytest.fixture
def fake_launchctl(monkeypatch: pytest.MonkeyPatch) -> FakeLaunchctl:
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "run_launchctl", fake)
    return fake


def test_plist_generation(settings, monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "papi"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchd.sys, "argv", [str(exe), "run", "service", "install"])
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
    spec = launchd.build_spec()
    data = plistlib.loads(launchd.render_plist(spec))
    assert data["Label"] == "com.papi.run"
    assert data["ProgramArguments"] == [str(exe.resolve()), "run"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["EnvironmentVariables"] == {
        "PAPI_HOME": str(settings.paths.home),
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
    }
    assert data["StandardOutPath"] == str(settings.paths.logs / "launchd.out.log")
    assert data["StandardErrorPath"] == str(settings.paths.logs / "launchd.err.log")
    assert data["WorkingDirectory"] == str(settings.paths.home)
    assert data["ExitTimeOut"] > 20


def test_plist_uv_program_arguments(settings, monkeypatch):
    monkeypatch.setattr(launchd.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    args = launchd.program_arguments(use_uv=True, project=Path("/repo"))
    assert args[1:] == ["run", "--project", "/repo", "papi", "run"]
    assert args[0].endswith("uv")


def test_install_status_uninstall(settings, agents, fake_launchctl, monkeypatch, tmp_path):
    monkeypatch.setattr(launchd.sys, "argv", [str(tmp_path / "missing" / "papi")])
    monkeypatch.setattr(launchd.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    path = launchd.install()
    assert path == agents / "com.papi.run.plist"
    assert plistlib.loads(path.read_bytes())["ProgramArguments"] == ["/usr/local/bin/papi", "run"]
    assert ["bootstrap", launchd.domain(), str(path)] in fake_launchctl.calls
    assert settings.paths.logs.is_dir()

    info = launchd.status()
    assert info["installed"] and info["loaded"]
    assert info["state"] == "running" and info["pid"] == "4242"

    launchd.install()  # reinstall: boots out the old one first
    assert ["bootout", launchd.service_target()] in fake_launchctl.calls

    assert launchd.uninstall() is True
    assert not path.exists()
    assert launchd.status()["loaded"] is False
    assert launchd.uninstall() is False


def test_install_reports_bootstrap_failure(settings, agents, monkeypatch, tmp_path):
    def failing(args: list[str]) -> subprocess.CompletedProcess[str]:
        rc = 5 if args[0] == "bootstrap" else 113
        return subprocess.CompletedProcess(args, rc, "", "Bootstrap failed: 5: Input/output error")

    monkeypatch.setattr(launchd, "run_launchctl", failing)
    monkeypatch.setattr(launchd.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        launchd.install()


def test_tail(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line {i}\n" for i in range(1000)))
    assert launchd.tail(p, 3) == ["line 997", "line 998", "line 999"]
    assert launchd.tail(tmp_path / "none.log", 3) == []
