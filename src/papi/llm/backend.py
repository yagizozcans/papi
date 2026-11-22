"""Claude Code CLI backend (04 §5): ``claude -p`` as a sandboxed pure function.

Every call runs in the empty ``$PAPI_HOME/claude-sandbox`` directory with ``ANTHROPIC_API_KEY``
removed from the environment (so the subscription is billed, never the API), no built-in tools
(except read-only ones an agent binds explicitly, e.g. ``WebSearch``),
no MCP servers except an explicitly passed config, project-only setting sources, no session
persistence and a hard turn cap. The prompt goes in on stdin; the system prompt via a temp file.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema

from papi.core.config import Settings, get_settings
from papi.llm.types import ErrorKind, LlmResult

# Environment variables never passed to ``claude``: the API key would switch billing to the API
# (B-23); ``CLAUDECODE`` marks a nested Claude Code session when papi is started from one.
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE")
EMPTY_MCP_CONFIG: dict[str, Any] = {"mcpServers": {}}
DEFAULT_GRACE_S = 5.0
_STDERR_KEEP = 4000


class LlmBackend(Protocol):
    def complete(
        self,
        *,
        task: str,
        prompt: str,
        system_prompt: str,
        schema: dict | None,
        model: str | None = None,
        max_turns: int | None = None,
        timeout_s: float | None = None,
        mcp_config: dict | None = None,
        allowed_tools: list[str] | None = None,
        builtin_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
        persist_session: bool = False,
        resume_session: str | None = None,
    ) -> LlmResult: ...


def get_backend() -> LlmBackend:
    backend = get_settings().llm.backend
    if backend == "claude_code_cli":
        return ClaudeCodeCliBackend()
    raise ValueError(f"unknown llm backend {backend!r}")


def sandbox_dir(settings: Settings | None = None) -> Path:
    """The empty working directory for ``claude`` (created on demand)."""
    path = (settings or get_settings()).paths.claude_sandbox
    path.mkdir(parents=True, exist_ok=True)
    return path


def prompt_tmp_dir(settings: Settings | None = None) -> Path:
    """papi-owned scratch dir for system-prompt files (deliberately outside the sandbox)."""
    path = (settings or get_settings()).paths.home / "tmp" / "llm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in SCRUBBED_ENV:
        env.pop(key, None)
    return env


def model_for_task(task: str, settings: Settings | None = None) -> str:
    models = (settings or get_settings()).llm.models
    return getattr(models, task) if task in type(models).model_fields else models.analyst


def max_turns_for_task(task: str, settings: Settings | None = None) -> int:
    llm = (settings or get_settings()).llm
    return llm.chat_max_turns if task == "chat" else llm.max_turns


def build_argv(
    *,
    claude_bin: str,
    model: str,
    system_prompt_file: Path,
    max_turns: int,
    schema: dict | None,
    mcp_config: dict | None = None,
    allowed_tools: list[str] | None = None,
    builtin_tools: list[str] | None = None,
    max_budget_usd: float | None = None,
    persist_session: bool = False,
    resume_session: str | None = None,
) -> list[str]:
    """argv as 04 §5.1 plus ``--verbose`` (the prompt itself is sent on stdin).

    ``--verbose`` turns the JSON output into the message list (system/init, rate_limit_event, …,
    result); the rate-limit event carries the subscription bar utilisation and exact reset times.
    """
    argv = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--system-prompt-file",
        str(system_prompt_file),
        "--tools",
        ",".join(builtin_tools or []),
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps(mcp_config if mcp_config is not None else EMPTY_MCP_CONFIG, ensure_ascii=False),
    ]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    argv += [
        "--setting-sources",
        "project",
        *([] if persist_session or resume_session else ["--no-session-persistence"]),
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
        "--verbose",
    ]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{max_budget_usd:g}"]
    if resume_session:
        argv += ["--resume", resume_session]
    return argv


# ---------------------------------------------------------------------------------------------
# Output parsing and error classification

_LIMIT_RE = re.compile(
    r"usage limit|limit reached|hit your (?:\w+ )*limit|rate[_ -]?limit|out of (?:extra )?usage"
    r"|too many requests|\b429\b",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"invalid api key|authentication_error|authentication failed|/login|not logged in"
    r"|oauth token (?:has )?expired|token (?:has )?expired|unauthori[sz]ed|\b401\b"
    r"|credit balance is too low",
    re.IGNORECASE,
)
_RESET_EPOCH_RE = re.compile(r"\|\s*(\d{9,11})\b")
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?"
    r"(?:(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+"
    r"(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}


def parse_reset_time(
    text: str, *, now: datetime | None = None, local_tz: tzinfo | None = None
) -> datetime | None:
    """Reset time from a usage-limit message (``…|<epoch>`` or ``resets 3pm (Europe/Istanbul)``)."""
    now = now or datetime.now(UTC)
    if m := _RESET_EPOCH_RE.search(text):
        return datetime.fromtimestamp(int(m.group(1)), UTC)
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or (not ampm and not m.group("m")):
        return None  # "resets 3" without am/pm or minutes is too ambiguous to trust
    tz = local_tz or datetime.now().astimezone().tzinfo or UTC
    if m.group("tz"):
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo(m.group("tz").strip())
    local_now = now.astimezone(tz)
    if m.group("mon"):
        month = _MONTHS[m.group("mon")[:3].lower()]
        day = int(m.group("day"))
        try:
            candidate = datetime(local_now.year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            return None
        if candidate < local_now - timedelta(days=1):
            candidate = candidate.replace(year=local_now.year + 1)
    else:
        candidate = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=tz).replace(
            hour=hour, minute=minute
        )
        if candidate <= local_now:
            candidate = datetime.combine(
                local_now.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz
            ).replace(hour=hour, minute=minute)
    return candidate.astimezone(UTC)


def _parse_messages(parsed: Any) -> tuple[dict | None, dict | None]:
    """(result object, last ``rate_limit_info``) from ``--output-format json`` (object or message list)."""
    if isinstance(parsed, dict):
        return (parsed if parsed.get("type", "result") == "result" else None), None
    if not isinstance(parsed, list):
        return None, None
    msgs = [m for m in parsed if isinstance(m, dict)]
    result = next((m for m in reversed(msgs) if m.get("type") == "result"), None)
    rate = next(
        (
            m["rate_limit_info"]
            for m in reversed(msgs)
            if m.get("type") == "rate_limit_event" and isinstance(m.get("rate_limit_info"), dict)
        ),
        None,
    )
    return result, rate


def _rate_limit_reset(rate: dict | None) -> datetime | None:
    value = rate.get("resetsAt") if rate else None
    return datetime.fromtimestamp(value, UTC) if isinstance(value, int | float) and value > 0 else None


def _model_usage(raw: dict) -> dict[str, dict]:
    usage = raw.get("modelUsage") or raw.get("model_usage") or {}
    return {k: v for k, v in usage.items() if isinstance(v, dict)} if isinstance(usage, dict) else {}


def _cost(raw: dict) -> float:
    total = raw.get("total_cost_usd")
    if isinstance(total, int | float):
        return float(total)
    return float(sum(float(v.get("costUSD") or v.get("cost_usd") or 0.0) for v in _model_usage(raw).values()))


def _error_texts(raw: dict | None, stderr: str) -> str:
    parts: list[str] = []
    if raw:
        for key in ("result", "error", "errors", "message", "api_error_status"):
            value = raw.get(key)
            if value is None or value == "" or value == []:
                continue
            parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    if stderr.strip():
        parts.append(stderr.strip()[-_STDERR_KEEP:])
    return "\n".join(parts)


def classify_output(
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    schema: dict | None,
    duration_ms: int,
    now: datetime | None = None,
) -> LlmResult:
    """Turn a finished ``claude -p --output-format json [--verbose]`` run into an ``LlmResult``."""
    raw: dict | None = None
    rate: dict | None = None
    parse_error: str | None = None
    try:
        raw, rate = _parse_messages(json.loads(stdout)) if stdout.strip() else (None, None)
    except json.JSONDecodeError as exc:
        parse_error = f"çıktı JSON değil: {exc}"
    if raw is None and stdout.strip() and parse_error is None:
        parse_error = "çıktıda result nesnesi yok"

    model_ids = sorted(_model_usage(raw)) if raw else []
    cost = _cost(raw) if raw else 0.0
    text = raw.get("result") if raw and isinstance(raw.get("result"), str) else None
    structured = raw.get("structured_output") if raw else None
    if structured is not None and not isinstance(structured, dict):
        structured = None
    is_error = bool(raw.get("is_error")) if raw else False
    subtype = str(raw.get("subtype") or "") if raw else ""

    def result(kind: ErrorKind, error: str | None, reset: datetime | None = None) -> LlmResult:
        return LlmResult(
            structured_output=structured if kind is None else None,
            text=text,
            model_ids=model_ids,
            cost_usd_est=cost,
            duration_ms=duration_ms,
            exit_code=exit_code,
            raw_json=raw,
            error_kind=kind,
            error=error,
            limit_reset_at=reset,
            rate_limit=rate,
        )

    failed = exit_code != 0 or is_error or raw is None
    if failed:
        detail = _error_texts(raw, stderr) or parse_error or f"çıkış kodu {exit_code}"
        api_status = raw.get("api_error_status") if raw else None
        if subtype == "error_max_turns":
            return result("error", f"error_max_turns: {detail}")
        rejected = bool(rate) and str(rate.get("status", "")).lower() == "rejected"
        if rejected or api_status == 429 or _LIMIT_RE.search(detail):
            reset = (_rate_limit_reset(rate) if rejected else None) or parse_reset_time(detail, now=now)
            return result("limit", detail, reset)
        if api_status in (401, 403) or _AUTH_RE.search(detail):
            return result("auth", detail)
        if subtype == "error_max_structured_output_retries":
            return result("schema", detail)
        return result("error", f"{subtype}: {detail}" if subtype else detail)

    if schema is not None:
        if structured is None:
            return result("schema", "structured_output yok")
        try:
            jsonschema.validate(structured, schema)
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "<kök>"
            return result("schema", f"şema doğrulaması başarısız ({path}): {exc.message}")
        except jsonschema.SchemaError as exc:
            return result("error", f"geçersiz şema: {exc.message}")
    return result(None, None)


# ---------------------------------------------------------------------------------------------
# Subprocess with timeout: SIGINT, grace, SIGTERM, grace, SIGKILL


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)


def run_with_timeout(
    argv: list[str],
    *,
    stdin_text: str,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    grace_s: float = DEFAULT_GRACE_S,
) -> tuple[int, str, str, bool]:
    """Run ``argv``; returns ``(exit_code, stdout, stderr, timed_out)``.

    The child gets its own process group so the interrupt reaches anything it spawned.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout_s)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        pass
    for sig in (signal.SIGINT, signal.SIGTERM):
        _signal_group(proc, sig)
        try:
            out, err = proc.communicate(timeout=grace_s)
            return proc.returncode, out or "", err or "", True
        except subprocess.TimeoutExpired:
            continue
    _signal_group(proc, signal.SIGKILL)
    out, err = proc.communicate()
    return proc.returncode, out or "", err or "", True


