"""Rules materiality 0–100 and band (PRD 04 §3). Formula documented in ``README.md``."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, bindparam, select, text

from papi.core.schema import instrument, issuer, price_bar
from papi.core.timeutil import ist_date
from papi.core.types import UtcDateTime
from papi.triage.taxonomy import Taxonomy

NOVELTY_WINDOW = timedelta(days=365)
NOVELTY_MULT = 1.15
LATE_MULT = 1.10
CORRECTION_MULT = 0.80

# (upper bound exclusive, multiplier); last bound is +inf
CAP_RATIO_STEPS = ((0.001, 0.6), (0.01, 0.85), (0.05, 1.1), (0.10, 1.3), (float("inf"), 1.5))
YIELD_STEPS = ((0.01, 0.8), (0.03, 1.0), (0.06, 1.2), (float("inf"), 1.4))
RATE_STEPS = ((0.25, 0.9), (1.0, 1.1), (3.0, 1.3), (float("inf"), 1.5))

BANDS = ((30, "low"), (60, "medium"), (80, "high"))  # score < bound → band; else critical

# payload key holding the money value compared with market cap, per code
_VALUE_KEYS = {
    "business.contract.award": "value",
    "mna.acquisition": "value",
    "mna.disposal": "value",
    "buyback.program.start": "max_fund_amount",
    "buyback.execution": "total_amount",
    "ownership.insider_trade": "total_amount",
}
# payload key with a nominal share count, used (× last close) when the money value is not stated
_SHARE_KEYS = {
    "buyback.execution": "shares_nominal",
    "ownership.insider_trade": "total_shares_nominal",
}
_RATE_KEYS = {
    "capital.increase.bonus": "bonus_rate_pct",
    "capital.increase.rights": "rights_rate_pct",
    "distribution.dividend.stock": "rate_pct",
}


@dataclass(frozen=True)
class Materiality:
    score: int
    band: str
    prior: int
    size: float
    size_basis: str | None
    novelty: float
    flags: float
    flag_names: tuple[str, ...]
    catchup: bool

    def explain(self) -> dict[str, Any]:
        d = asdict(self)
        d["flag_names"] = list(self.flag_names)
        return d


def band_for(score: int) -> str:
    for bound, name in BANDS:
        if score < bound:
            return name
    return "critical"


def _step(value: float, steps: tuple[tuple[float, float], ...]) -> float:
    for bound, mult in steps:
        if value < bound:
            return mult
    return steps[-1][1]


def last_close(conn: Connection, instrument_id: str, on_or_before: datetime | None) -> float | None:
    q = select(price_bar.c.close).where(
        price_bar.c.instrument_id == instrument_id,
        price_bar.c.interval == "1d",
        price_bar.c.close.is_not(None),
    )
    if on_or_before is not None:
        q = q.where(price_bar.c.ts <= ist_date(on_or_before))
    row = conn.execute(q.order_by(price_bar.c.ts.desc()).limit(1)).first()
    return float(row[0]) if row else None


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def shares_outstanding(conn: Connection, instrument_id: str) -> float | None:
    """Share count from ``instrument.extra.shares_outstanding`` or the issuer's paid-in capital
    (``issuer.extra.shares_outstanding`` / ``paid_in_capital``; BIST nominal is 1 TL per share)."""
    row = conn.execute(
        select(instrument.c.extra, issuer.c.extra)
        .select_from(instrument.outerjoin(issuer, instrument.c.issuer_id == issuer.c.id))
        .where(instrument.c.id == instrument_id)
    ).first()
    if row is None:
        return None
    inst_extra, iss_extra = row[0] or {}, row[1] or {}
    for value in (
        inst_extra.get("shares_outstanding"),
        iss_extra.get("shares_outstanding"),
        iss_extra.get("paid_in_capital"),
    ):
        num = _num(value)
        if num:
            return num
    return None


def market_cap(
    conn: Connection, instrument_id: str, at: datetime | None, payload: dict[str, Any] | None = None
) -> tuple[float | None, float | None]:
    """(market cap TRY, last close). Capital-event payloads may supply the share count themselves."""
    close = last_close(conn, instrument_id, at)
    if close is None:
        return None, None
    shares = shares_outstanding(conn, instrument_id)
    if shares is None and payload:
        shares = _num(payload.get("current_capital"))
    return (close * shares if shares else None), close


def size_signal(
    conn: Connection,
    code: str,
    payload: dict[str, Any] | None,
    instrument_ids: list[str],
    at: datetime | None,
) -> tuple[float, str | None]:
    """(multiplier, basis). 1.0 / None when no size signal is available."""
    if not payload:
        return 1.0, None
    if code in _RATE_KEYS:
        rate = _num(payload.get(_RATE_KEYS[code]))
        if rate is not None:
            return _step(rate / 100.0, RATE_STEPS), f"rate={rate:g}%"
        return 1.0, None
    primary = instrument_ids[0] if instrument_ids else None
    if primary is None:
        return 1.0, None
    if code == "distribution.dividend.cash":
        dps = _num(payload.get("gross_per_share"))
        if dps is None or payload.get("currency") not in (None, "TRY"):
            return 1.0, None
        close = last_close(conn, primary, at)
        if not close:
            return 1.0, None
        y = dps / close
        return _step(y, YIELD_STEPS), f"yield={y:.4f}"
    key = _VALUE_KEYS.get(code)
    if key is None:
        return 1.0, None
    value = _num(payload.get(key))
    if payload.get("currency") not in (None, "TRY"):
        return 1.0, None
    cap, close = market_cap(conn, primary, at, payload)
    if value is None and code in _SHARE_KEYS and close:
        shares = _num(payload.get(_SHARE_KEYS[code]))
        value = shares * close if shares else None  # nominal 1 TL per share → shares × last close
    if value is None or not cap:
        return 1.0, None
    r = value / cap
    return _step(r, CAP_RATIO_STEPS), f"value/mcap={r:.5f}"


def is_novel(
    conn: Connection,
    family: str,
    issuer_ids: list[str],
    at: datetime | None,
    *,
    exclude_disclosure_ids: list[str] | None = None,
) -> bool:
    """True when none of ``issuer_ids`` had an event of ``family`` in the 365 days before ``at``."""
    if not issuer_ids or at is None:
        return False
    excluded = list(exclude_disclosure_ids or [])
    q = text(
        "SELECT 1 FROM event e, json_each(e.issuer_ids) j"
        " WHERE e.code LIKE :fam AND e.announced_at < :at AND e.announced_at >= :since"
        " AND j.value IN :issuers"
        " AND (e.primary_disclosure_id IS NULL OR e.primary_disclosure_id NOT IN :excluded) LIMIT 1"
    ).bindparams(
        bindparam("at", type_=UtcDateTime()),
        bindparam("since", type_=UtcDateTime()),
        bindparam("issuers", expanding=True),
        bindparam("excluded", expanding=True),
    )
    row = conn.execute(
        q,
        {
            "fam": f"{family}.%",
            "at": at,
            "since": at - NOVELTY_WINDOW,
            "issuers": list(issuer_ids),
            "excluded": excluded or ["-"],
        },
    ).first()
    return row is None


def compute(
    conn: Connection,
    taxonomy: Taxonomy,
    code: str,
    *,
    payload: dict[str, Any] | None,
    issuer_ids: list[str],
    instrument_ids: list[str],
    announced_at: datetime | None,
    is_late: bool,
    revision_kind: str | None,
    is_catchup: bool,
    exclude_disclosure_ids: list[str] | None = None,
) -> Materiality:
    ec = taxonomy.get(code)
    prior = ec.prior if ec else taxonomy.families[taxonomy.family_of(code)].prior
    size, basis = size_signal(conn, code, payload, instrument_ids, announced_at)
    novelty = 1.0
    if (
        ec is not None
        and not ec.recurring
        and is_novel(conn, ec.family, issuer_ids, announced_at, exclude_disclosure_ids=exclude_disclosure_ids)
    ):
        novelty = NOVELTY_MULT
    flags = 1.0
    names: list[str] = []
    if is_late:
        flags *= LATE_MULT
        names.append("late")
    if revision_kind == "correction":
        flags *= CORRECTION_MULT
        names.append("correction")
    score = round(max(0.0, min(100.0, prior * size * novelty * flags)))
    return Materiality(
        score, band_for(score), prior, size, basis, novelty, round(flags, 4), tuple(names), is_catchup
    )
