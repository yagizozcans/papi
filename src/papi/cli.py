"""``papi`` command line.

Core commands: ``init``, ``version``, ``db migrate``, ``db current``, ``config show``, ``config path``.
Slice command groups are mounted from ``papi.<module>.cli:app`` (Typer) when the module exists; see
``SUBCOMMAND_MODULES``. Only a missing ``papi.<module>.cli`` module is tolerated; any other import
error propagates.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import typer
from rich.console import Console

from papi import __version__
from papi.core.config import get_settings, get_telegram_token, get_web_token

app = typer.Typer(name="papi", help="papi — personal BIST/KAP disclosure intelligence.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
config_app = typer.Typer(help="Configuration.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")

console = Console(soft_wrap=True, markup=False, highlight=False)

# (module under papi., command group name)
SUBCOMMAND_MODULES: tuple[tuple[str, str], ...] = (
    ("portfolio", "portfolio"),
    ("sources.kap", "kap"),
    ("sources.prices", "prices"),
    ("kb", "kb"),
    ("triage", "triage"),
    ("llm", "llm"),
    ("telegram", "telegram"),
    ("web", "web"),
    ("run", "run"),
    ("evals", "eval"),
    ("analyst", "analyst"),
)


def mount_subcommands(root: typer.Typer) -> list[str]:
    """Add each available ``papi.<module>.cli.app`` to ``root``; returns mounted group names."""
    mounted: list[str] = []
    for module, name in SUBCOMMAND_MODULES:
        modname = f"papi.{module}.cli"
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            if exc.name != modname:
                raise
            continue
        root.add_typer(mod.app, name=name)
        mounted.append(name)
    return mounted


@app.command()
def init() -> None:
    """Create ~/.papi (or $PAPI_HOME): dirs, default config, database, seed rows. Safe to re-run."""
    from papi.core.bootstrap import init_home

    report = init_home()
    s = get_settings()
    if not report.changed:
        console.print(f"papi home already initialised at {s.paths.home} (revision {report.revision_after}).")
        return
    for d in report.created_dirs:
        console.print(f"created {d}")
    if report.config_written:
        console.print(f"wrote default config {s.config_path}")
    if report.revision_before != report.revision_after:
        console.print(
            f"database {s.paths.db} migrated {report.revision_before or 'empty'} → {report.revision_after}"
        )
    seeded = {k: v for k, v in report.seeded.items() if v}
    if seeded:
        console.print("seeded " + ", ".join(f"{k}={v}" for k, v in seeded.items()))


@app.command()
def version() -> None:
    """Print the papi version."""
    console.print(f"papi {__version__}")


@db_app.command("migrate")
def db_migrate() -> None:
    """Upgrade the database schema to the latest revision."""
    from papi.core import db

    before = db.current_revision()
    db.migrate()
    after = db.current_revision()
    console.print(f"{get_settings().paths.db}: {before or 'empty'} → {after}")


@db_app.command("current")
def db_current() -> None:
    """Show the database's current Alembic revision."""
    from papi.core import db

    console.print(db.current_revision() or "empty")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


@config_app.command("show")
def config_show(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """Print the effective configuration (secrets reported as set/unset only)."""
    s = get_settings()
    data = _jsonable(s.model_dump(mode="json"))
    data["secrets"] = {
        "telegram_token": "set" if get_telegram_token() else "unset",
        "web_token": "set" if get_web_token() else "unset",
    }
    if data.get("web", {}).get("token"):
        data["web"]["token"] = "***"
    if as_json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        console.print_json(data=data)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file path."""
    typer.echo(str(get_settings().config_path))


mount_subcommands(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