class ClaudeCodeCliBackend:
    """``LlmBackend`` over the Claude Code CLI in print mode."""

    def __init__(self, settings: Settings | None = None, *, grace_s: float = DEFAULT_GRACE_S) -> None:
        self._settings = settings
        self.grace_s = grace_s

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def complete(
        self,
        *,
        task: str,
        prompt: str,
        system_prompt: str,
        schema: dict | None,
        model: str | None = None,
        max_turns: int | None = None,
        timeout_s: float | None = None,
        mcp_config: dict | None = None,
        allowed_tools: list[str] | None = None,
        builtin_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
        persist_session: bool = False,
        resume_session: str | None = None,
    ) -> LlmResult:
        s = self.settings
        timeout = float(timeout_s if timeout_s is not None else s.llm.timeout_s)
        cwd = sandbox_dir(s)
        fd, name = tempfile.mkstemp(prefix=f"system-{task}-", suffix=".md", dir=prompt_tmp_dir(s))
        prompt_file = Path(name)
        started = time.monotonic()
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(system_prompt)
            argv = build_argv(
                claude_bin=s.llm.claude_bin,
                model=model or model_for_task(task, s),
                system_prompt_file=prompt_file,
                max_turns=max_turns if max_turns is not None else max_turns_for_task(task, s),
                schema=schema,
                mcp_config=mcp_config,
                allowed_tools=allowed_tools,
                builtin_tools=builtin_tools,
                max_budget_usd=max_budget_usd,
                persist_session=persist_session,
                resume_session=resume_session,
            )
            try:
                code, out, err, timed_out = run_with_timeout(
                    argv,
                    stdin_text=prompt,
                    cwd=cwd,
                    env=scrubbed_env(),
                    timeout_s=timeout,
                    grace_s=self.grace_s,
                )
            except OSError as exc:
                return LlmResult(
                    structured_output=None,
                    text=None,
                    model_ids=[],
                    cost_usd_est=0.0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    exit_code=-1,
                    raw_json=None,
                    error_kind="error",
                    error=f"claude çalıştırılamadı ({s.llm.claude_bin}): {exc}",
                )
        finally:
            prompt_file.unlink(missing_ok=True)
        duration_ms = int((time.monotonic() - started) * 1000)
        if timed_out:
            res = classify_output(
                exit_code=code, stdout=out, stderr=err, schema=schema, duration_ms=duration_ms
            )
            res.structured_output = None
            res.error_kind = "timeout"
            res.error = f"{timeout:g} sn zaman aşımı (SIGINT/SIGTERM gönderildi)"
            return res
        return classify_output(exit_code=code, stdout=out, stderr=err, schema=schema, duration_ms=duration_ms)


