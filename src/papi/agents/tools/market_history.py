from __future__ import annotations

import math
import re
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from papi.agents.tools import _market
from papi.core.timeutil import ist_date

STEPS = ("day", "week", "month")
INDEX_RE = re.compile(r"^X[A-Z0-9]{4}$")
STOCK_RE = re.compile(r"^[A-Z0-9]{3,6}$")
MAX_POINTS = 260


def market_history(symbol: str, start: str = "", end: str = "", step: str = "week") -> dict[str, Any]:
    """Real daily market data with summary statistics. `symbol` is one of:
    - a BIST stock, e.g. "THYAO": adjusted TRY close, USD-based price, BIST 100 level, USD/TRY and
      market cap per session (İş Yatırım); stats also give the change relative to BIST 100;
    - a BIST index: "XU100", "XU030", "XUTUM", "XULAS" (transport), "XBANK" … (İş Yatırım);
    - a macro series (Yahoo): BRENT, WTI, HEATINGOIL (jet fuel proxy), NATGAS, GOLD, USDTRY, EURTRY,
      EURUSD, DXY, US10Y, VIX, SP500.
    `start`/`end` are YYYY-MM-DD (default: the last 365 days to today). `step` (day|week|month) samples
    the returned points (last session of each period); stats always use every session. Stats: first,
    last, min, max (with dates), change_pct, max_drawdown_pct and annualised volatility. Cite as
    `market_history(<symbol>)` with the dates you used. Never invent a value that is not returned."""
    sym = symbol.strip().upper()
    if step not in STEPS:
        return {"success": False, "error": f"step must be one of {STEPS}"}
    stop = date.fromisoformat(end) if end else ist_date()
    begin = date.fromisoformat(start) if start else stop - timedelta(days=365)
    if begin >= stop:
        return {"success": False, "error": "start must be before end"}
    if sym in _market.MACRO:
        kind, unit = "macro", _market.MACRO[sym][1]
        rows = _market.macro_rows(sym, begin, stop)
    elif INDEX_RE.match(sym):
        kind, unit = "index", "puan"
        rows = _market.index_rows(sym, begin, stop)
    elif STOCK_RE.match(sym):
        kind, unit = "stock", "TRY (bedelsiz/temettüye göre düzeltilmiş)"
        rows = _market.stock_rows(sym, begin, stop)
    else:
        return {"success": False, "error": f"unknown symbol {symbol!r}; macro: {sorted(_market.MACRO)}"}
    if not rows:
        return {"success": False, "error": f"no data for {sym} in {begin}..{stop}"}

    out: dict[str, Any] = {
        "success": True,
        "symbol": sym,
        "kind": kind,
        "unit": unit,
        "from": rows[0]["date"].isoformat(),
        "to": rows[-1]["date"].isoformat(),
        "sessions": len(rows),
        "stats": _stats(rows, "close"),
    }
    if kind == "stock":
        out["stats_usd"] = _stats(rows, "close_usd")
        xu = _stats(rows, "xu100")
        out["stats_xu100"] = xu
        if out["stats"] and xu:
            rel = (1 + out["stats"]["change_pct"] / 100) / (1 + xu["change_pct"] / 100) - 1
            out["relative_to_xu100_pct"] = round(rel * 100, 2)
        last = rows[-1]
        out["latest"] = {
            "date": last["date"].isoformat(),
            "mcap_try_mn": _mn(last["mcap_try"]),
            "mcap_usd_mn": _mn(last["mcap_usd"]),
            "free_float_mcap_try_mn": _mn(last["free_float_mcap_try"]),
            "usdtry": last["usdtry"],
        }
        fields = ("close", "close_usd", "xu100", "usdtry")
    else:
        fields = ("close",)
    points = _sample(rows, step)
    if len(points) > MAX_POINTS:
        return {"success": False, "error": f"{len(points)} points; use a coarser step or a shorter window"}
    out["points"] = [{"date": r["date"].isoformat(), **{f: _round(r.get(f)) for f in fields}} for r in points]
    return out


def _round(v: float | None) -> float | None:
    if v is None:
        return None
    return round(v, 4) if abs(v) < 100 else round(v, 2)


def _mn(v: float | None) -> float | None:
    return None if v is None else round(v / 1e6, 1)


def _stats(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    pts = [(r["date"], r[key]) for r in rows if r.get(key) is not None and r[key] > 0]
    if len(pts) < 2:
        return None
    first, last = pts[0], pts[-1]
    lo = min(pts, key=lambda p: p[1])
    hi = max(pts, key=lambda p: p[1])
    peak, mdd = pts[0][1], 0.0
    for _, v in pts:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [math.log(b[1] / a[1]) for a, b in pairwise(pts)]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / max(1, len(rets) - 1)
    return {
        "first": {"date": first[0].isoformat(), "value": _round(first[1])},
        "last": {"date": last[0].isoformat(), "value": _round(last[1])},
        "min": {"date": lo[0].isoformat(), "value": _round(lo[1])},
        "max": {"date": hi[0].isoformat(), "value": _round(hi[1])},
        "change_pct": round((last[1] / first[1] - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "volatility_annual_pct": round(math.sqrt(var * 252) * 100, 1),
    }


def _sample(rows: list[dict[str, Any]], step: str) -> list[dict[str, Any]]:
    if step == "day":
        return rows

    def key(d: date) -> tuple[int, int]:
        return d.isocalendar()[:2] if step == "week" else (d.year, d.month)

    out: dict[tuple[int, int], dict[str, Any]] = {}
    for r in rows:
        out[key(r["date"])] = r
    return list(out.values())
