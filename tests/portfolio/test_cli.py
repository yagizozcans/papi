from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from rich.console import Console
from sqlalchemy import Connection, select
from typer.testing import CliRunner

from papi.core import schema as s
from papi.portfolio import cli, service
from papi.portfolio.cli import app
from tests.portfolio.conftest import add_close, add_corporate_action, add_instrument

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "console", Console(width=300, soft_wrap=True, markup=False, highlight=False))


@pytest.fixture
def seeded(db: Connection) -> dict[str, str]:
    ids = {
        "THYAO": add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.", "TÜRK HAVA YOLLARI"),
        "ASELS": add_instrument(db, "ASELS", "ASELSAN ELEKTRONİK SANAYİ VE TİCARET A.Ş.", "ASELSAN"),
    }
    add_close(db, ids["THYAO"], date(2026, 9, 30), 280.0)
    db.commit()
    return ids


def _run(*args: str, input: str | None = None) -> str:
    result = runner.invoke(app, list(args), input=input)
    assert result.exit_code == 0, result.output
    return result.output


def test_add_list_txns_undo(db: Connection, seeded: dict[str, str]) -> None:
    out = _run("add", "BUY", "thyao", "100", "@", "285.40", "2026-10-01", "--fees", "4,20")
    assert "ALIŞ THYAO 100 @ 285,4 · 01.10.2026 · komisyon 4,20" in out
    assert "Kaydedildi" in out and "THYAO: 100 pay, ort. maliyet 285,44" in out
    out = _run("add", "AL", "ASELS", "10", "lot", "@62,5", "01.10.2026", "--yes")
    assert "Son kapanış fiyatı yok" in out

    out = _run("list")
    assert "THYAO" in out and "28.544,20" in out and "280,00 (30.09.2026)" in out and "ASELS" in out

    db.rollback()
    tid = db.execute(select(s.txn.c.id).where(s.txn.c.instrument_id == seeded["ASELS"])).scalar_one()
    out = _run("undo", tid[-8:], "--reason", "deneme")
    assert f"Geri alındı: {tid}" in out
    out = _run("txns")
    assert "geri alındı" in out and "ters kayıt" in out

    bad = runner.invoke(app, ["add", "BUY", "THYA", "1", "1"])
    assert bad.exit_code == 1 and "Bilinmeyen hisse kodu: THYA." in bad.output
    declined = runner.invoke(app, ["add", "SELL", "THYAO", "500", "290"], input="n\n")
    assert declined.exit_code == 1 and "eldeki adetten" in declined.output


def test_import_dry_run_then_import_then_reimport(
    db: Connection, seeded: dict[str, str], tmp_path: Path
) -> None:
    out = _run("template")
    assert out.startswith("ticker,type,quantity,price,date,fees,note")
    path = tmp_path / "holdings.csv"
    path.write_text(out, encoding="utf-8")

    out = _run("import", str(path), "--dry-run")
    assert "4 yeni, 0 zaten kayıtlı, 0 hatalı satır." in out
    db.rollback()
    assert db.execute(select(s.txn.c.id)).first() is None

    out = _run("import", str(path), "--yes")
    assert "4 işlem kaydedildi." in out
    out = _run("import", str(path), "--yes")
    assert "0 yeni, 4 zaten kayıtlı" in out and "Eklenecek yeni satır yok." in out


def test_reconcile_file_and_interactive(db: Connection, seeded: dict[str, str], tmp_path: Path) -> None:
    _run("add", "BUY", "THYAO", "100", "285", "2026-10-01", "--yes")
    qfile = tmp_path / "iscep.csv"
    qfile.write_text("hisse;adet\nTHYAO;120\nASELS;5\n", encoding="utf-8")
    out = runner.invoke(app, ["reconcile", "--file", str(qfile)], input="n\n").output
    assert "Mutabakat farkları" in out and "+20" in out and "Kaydedilmedi." in out

    out = _run("reconcile", "--file", str(qfile), "--yes")
    assert "2 mutabakat düzeltmesi kaydedildi." in out
    out = _run("reconcile", "--file", str(qfile))
    assert "Fark yok" in out

    # interactive: accept THYAO, change ASELS, no extra tickers, then decline
    out = runner.invoke(app, ["reconcile"], input="120\n7\n\nn\n").output
    assert "ASELS" in out and "+2" in out


def test_pending_apply_dismiss_and_watch(db: Connection, seeded: dict[str, str]) -> None:
    _run("add", "BUY", "THYAO", "1200", "10", "2026-09-01", "--yes")
    db.rollback()
    ca = add_corporate_action(db, seeded["THYAO"], "bonus", date(2026, 10, 1), factor="1")
    ca2 = add_corporate_action(db, seeded["THYAO"], "dividend_cash", date(2026, 10, 2), cash_amount="1")
    [pa] = service.propose_for_corporate_action(db, ca)
    [pa2] = service.propose_for_corporate_action(db, ca2)
    db.commit()
    out = _run("pending")
    assert "1.200 → 2.400 pay" in out
    assert "Uygulandı" in _run("apply", pa)
    assert "Reddedildi" in _run("dismiss", pa2)
    assert "Bekleyen kurumsal işlem yok." in _run("pending")
    assert "THYAO" in _run("list") and "2.400" in _run("list")

    assert "eklendi" in _run("watch", "ASELS")
    assert "eklendi" in _run("watch", "sektor:Savunma", "--list", "Savunma")
    out = _run("watchlists")
    assert "ASELS" in out and "Savunma" in out
    assert "Çıkarıldı" in _run("unwatch", "asels")
    bad = runner.invoke(app, ["watch", "ASELSX"])
    assert bad.exit_code == 1 and "Bilinmeyen hisse kodu" in bad.output


def test_add_parses_quantity_decimal_and_rejects_bad_grammar(db: Connection, seeded: dict[str, str]) -> None:
    bad = runner.invoke(app, ["add", "BUY", "THYAO"])
    assert bad.exit_code == 1 and "Kullanım" in bad.output
    bad = runner.invoke(app, ["add", "XXX", "THYAO", "1", "2"])
    assert bad.exit_code == 1 and "bilinmeyen işlem türü" in bad.output
    out = _run("add", "FEE", "12,5", "2026-10-01")
    assert "MASRAF" in out and "tutar 12,50" in out
    db.rollback()
    row = db.execute(select(s.txn).where(s.txn.c.type == "fee")).one()
    assert row.fees == Decimal("12.5") and row.instrument_id is None