# ---------------------------------------------------------------------------------------------
# Startup check (B-23)


def claude_version(settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    exe = shutil.which(s.llm.claude_bin)
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=30, env=scrubbed_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def check_environment(settings: Settings | None = None) -> list[str]:
    """Problems that make Claude calls unsafe or impossible; empty list = OK."""
    s = settings or get_settings()
    problems: list[str] = []
    if shutil.which(s.llm.claude_bin) is None:
        problems.append(f"claude CLI bulunamadı: {s.llm.claude_bin!r} PATH üzerinde yok (llm.claude_bin).")
    elif claude_version(s) is None:
        problems.append(f"`{s.llm.claude_bin} --version` çalışmadı; Claude Code kurulumu bozuk olabilir.")
    if os.environ.get("ANTHROPIC_API_KEY"):
        problems.append(
            "ANTHROPIC_API_KEY papi ortamında tanımlı: Claude Code abonelik yerine API'den ücretlendirir. "
            "papi bu anahtarı çağrılardan siler, ama ortamdan kaldırın (B-23)."
        )
    sandbox = sandbox_dir(s)
    entries = sorted(p.name for p in sandbox.iterdir())
    if entries:
        shown = ", ".join(entries[:5]) + (" …" if len(entries) > 5 else "")
        problems.append(f"Claude sandbox dizini boş değil ({sandbox}): {shown}")
    return problems
