from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Connection
from typer.testing import CliRunner

from papi.core.timeutil import ist_date
from papi.sources.prices import cli, service
from tests.prices.conftest import add_bar, add_instrument

runner = CliRunner()


def test_show_and_check(db: Connection) -> None:
    thy = add_instrument(db, "THYAO")
    d1, d2 = ist_date() - timedelta(days=2), ist_date() - timedelta(days=1)
    add_bar(db, thy, d1, "isyatirim_prices", 100.0, high=101.0, low=99.0, value_traded=5e9)
    add_bar(db, thy, d1, "yahoo_prices", 103.0)
    add_bar(db, thy, d2, "isyatirim_prices", 104.5)
    db.commit()
    res = runner.invoke(cli.app, ["check", "--days", "5"])
    assert res.exit_code == 0, res.output
    assert "uyumsuz 1" in res.output and "tek kaynak 1" in res.output and "THYAO" in res.output
    res = runner.invoke(cli.app, ["show", "THYAO", "--days", "5"])
    assert res.exit_code == 0, res.output
    assert "Son kapanış: 104,50" in res.output and "mismatch" in res.output
    assert runner.invoke(cli.app, ["show", "NOPE"]).exit_code == 1


def test_sync_command(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    thy = add_instrument(db, "THYAO")
    db.commit()
    seen: list[Any] = []

    def fake(ids: list[str], start: Any, end: Any = None, **kw: Any) -> dict:
        seen.append((ids, start, end))
        return {
            "instruments": ["THYAO", "XU100"],
            "rows": {"isyatirim_prices": 2, "yahoo_prices": 2},
            "errors": [],
            "cross_check": {
                "primary": "isyatirim_prices",
                "secondary": "yahoo_prices",
                "tolerance_pct": 0.5,
                "counts": {"ok": 2, "mismatch": 0, "single_source": 0},
                "mismatches": [],
                "missing_primary": [],
                "missing_secondary": [],
            },
        }

    monkeypatch.setattr(service, "sync_prices_isolated", fake)
    res = runner.invoke(
        cli.app, ["sync", "--tickers", "thyao,XU100,NOPE", "--from", "2026-10-01", "--to", "2026-10-27"]
    )
    assert res.exit_code == 0, res.output
    assert "Bilinmeyen sembol(ler): NOPE" in res.output and "uyumlu 2" in res.output
    ((ids, start, end),) = seen
    assert ids[0] == thy and len(ids) == 2 and str(start) == "2026-10-01" and str(end) == "2026-10-27"
    assert runner.invoke(cli.app, ["sync", "--tickers", "NOPE"]).exit_code == 1
