"""``papi run`` — the long-running process, its launchd service, status and backups (09 §1, §4–§6)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from papi.core import db
from papi.core.config import get_settings
from papi.core.timeutil import parse_iso, to_ist

app = typer.Typer(
    help="Uzun süre çalışan süreç (zamanlayıcı, işçiler, web), servis, durum ve yedekler.",
    invoke_without_command=True,
)
service_app = typer.Typer(help="launchd LaunchAgent (com.papi.run).", no_args_is_help=True)
app.add_typer(service_app, name="service")
console = Console(soft_wrap=True, markup=False, highlight=False)

WebOpt = Annotated[bool, typer.Option("--web/--no-web", help="Web panelini aynı süreçte çalıştır.")]
HostOpt = Annotated[str | None, typer.Option("--host", help="Web adresi (varsayılan web.host).")]
PortOpt = Annotated[int | None, typer.Option("--port", help="Web portu (varsayılan web.port).")]


def _start(web: bool, host: str | None, port: int | None) -> None:
    from papi.run.service import lingering_threads, main

    code = main(web=web, host=host, port=port)
    stuck = lingering_threads()
    if stuck:
        # Job threads still blocked (e.g. a Telegram long poll); their leases were handed back.
        logging.getLogger("papi.run").warning("exiting with %d job threads still running", len(stuck))
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    raise typer.Exit(code)


@app.callback()
def run(ctx: typer.Context, web: WebOpt = True, host: HostOpt = None, port: PortOpt = None) -> None:
    """Alt komut verilmezse papi'yi başlatır (Ctrl-C / SIGTERM ile temiz kapanır)."""
    if ctx.invoked_subcommand is None:
        _start(web, host, port)


@app.command("start")
def start(web: WebOpt = True, host: HostOpt = None, port: PortOpt = None) -> None:
    """papi'yi başlat (`papi run` ile aynı)."""
    _start(web, host, port)


def _fmt_ts(value: Any) -> str:
    if not value:
        return "-"
    dt = value if isinstance(value, datetime) else parse_iso(str(value))
    return f"{to_ist(dt):%d.%m.%Y %H:%M:%S}"


@app.command("status")
def status_cmd(as_json: Annotated[bool, typer.Option("--json", help="Makine okunur çıktı.")] = False) -> None:
    """Çalışıyor mu, son zamanlayıcı tıkı, kulvarlar, iş sayıları, son KAP yoklaması, yönetici, Telegram."""
    from papi.run.status import collect

    with db.connect() as conn:
        info = collect(conn)
    if as_json:
        console.print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return
    hb = info["heartbeat"] or {}
    running = "evet" if info["running"] else "hayır"
    console.print(f"Çalışıyor: {running}" + (f" (pid {info['pid']})" if info["pid"] else ""))
    if hb:
        stale = " (ESKİ)" if info["heartbeat_stale"] else ""
        console.print(
            f"Son nabız: {_fmt_ts(hb.get('heartbeat_at'))}{stale}; başlangıç {_fmt_ts(hb.get('started_at'))}"
        )
        console.print(f"Son zamanlayıcı tıkı: {_fmt_ts(hb.get('last_scheduler_tick'))}")
        if hb.get("stopped_at"):
            console.print(f"Durdu: {_fmt_ts(hb['stopped_at'])}")
        console.print(
            f"Web: {hb.get('web') or 'kapalı'}; caffeinate: {'açık' if hb.get('caffeinate') else 'kapalı'}"
        )
    lanes = {ln["name"]: ln for ln in hb.get("lanes") or []}
    for name in sorted(set(lanes) | set(info["lanes"])):
        counts = info["lanes"].get(name, {})
        conc = lanes.get(name, {}).get("concurrency", "?")
        console.print(
            f"  kulvar {name:<9} ×{conc}: kuyruk {counts.get('queued', 0)}, "
            f"çalışan {counts.get('running', 0)}, ölü {counts.get('dead', 0)}"
        )
    console.print("İşler: " + (", ".join(f"{k} {v}" for k, v in sorted(info["jobs"].items())) or "-"))
    kap = info["kap"]
    console.print(
        f"Son KAP yoklaması: {_fmt_ts(kap['last_poll_at'])} (başarılı: {_fmt_ts(kap['last_poll_ok_at'])})"
    )
    gov = info["governor"]
    if gov["paused"]:
        console.print(f"Claude yöneticisi: DURAKLATILDI ({gov['reason']}, {_fmt_ts(gov['until'])} kadar)")
    else:
        console.print("Claude yöneticisi: aktif")
    tg = info["telegram"]
    console.print(f"Telegram: {'etkin' if tg['enabled'] else 'kapalı'}{' (hazır)' if tg['ready'] else ''}")
    lb = info["last_backup"]
    console.print(f"Son yedek: {lb['path']} ({_fmt_ts(lb['at'])})" if lb else "Son yedek: -")
    if info["last_wake"]:
        w = info["last_wake"]
        console.print(f"Son uyanma: {_fmt_ts(w['at'])} ({int(w['jump_s'])} sn uyku)")
    if "service" in info:
        svc = info["service"]
        state = svc.get("state") or ("yüklü" if svc["loaded"] else "yüklü değil")
        console.print(f"launchd servisi: {state}" + (f" (pid {svc['pid']})" if svc.get("pid") else ""))


