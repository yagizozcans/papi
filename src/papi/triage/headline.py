"""Turkish one-line headline for a rules-triaged event (C5 ``extra.headline_tr``)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from papi.triage.taxonomy import Taxonomy

MAX_SUMMARY = 140


def fmt_num(value: Any, max_decimals: int = 2) -> str:
    """Turkish notation: ``1234567.5`` → ``1.234.567,5``."""
    d = Decimal(str(value)).quantize(Decimal(1).scaleb(-max_decimals))
    sign = "-" if d < 0 else ""
    int_part, _, frac = f"{abs(d):f}".partition(".")
    frac = frac.rstrip("0")
    groups = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    out = ".".join(groups)
    return f"{sign}{out},{frac}" if frac else f"{sign}{out}"


def _cur(payload: dict[str, Any]) -> str:
    cur = payload.get("currency")
    return "TL" if cur in (None, "TRY") else str(cur)


def _dividend_cash(p: dict[str, Any]) -> str | None:
    if p.get("gross_per_share") is not None:
        return f"brüt {fmt_num(p['gross_per_share'], 4)} {_cur(p)}/pay"
    if p.get("gross_rate_pct") is not None:
        return f"brüt %{fmt_num(p['gross_rate_pct'])}"
    return None


def _dividend_stock(p: dict[str, Any]) -> str | None:
    return f"%{fmt_num(p['rate_pct'])} pay olarak" if p.get("rate_pct") is not None else None


def _bonus(p: dict[str, Any]) -> str | None:
    if p.get("bonus_rate_pct") is not None:
        return f"%{fmt_num(p['bonus_rate_pct'])} bedelsiz"
    if p.get("target_capital") is not None:
        return f"sermaye {fmt_num(p['target_capital'])} {_cur(p)}"
    return None


def _rights(p: dict[str, Any]) -> str | None:
    parts = []
    if p.get("rights_rate_pct") is not None:
        parts.append(f"%{fmt_num(p['rights_rate_pct'])} bedelli")
    if p.get("subscription_price") is not None:
        parts.append(f"{fmt_num(p['subscription_price'], 4)} {_cur(p)}/pay")
    return ", ".join(parts) or None


def _buyback_program(p: dict[str, Any]) -> str | None:
    if p.get("max_fund_amount") is not None:
        return f"azami fon {fmt_num(p['max_fund_amount'])} {_cur(p)}"
    if p.get("max_shares_nominal") is not None:
        return f"azami {fmt_num(p['max_shares_nominal'])} adet"
    return None


def _buyback_exec(p: dict[str, Any]) -> str | None:
    if p.get("shares_nominal") is None:
        return None
    text = f"{fmt_num(p['shares_nominal'])} adet"
    if p.get("avg_price") is not None:
        text += f", ort. {fmt_num(p['avg_price'], 4)} {_cur(p)}"
    return text


_SIDE_TR = {"buy": "alış", "sell": "satış", "mixed": "alış/satış"}


def _insider(p: dict[str, Any]) -> str | None:
    parts = []
    name = (p.get("person") or {}).get("name")
    if name:
        parts.append(str(name))
    if p.get("side"):
        parts.append(_SIDE_TR.get(p["side"], p["side"]))
    if p.get("total_shares_nominal") is not None:
        parts.append(f"{fmt_num(p['total_shares_nominal'])} adet")
    if p.get("avg_price") is not None:
        parts.append(f"ort. {fmt_num(p['avg_price'], 4)} {_cur(p)}")
    return " ".join(parts) or None


def _threshold(p: dict[str, Any]) -> str | None:
    if p.get("threshold_pct") is None:
        return None
    arrow = "üzerine çıktı" if p.get("direction") == "up" else "altına indi"
    who = f"{p['holder_name']} " if p.get("holder_name") else ""
    return f"{who}%{fmt_num(p['threshold_pct'], 0)} eşiğinin {arrow}"


def _value(p: dict[str, Any]) -> str | None:
    return f"{fmt_num(p['value'])} {_cur(p)}" if p.get("value") is not None else None


def _periodic(p: dict[str, Any]) -> str | None:
    if p.get("period_year") is None:
        return None
    months = p.get("period_months")
    text = f"{p['period_year']}" + (
        f" {months} aylık" if months and months != 12 else " yıllık" if months else ""
    )
    if p.get("consolidated") is True:
        text += " (konsolide)"
    elif p.get("consolidated") is False:
        text += " (konsolide olmayan)"
    return text


_KEY_VALUE: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "distribution.dividend.cash": _dividend_cash,
    "distribution.dividend.stock": _dividend_stock,
    "capital.increase.bonus": _bonus,
    "capital.increase.rights": _rights,
    "buyback.program.start": _buyback_program,
    "buyback.execution": _buyback_exec,
    "ownership.insider_trade": _insider,
    "ownership.threshold_crossing": _threshold,
    "business.contract.award": _value,
    "mna.acquisition": _value,
    "mna.disposal": _value,
    "report.financial.periodic": _periodic,
}


def key_value(code: str, payload: dict[str, Any] | None) -> str | None:
    fn = _KEY_VALUE.get(code)
    return fn(payload) if fn and payload else None


def headline(
    taxonomy: Taxonomy,
    code: str,
    *,
    tickers: list[str],
    issuer_name: str | None,
    payload: dict[str, Any] | None,
    summary: str | None,
    subject: str | None,
) -> str:
    """``TICKER: <Turkish label> — <key payload value | KAP summary>``."""
    who = "/".join(tickers) if tickers else (issuer_name or "")
    label = taxonomy.label(code, "tr") if code != "other.unclassified" else (subject or taxonomy.label(code))
    detail = key_value(code, payload)
    if detail is None:
        s = " ".join((summary or "").split())
        if s and s.casefold() != (subject or "").casefold():
            detail = s if len(s) <= MAX_SUMMARY else s[: MAX_SUMMARY - 1].rstrip() + "…"
    text = f"{who}: {label}" if who else label
    return f"{text} — {detail}" if detail else text
