from __future__ import annotations

from typing import Any

from papi.core.db import connect
from papi.portfolio import service as portfolio


def portfolio_position(ticker: str = "") -> dict[str, Any]:
    """The user's own position: quantity, average cost, total cost, last close and date, market value
    and portfolio weight. With `ticker` (e.g. "THYAO"): that holding only, or held=false. Without:
    every holding. Use it only to relate a disclosure to what the user owns; never invent numbers."""
    with connect() as conn:
        rows = portfolio.positions(conn)
    want = ticker.strip().upper()
    out = [
        {
            "ticker": p.ticker,
            "quantity": str(p.quantity),
            "avg_cost": str(p.avg_cost) if p.avg_cost is not None else None,
            "cost_total": str(p.cost_total),
            "last_close": str(p.last_close) if p.last_close is not None else None,
            "last_close_date": p.last_close_date.isoformat() if p.last_close_date else None,
            "market_value": str(p.market_value) if p.market_value is not None else None,
            "weight_pct": round(p.weight * 100, 2) if p.weight is not None else None,
        }
        for p in rows
        if not want or p.ticker.upper() == want
    ]
    if want:
        return {"success": True, "held": bool(out), "position": out[0] if out else None}
    return {"success": True, "positions": out}