@app.command("backup-now")
def backup_now_cmd() -> None:
    """Veritabanını şimdi yedekle (SQLite online backup) ve eski yedekleri buda."""
    from papi.run.backup import BackupError, backup_now

    try:
        res = backup_now()
    except BackupError as exc:
        console.print(f"Hata: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Yedek: {res.path} ({res.bytes:,} bayt, {res.duration_s:.1f} sn)")
    for p in res.pruned:
        console.print(f"  silindi: {p.name}")


@app.command("backups")
def backups_cmd() -> None:
    """Mevcut yedekleri listele (en yenisi önce)."""
    from papi.run.backup import list_backups

    items = list_backups()
    if not items:
        console.print("Yedek yok.")
    for day, path in items:
        console.print(f"{day.isoformat()}  {path.stat().st_size:>14,}  {path}")


@app.command("restore")
def restore_cmd(
    source: Annotated[
        str, typer.Option("--from", help="Yedek dosyası, yedekler dizinindeki ad ya da YYYYMMDD.")
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Onay sorma.")] = False,
) -> None:
    """Veritabanını yedekten geri yükle (papi run çalışırken reddeder; önce güvenlik kopyası alır)."""
    from papi.run.backup import BackupError, resolve_backup, restore

    try:
        path = resolve_backup(source)
    except BackupError as exc:
        console.print(f"Hata: {exc}")
        raise typer.Exit(1) from exc
    if not yes:
        typer.confirm(f"{get_settings().paths.db} dosyası {path} ile değiştirilecek. Devam?", abort=True)
    try:
        res = restore(str(path))
    except BackupError as exc:
        console.print(f"Hata: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Geri yüklendi: {res.restored_from}")
    if res.safety_copy:
        console.print(f"Önceki veritabanının kopyası: {res.safety_copy}")


@service_app.command("install")
def service_install(
    use_uv: Annotated[
        bool, typer.Option("--uv", help="`uv run --project <repo> papi run` ile başlat (kurulu papi yerine).")
    ] = False,
) -> None:
    """LaunchAgent plist'ini yaz ve yükle (RunAtLoad, KeepAlive)."""
    from papi.run import launchd

    try:
        path = launchd.install(use_uv=use_uv)
    except RuntimeError as exc:
        console.print(f"Hata: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Kuruldu ve yüklendi: {path}")
    console.print("Komut: " + " ".join(launchd.build_spec(use_uv=use_uv).program_arguments))


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Servisi durdur (bootout) ve plist'i sil."""
    from papi.run import launchd

    try:
        changed = launchd.uninstall()
    except RuntimeError as exc:
        console.print(f"Hata: {exc}")
        raise typer.Exit(1) from exc
    console.print("Kaldırıldı." if changed else "Servis kurulu değildi.")


@service_app.command("status")
def service_status() -> None:
    """launchd durumu."""
    from papi.run import launchd

    info = launchd.status()
    console.print(f"plist: {info['plist']} ({'var' if info['installed'] else 'yok'})")
    console.print(f"yüklü: {'evet' if info['loaded'] else 'hayır'}")
    for key in ("state", "pid", "last_exit_code"):
        if key in info:
            console.print(f"{key}: {info[key]}")


@service_app.command("logs")
def service_logs(
    lines: Annotated[int, typer.Option("--lines", "-n", help="Dosya başına satır.")] = 40,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="tail -F ile izle.")] = False,
) -> None:
    """launchd stdout/stderr ve papi.log son satırları."""
    from papi.run import launchd

    out, err = launchd.log_paths()
    files: list[Path] = [out, err, get_settings().paths.logs / "papi.log"]
    if follow:
        existing = [str(p) for p in files if p.exists()]
        if not existing:
            console.print("Günlük dosyası yok.")
            raise typer.Exit(1)
        os.execvp("tail", ["tail", "-n", str(lines), "-F", *existing])
    for p in files:
        console.print(f"==> {p} <==")
        for line in launchd.tail(p, lines):
            console.print(line)
