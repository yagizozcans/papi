"""``papi llm`` — Claude backend & usage governor commands (04 §5–§6)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Any

import typer
from rich.console import Console

from papi.core.config import get_settings
from papi.core.db import connect, transaction
from papi.core.timeutil import now_utc, to_ist
from papi.llm import calibration, governor
from papi.llm.backend import ClaudeCodeCliBackend, check_environment, claude_version, model_for_task

app = typer.Typer(help="Claude Code arka ucu ve kullanım yöneticisi.", no_args_is_help=True)
console = Console(soft_wrap=True, markup=False, highlight=False)

TEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}
TEST_SYSTEM_PROMPT = 'You are a health check. Reply with the JSON object {"ok": true} and nothing else.'
TEST_PROMPT = "Health check: return ok=true."

_DURATION_RE = re.compile(r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*$", re.IGNORECASE)


def parse_duration(text: str) -> timedelta:
    """``3h``, ``90m``, ``1h30m``, ``2d`` → timedelta."""
    m = _DURATION_RE.match(text)
    if not m or not any(m.groups()):
        raise typer.BadParameter(f"süre anlaşılamadı: {text!r} (örnek: 3h, 90m, 1h30m, 2d)")
    d, h, mins = (int(g or 0) for g in m.groups())
    delta = timedelta(days=d, hours=h, minutes=mins)
    if delta <= timedelta(0):
        raise typer.BadParameter("süre sıfırdan büyük olmalı")
    return delta


def _fmt_dt(value: datetime | None) -> str:
    return f"{to_ist(value):%d.%m.%Y %H:%M}" if value else "-"


def _print_bars(bars: dict[str, Any] | None) -> None:
    """Last subscription bar utilisation seen by the CLI (includes interactive Claude use)."""
    if not bars:
        return
    for key, label in (("five_hour", "5 saat"), ("seven_day", "haftalık")):
        bar = bars.get(key)
        if bar and bar.get("utilization") is not None:
            console.print(
                f"Claude {label} çubuğu (tüm kullanım, {_fmt_dt(bars['observed_at'])} itibarıyla): "
                f"%{bar['utilization'] * 100:.0f} — sıfırlanma {_fmt_dt(bar['resets_at'])}"
            )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


@app.command("status")
def status_cmd(as_json: Annotated[bool, typer.Option("--json", help="JSON çıktı")] = False) -> None:
    """Kullanım, limitler, duraklatma ve kuyruk durumu."""
    with connect() as conn:
        st = governor.status(conn)
    if as_json:
        console.print(json.dumps(_jsonable(st), ensure_ascii=False, indent=2))
        return
    if st["paused"]:
        console.print(f"Durum: DURAKLATILDI ({st['pause_reason']}) — bitiş: {_fmt_dt(st['paused_until'])}")
    else:
        console.print("Durum: aktif")
    console.print(f"5 saat (boru hattı): {st['used_5h']:.4f} / {st['cap_5h']:.4f}")
    console.print(f"5 saat (sohbet):     {st['used_chat_5h']:.4f} / {st['cap_chat_5h']:.4f}")
    console.print(f"7 gün:               {st['used_week']:.4f} / {st['cap_week']:.4f}")
    console.print(f"Kalibrasyon modu: {'açık (yalnız CHAT/P1/P2)' if st['calibration_mode'] else 'kapalı'}")
    if st["focus"]:
        console.print(
            f"Odak penceresi: en fazla {st['focus']['max_priority']} — bitiş {_fmt_dt(st['focus']['until'])}"
        )
    console.print(f"Gece penceresi aktif: {'evet' if st['night_window_active'] else 'hayır'}")
    _print_bars(st["subscription_bars"])
    if st["by_task"]:
        console.print("Son 7 gün (görev: çağrı, maliyet, ort., hata):")
        for task, row in sorted(st["by_task"].items()):
            console.print(
                f"  {task}: {row['calls']}, {row['cost']:.4f}, {row['avg_cost']:.4f}, {row['failures']}"
            )
    deferred = st["queue"]["llm_deferred"]
    if deferred:
        console.print("Ertelenen LLM işleri: " + ", ".join(f"{k}={v}" for k, v in sorted(deferred.items())))


@app.command("pause")
def pause_cmd(
    for_: Annotated[
        str | None, typer.Option("--for", help="Süre (3h, 90m, 1h30m); boşsa resume'a kadar")
    ] = None,
    reason: Annotated[str, typer.Option(help="Neden")] = "manual",
) -> None:
    """Tüm Claude çağrılarını duraklat."""
    until = now_utc() + parse_duration(for_) if for_ else None
    with transaction() as conn:
        governor.pause(conn, until, reason=reason)
    console.print(
        f"Claude çağrıları duraklatıldı — bitiş: {_fmt_dt(until) if until else 'resume komutuna kadar'}"
    )


@app.command("resume")
def resume_cmd() -> None:
    """Duraklatmayı kaldır."""
    with transaction() as conn:
        governor.resume(conn)
    console.print("Claude çağrıları yeniden etkin.")


@app.command("check")
def check_cmd() -> None:
    """Claude CLI kurulumunu, ortamı (ANTHROPIC_API_KEY) ve sandbox dizinini denetle."""
    s = get_settings()
    version = claude_version(s)
    console.print(f"claude: {s.llm.claude_bin} — sürüm: {version or 'bulunamadı'}")
    console.print(f"sandbox: {s.paths.claude_sandbox}")
    problems = check_environment(s)
    for p in problems:
        console.print(f"HATA: {p}")
    if problems:
        raise typer.Exit(1)
    console.print("Tamam.")


@app.command("test")
def test_cmd(
    model: Annotated[str | None, typer.Option(help="Model (varsayılan: analyst modeli)")] = None,
) -> None:
    """Küçük, gerçek bir Claude çağrısı yap (şema: {ok: boolean}); deftere işlenir."""
    backend = ClaudeCodeCliBackend()
    res = backend.complete(
        task="test",
        prompt=TEST_PROMPT,
        system_prompt=TEST_SYSTEM_PROMPT,
        schema=TEST_SCHEMA,
        model=model or model_for_task("analyst"),
        max_turns=get_settings().llm.max_turns,
    )
    with transaction() as conn:
        governor.record_usage(conn, res, task="test", priority="P1")
    console.print(f"sonuç: {res.error_kind or 'ok'}{' — ' + res.error if res.error else ''}")
    console.print(f"structured_output: {json.dumps(res.structured_output, ensure_ascii=False)}")
    console.print(f"modeller: {', '.join(res.model_ids) or '-'}")
    console.print(f"maliyet (total_cost_usd): {res.cost_usd_est:.6f}")
    console.print(f"süre: {res.duration_ms} ms (çıkış kodu {res.exit_code})")
    if res.raw_json:
        console.print(f"JSON anahtarları: {', '.join(sorted(res.raw_json))}")
    if res.error_kind:
        raise typer.Exit(1)


@app.command("calibrate")
def calibrate_cmd(
    start: Annotated[bool, typer.Option("--start", help="Kalibrasyon başlangıç işaretini şimdi koy")] = False,
    bar_5h_pct: Annotated[
        float | None, typer.Option("--bar-5h-pct", help="papi'nin 5 saatlik çubukta yol açtığı artış (%)")
    ] = None,
    bar_week_pct: Annotated[
        float | None, typer.Option("--bar-week-pct", help="papi'nin haftalık çubukta yol açtığı artış (%)")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Hesapla ama yapılandırmaya yazma")] = False,
) -> None:
    """claude.ai kullanım çubuğu hareketini defterle karşılaştırıp limitleri ayarla (Max 5x profili)."""
    if start:
        with transaction() as conn:
            at = calibration.start(conn)
            bars = governor.subscription_bars(conn)
        console.print(
            f"Kalibrasyon başladı: {_fmt_dt(at)}. claude.ai → Settings → Usage çubuklarını şimdi not edin; "
            "sonra `papi llm calibrate --bar-5h-pct X --bar-week-pct Y` çalıştırın."
        )
        _print_bars(bars)
        return
    with connect() as conn:
        try:
            cal = calibration.compute(conn, bar_5h_pct=bar_5h_pct, bar_week_pct=bar_week_pct)
        except calibration.CalibrationError as exc:
            console.print(f"HATA: {exc}")
            raise typer.Exit(1) from exc
        bars = governor.subscription_bars(conn)
    console.print(
        f"Başlangıç: {_fmt_dt(cal.since)} — {cal.calls} çağrı, defter toplamı {cal.ledger_cost:.4f}"
    )
    _print_bars(bars)
    if cal.cost_per_pct_5h is not None:
        console.print(
            f"5 saat: %1 = {cal.cost_per_pct_5h:.4f} → share_5h = %{calibration.PROFILE_5H_PCT:g} "
            f"= {cal.share_5h:.4f}"
        )
    if cal.cost_per_pct_week is not None:
        console.print(
            f"Haftalık: %1 = {cal.cost_per_pct_week:.4f} → share_week = %{calibration.PROFILE_WEEK_PCT:g} "
            f"= {cal.share_week:.4f}"
        )
    if dry_run:
        console.print("--dry-run: yapılandırma değiştirilmedi.")
        return
    calibration.apply(cal)
    console.print(
        f"config.toml güncellendi ({get_settings().config_path}); kalibrasyon modu kapatıldı, P3 açıldı. "
        "Gece birikim işleri için llm.backlog_enabled ayarını elle açın."
    )
