from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from papi.agents.tools import _market
from papi.core.timeutil import ist_date

GROUP = "XI_29"  # İş Yatırım layout of industrial companies (banks/insurers use other layouts)
# P&L and cash-flow items are reported year-to-date (YTD).
FLOW_ITEMS = {
    "revenue": "3C",
    "gross_profit": "3D",
    "selling_expenses": "3DA",
    "admin_expenses": "3DB",
    "rnd_expenses": "3DC",
    "operating_profit": "3DF",
    "pretax_profit": "3I",
    "net_income_parent": "3Z",
    "depreciation": "4B",
    "domestic_sales": "4BC",
    "foreign_sales": "4BD",
}
STOCK_ITEMS = {
    "cash": "1AA",
    "st_financial_investments": "1AB",
    "total_assets": "1BL",
    "financial_debt_st": "2AA",
    "financial_debt_lt": "2BA",
    "equity_parent": "2O",
    "net_fx_position": "4BE",
}
NOTES = [
    "Çeyrek değerleri ara dönem raporlarının yıl başından itibaren (YTD) farkıdır; 4. çeyrek "
    "hesaplanmaz çünkü yıllık rapor ara raporlarla aynı ölçüde değildir (enflasyon düzeltmesi / "
    "yeniden ifade): yıllık rakamlar 'annual' altında, olduğu gibi ve dikkatle kullanılmalı.",
    "Karşılaştırma için en güvenilir alan 'ytd': aynı dönemin geçen yılla kıyası.",
    "TMS 29 (enflasyon muhasebesi) uygulayan şirketlerde TRY rakamlar her raporun kendi dönem sonu "
    "fiyatlarıyla ifade edilir; uzun TRY serilerini doğrudan kıyaslama.",
]


