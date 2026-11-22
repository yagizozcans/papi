"""``papi web …`` — run the local dashboard / API server."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from papi.core.config import get_settings

app = typer.Typer(help="Yerel pano ve /v1 API.", no_args_is_help=True)


@app.command("serve")
def serve(
    host: str | None = typer.Option(None, "--host", help="Bağlanılacak adres (varsayılan web.host)."),
    port: int | None = typer.Option(None, "--port", help="Port (varsayılan web.port)."),
    log_level: str = typer.Option("info", "--log-level", help="uvicorn log düzeyi."),
) -> None:
    """Start the dashboard (FastAPI + uvicorn). Non-loopback hosts require a bearer token."""
    import uvicorn

    from papi.core.logging import setup_logging
    from papi.web.app import create_app, is_loopback

    settings = get_settings()
    bind = host or settings.web.host
    bind_port = port or settings.web.port
    setup_logging(settings)
    try:
        application = create_app(host=bind)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    note = "" if is_loopback(bind) else " (bearer token zorunlu)"
    typer.echo(f"papi pano: http://{bind}:{bind_port}/{note}")
    uvicorn.run(application, host=bind, port=bind_port, log_level=log_level, proxy_headers=False)


@app.command("openapi")
def openapi(
    out: Path | None = typer.Option(None, "--out", help="Dosyaya yaz (varsayılan stdout)."),
) -> None:
    """Print the /v1 OpenAPI schema as stable JSON (no server, no network)."""
    from papi.web.app import create_app

    text = (
        json.dumps(create_app(host="127.0.0.1").openapi(), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text, encoding="utf-8")
