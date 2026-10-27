"""Cap calibration against the claude.ai usage bars (04 §6, Max 5x profile).

1. ``papi llm calibrate --start`` stores a marker (now) in ``kv_state``.
2. Let papi work (week-1 calibration mode: CHAT/P1/P2 only) and note how far the claude.ai →
   Settings → Usage bars moved because of papi.
3. ``papi llm calibrate --bar-5h-pct X --bar-week-pct Y`` divides the ledger cost since the marker
   by the observed movement → cost-equivalent per 1 % of each bar, then sets the week-2 caps:
   ``share_5h`` = 15 % of the 5 h bar, ``share_week`` = 20 % of the weekly bar, calibration off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, func, select

from papi.core.schema import llm_usage_ledger
from papi.core.state import get_state, set_state
from papi.core.timeutil import ist_date, now_utc, parse_iso
from papi.llm.governor import CALIBRATION_MARKER_KEY
from papi.llm.tomlconf import update_llm_config

PROFILE_5H_PCT = 15.0
PROFILE_WEEK_PCT = 20.0


@dataclass
class Calibration:
    since: datetime
    ledger_cost: float
    calls: int
    cost_per_pct_5h: float | None
    cost_per_pct_week: float | None
    share_5h: float | None
    share_week: float | None
    calibrated_at: date

    def config_values(self) -> dict[str, object]:
        values: dict[str, object] = {"calibration_mode": False, "calibrated_at": self.calibrated_at}
        if self.cost_per_pct_5h is not None:
            values["calibration_cost_per_pct_5h"] = round(self.cost_per_pct_5h, 6)
            values["share_5h"] = round(self.share_5h or 0.0, 6)
        if self.cost_per_pct_week is not None:
            values["calibration_cost_per_pct_week"] = round(self.cost_per_pct_week, 6)
            values["share_week"] = round(self.share_week or 0.0, 6)
        return values


class CalibrationError(ValueError):
    pass


def start(conn: Connection, *, now: datetime | None = None) -> datetime:
    now = now or now_utc()
    set_state(conn, CALIBRATION_MARKER_KEY, {"at": now.isoformat()})
    return now


def marker(conn: Connection) -> datetime | None:
    raw = get_state(conn, CALIBRATION_MARKER_KEY)
    return parse_iso(raw["at"]) if raw and raw.get("at") else None


def compute(
    conn: Connection,
    *,
    bar_5h_pct: float | None,
    bar_week_pct: float | None,
    now: datetime | None = None,
) -> Calibration:
    """Cost-per-percent from ledger usage since the marker and the observed bar movement."""
    if bar_5h_pct is None and bar_week_pct is None:
        raise CalibrationError("--bar-5h-pct veya --bar-week-pct gerekli.")
    for name, pct in (("--bar-5h-pct", bar_5h_pct), ("--bar-week-pct", bar_week_pct)):
        if pct is not None and not 0 < pct <= 100:
            raise CalibrationError(f"{name} 0 ile 100 arasında olmalı (yüzde puan).")
    since = marker(conn)
    if since is None:
        raise CalibrationError("Kalibrasyon başlangıcı yok: önce `papi llm calibrate --start` çalıştırın.")
    now = now or now_utc()
    cost, calls = conn.execute(
        select(func.coalesce(func.sum(llm_usage_ledger.c.cost_usd_est), 0.0), func.count()).where(
            llm_usage_ledger.c.at >= since, llm_usage_ledger.c.at <= now
        )
    ).one()
    cost = float(cost)
    if cost <= 0:
        raise CalibrationError(
            "Başlangıçtan beri defterde maliyet yok; önce papi birkaç Claude çağrısı yapmalı."
        )
    cpp_5h = cost / bar_5h_pct if bar_5h_pct else None
    cpp_week = cost / bar_week_pct if bar_week_pct else None
    return Calibration(
        since=since,
        ledger_cost=cost,
        calls=int(calls),
        cost_per_pct_5h=cpp_5h,
        cost_per_pct_week=cpp_week,
        share_5h=PROFILE_5H_PCT * cpp_5h if cpp_5h else None,
        share_week=PROFILE_WEEK_PCT * cpp_week if cpp_week else None,
        calibrated_at=ist_date(now),
    )


def apply(cal: Calibration) -> None:
    """Write the calibrated caps to ``config.toml`` (calibration mode off)."""
    update_llm_config(cal.config_values())