def company_financials(ticker: str, quarters: int = 6, currency: str = "TRY") -> dict[str, Any]:
    """Real financial statements of a BIST industrial company (İş Yatırım tables of its KAP
    financial reports) in millions of `currency` ("TRY" or "USD", İş Yatırım's conversion).
    Returns: `ytd` — latest year-to-date period vs the same period a year earlier (revenue, EBITDA,
    net income to parent, margins, change %; the most reliable comparison); `quarters` — single
    Q1–Q3 values derived from YTD figures, newest first; `annual` — full-year figures as reported;
    `balance` — cash, financial debt, net debt (incl. leases), parent equity, net FX position per
    period end; `valuation` — market cap at the latest close, P/B, and P/E and EV/EBITDA on the last
    full year. EBITDA = gross profit − selling, admin and R&D expenses + depreciation. Read `notes`
    before comparing periods. Banks and insurers are not supported. Cite as
    `company_financials(<ticker>, <currency>) <period>`."""
    code = ticker.strip().upper()
    cur = currency.strip().upper()
    if cur not in ("TRY", "USD"):
        return {"success": False, "error": "currency must be TRY or USD"}
    n = max(1, min(int(quarters), 10))
    periods = _periods_back(ist_date(), -(-max(n + 2, 6) // 4) * 4)
    values = _fetch(code, periods, cur)
    available = [p for p in periods if values.get(("1BL", p))]
    if not available:
        return {
            "success": False,
            "error": f"no industrial (XI_29) statements for {code}; banks/insurers are not supported",
        }

    def ytd_row(p: tuple[int, int]) -> dict[str, float | None]:
        return {name: values.get((item, p)) for name, item in FLOW_ITEMS.items()}

    def quarter_row(p: tuple[int, int]) -> dict[str, float | None] | None:
        if p[1] == 12:
            return None
        cur_ytd = ytd_row(p)
        if p[1] == 3:
            return cur_ytd
        prev = ytd_row((p[0], p[1] - 3))
        return {k: None if v is None or prev[k] is None else v - prev[k] for k, v in cur_ytd.items()}

    quarters_out = []
    for p in available:
        row = quarter_row(p)
        if row is not None and len(quarters_out) < n:
            quarters_out.append({"period": _label(p), **_present(row)})

    latest = available[0]
    out: dict[str, Any] = {
        "success": True,
        "ticker": code,
        "currency": cur,
        "unit": f"milyon {cur}",
        "latest_period": _label(latest),
        "notes": NOTES,
    }
    prior = (latest[0] - 1, latest[1])
    if values.get(("1BL", prior)):
        now_p, then_p = _present(ytd_row(latest)), _present(ytd_row(prior))
        out["ytd"] = {
            "period": _label(latest, ytd=True),
            "prior_period": _label(prior, ytd=True),
            "current": now_p,
            "prior": then_p,
            "change_pct": {
                k: _change(now_p[k], then_p[k])
                for k in ("revenue", "ebitda", "operating_profit", "net_income_parent")
            },
        }
    out["quarters"] = quarters_out
    out["annual"] = [{"period": f"{p[0]}FY", **_present(ytd_row(p))} for p in available if p[1] == 12]
    out["balance"] = [{"period": _label(p), **_balance(values, p)} for p in available[:n]]
    out["valuation"] = _valuation(code, cur, out["annual"][0] if out["annual"] else None, out["balance"][0])
    return out


def _fetch(code: str, periods: list[tuple[int, int]], cur: str) -> dict[tuple[str, tuple[int, int]], float]:
    wanted = set(FLOW_ITEMS.values()) | set(STOCK_ITEMS.values())
    values: dict[tuple[str, tuple[int, int]], float] = {}
    for i in range(0, len(periods), 4):
        batch = periods[i : i + 4]
        for row in _market.financial_rows(code, batch, cur, GROUP):
            if row.get("itemCode") not in wanted:
                continue
            for k, p in enumerate(batch, start=1):
                v = row.get(f"value{k}")
                if v not in (None, ""):
                    values[(row["itemCode"], p)] = float(v)
    return values


def _periods_back(today: date, count: int) -> list[tuple[int, int]]:
    """Quarter ends (year, month) up to the last one before ``today``, newest first."""
    y, m = today.year, (today.month - 1) // 3 * 3
    if m == 0:
        y, m = y - 1, 12
    out = []
    for _ in range(count):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 3 else (y, m - 3)
    return out


def _label(p: tuple[int, int], *, ytd: bool = False) -> str:
    return f"{p[0]} {p[1]} ay" if ytd else f"{p[0]}Q{p[1] // 3}"


def _present(row: dict[str, float | None]) -> dict[str, float | None]:
    """Millions, EBITDA and margins; expense lines folded into EBITDA."""
    parts = [row["gross_profit"], row["selling_expenses"], row["admin_expenses"], row["depreciation"]]
    ebitda = None if None in parts else sum(parts) + (row["rnd_expenses"] or 0.0)  # type: ignore[arg-type]
    out = {k: _mn(v) for k, v in row.items() if not k.endswith("_expenses")}
    out["ebitda"] = _mn(ebitda)
    rev = row["revenue"]
    out["gross_margin_pct"] = _pct(row["gross_profit"], rev)
    out["ebitda_margin_pct"] = _pct(ebitda, rev)
    out["net_margin_pct"] = _pct(row["net_income_parent"], rev)
    return out


def _balance(values: dict[tuple[str, tuple[int, int]], float], p: tuple[int, int]) -> dict[str, Any]:
    row = {name: values.get((item, p)) for name, item in STOCK_ITEMS.items()}
    parts = [row["financial_debt_st"], row["financial_debt_lt"], row["cash"], row["st_financial_investments"]]
    net_debt = None if None in parts else parts[0] + parts[1] - parts[2] - parts[3]  # type: ignore[operator]
    return {**{k: _mn(v) for k, v in row.items()}, "net_debt": _mn(net_debt)}


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else round(a / b * 100, 1)


def _change(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b or b <= 0 else round((a / b - 1) * 100, 1)


def _mn(v: float | None) -> float | None:
    return None if v is None else round(v / 1e6, 1)


def _valuation(code: str, cur: str, fy: dict[str, Any] | None, bal: dict[str, Any]) -> dict[str, Any] | None:
    end = ist_date()
    rows = _market.stock_rows(code, end - timedelta(days=10), end)
    if not rows:
        return None
    last = rows[-1]
    mcap = _mn(last["mcap_usd"] if cur == "USD" else last["mcap_try"])
    if mcap is None:
        return None

    def ratio(a: float | None, b: float | None) -> float | None:
        return None if a is None or not b or b <= 0 else round(a / b, 2)

    net_debt = bal.get("net_debt")
    out: dict[str, Any] = {
        "date": last["date"].isoformat(),
        "market_cap": mcap,
        "pb": ratio(mcap, bal.get("equity_parent")),
        "balance_period": bal["period"],
    }
    if fy:
        out |= {
            "fy_period": fy["period"],
            "pe_fy": ratio(mcap, fy["net_income_parent"]),
            "ev_ebitda_fy": ratio(None if net_debt is None else mcap + net_debt, fy["ebitda"]),
            "note": "F/K ve FD/FAVÖK son yıllık rapora göre (yıllık rapor notuna bak); "
            "net borç kiraları içerir",
        }
    return out
