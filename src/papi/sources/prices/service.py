"""Price sync, cross-check and lookups (contract C7).

Flow of a sync: resolve tickers (DB read) → fetch every source sequentially (network, no DB writes)
→ store raw responses + upsert ``price_bar`` rows → cross-check primary vs secondary closes on the
window → metrics + ops alert on problems.

``sync_prices(conn, ...)`` runs all of it on the caller's connection (C7). Long network phases inside
one SQLite transaction hold a read snapshot, so background code uses :func:`sync_prices_isolated`,
which reads, fetches and writes in separate short transactions.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import Connection, and_, select
from sqlalchemy.dialects.sqlite import insert

from papi.core import db, metrics, rawstore
from papi.core.config import Settings, get_settings
from papi.core.ids import uuid7
from papi.core.logging import get_logger
from papi.core.schema import (
    OPEN_START,
    identifier,
    instrument,
    price_bar,
    source,
    trading_calendar,
    txn,
    watchlist,
)
from papi.core.timeutil import ist_date, now_utc
from papi.sources.prices import isyatirim, tefas, yahoo
from papi.sources.prices.base import FETCHER_VERSION, Fetched, Throttle, default_throttle, make_client

log = get_logger("prices")

INTERVAL = "1d"
MARKET_INDEX = "XU100"
INDEX_NAMES = {"XU100": "BIST 100", "XU030": "BIST 30", "XU050": "BIST 50", "XUTUM": "BIST Tüm"}
INDEX_TICKER_RE = re.compile(r"^X[A-Z0-9]{4}$")
CALENDAR_ID = "XIST"
DAILY_WINDOW_DAYS = 10
DAILY_STATE_KEY = "prices:daily_last_date"

Fetcher = Callable[..., list[Fetched]]
FETCHERS: dict[str, Fetcher] = {isyatirim.SOURCE_CODE: isyatirim.fetch, yahoo.SOURCE_CODE: yahoo.fetch}

# txn types that add / remove shares (sign taken from the type, magnitude from |quantity|);
# ``split`` and ``reconcile_adjustment`` carry a signed quantity delta.
_TXN_PLUS = {"buy", "bonus_shares", "rights_subscription", "transfer_in", "opening_balance"}
_TXN_MINUS = {"sell", "transfer_out"}
_TXN_SIGNED = {"split", "reconcile_adjustment"}


@dataclass(frozen=True)
class Target:
    instrument_id: str
    ticker: str
    is_index: bool
    is_fund: bool = False  # TEFAS fund: priced only by ``tefas``, never cross-checked


@dataclass
class Mismatch:
    ticker: str
    ts: date
    primary_close: float
    secondary_close: float
    diff_pct: float


@dataclass
class CrossCheckReport:
    primary: str
    secondary: str
    tolerance_pct: float
    counts: dict[str, int] = field(default_factory=lambda: {"ok": 0, "mismatch": 0, "single_source": 0})
    mismatches: list[Mismatch] = field(default_factory=list)
    missing_primary: list[tuple[str, date]] = field(default_factory=list)
    missing_secondary: list[tuple[str, date]] = field(default_factory=list)
    newest_day: date | None = None  # newest session date seen in the window (any source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "tolerance_pct": self.tolerance_pct,
            "counts": dict(self.counts),
            "mismatches": [
                {
                    "ticker": m.ticker,
                    "date": m.ts.isoformat(),
                    "primary_close": m.primary_close,
                    "secondary_close": m.secondary_close,
                    "diff_pct": round(m.diff_pct, 4),
                }
                for m in self.mismatches
            ],
            "missing_primary": [{"ticker": t, "date": d.isoformat()} for t, d in self.missing_primary],
            "missing_secondary": [{"ticker": t, "date": d.isoformat()} for t, d in self.missing_secondary],
        }


# --------------------------------------------------------------------------- instruments


def source_codes(settings: Settings | None = None) -> list[str]:
    """Configured sources, primary first (secondary '' disables the cross-check)."""
    s = settings or get_settings()
    codes = [s.prices.primary] + ([s.prices.secondary] if s.prices.secondary else [])
    for code in codes:
        if code not in FETCHERS:
            raise ValueError(f"unknown price source {code!r}; known: {', '.join(sorted(FETCHERS))}")
    return codes


def _ticker_rows(at: date) -> Any:
    return select(identifier.c.entity_id, identifier.c.value).where(
        identifier.c.entity_type == "instrument",
        identifier.c.scheme == "TICKER",
        identifier.c.valid_from <= at,
        (identifier.c.valid_to.is_(None)) | (identifier.c.valid_to >= at),
    )


def ticker_of(conn: Connection, instrument_id: str, at: date | None = None) -> str | None:
    at = at or ist_date()
    row = conn.execute(
        _ticker_rows(at)
        .where(identifier.c.entity_id == instrument_id)
        .order_by(identifier.c.valid_from.desc())
    ).first()
    return row.value if row else None


def instrument_for_ticker(conn: Connection, ticker: str, at: date | None = None) -> str | None:
    at = at or ist_date()
    row = conn.execute(
        _ticker_rows(at).where(identifier.c.value == ticker.upper()).order_by(identifier.c.valid_from.desc())
    ).first()
    return row.entity_id if row else None


def _ensure_issuerless_instrument(conn: Connection, code: str, type_: str, extra: dict[str, Any]) -> str:
    """Instrument id for TICKER ``code``; created (no issuer, TRY) with ``type_``/``extra`` if missing."""
    existing = instrument_for_ticker(conn, code)
    if existing is not None:
        return existing
    now = now_utc()
    iid = uuid7()
    conn.execute(
        instrument.insert().values(
            id=iid,
            issuer_id=None,
            type=type_,
            currency="TRY",
            status="active",
            extra={**extra, "created_by": "prices"},
            created_at=now,
        )
    )
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type="instrument",
            entity_id=iid,
            scheme="TICKER",
            value=code,
            valid_from=OPEN_START,
            recorded_at=now,
        )
    )
    return iid


def ensure_index_instrument(conn: Connection, ticker: str = MARKET_INDEX) -> str:
    """Instrument id of index ``ticker`` (type ``index``, no issuer), created if missing (C2)."""
    code = ticker.upper()
    return _ensure_issuerless_instrument(conn, code, "index", {"name": INDEX_NAMES.get(code, code)})


def ensure_fund_instrument(conn: Connection, code: str, name: str) -> str:
    """Instrument id of TEFAS fund ``code`` (type ``fund``, no issuer), created if missing.
    Raises ``ValueError`` when ``code`` already belongs to a non-fund instrument."""
    code = code.upper()
    existing = instrument_for_ticker(conn, code)
    if existing is not None:
        kind = conn.execute(select(instrument.c.type).where(instrument.c.id == existing)).scalar_one()
        if kind != "fund":
            raise ValueError(f"{code} zaten bir {kind} enstrümanı.")
        return existing
    return _ensure_issuerless_instrument(conn, code, "fund", {"name": name, "tefas_code": code})


def resolve_tickers(
    conn: Connection, tickers: Iterable[str], *, create_indices: bool = True
) -> tuple[dict[str, str], list[str]]:
    """Map tickers to instrument ids. Unknown index codes (``X????``) are created as index
    instruments when ``create_indices``; other unknown tickers are returned in the second list."""
    found: dict[str, str] = {}
    unknown: list[str] = []
    for raw in tickers:
        t = raw.strip().upper()
        if not t:
            continue
        iid = instrument_for_ticker(conn, t)
        if iid is None and create_indices and INDEX_TICKER_RE.match(t):
            iid = ensure_index_instrument(conn, t)
        if iid is None:
            unknown.append(t)
        else:
            found[t] = iid
    return found, unknown


def resolve_targets(conn: Connection, instrument_ids: Sequence[str]) -> tuple[list[Target], list[str]]:
    """Targets for instrument ids; ids without a current TICKER are returned as unresolved."""
    targets: list[Target] = []
    unresolved: list[str] = []
    rows = conn.execute(
        select(instrument.c.id, instrument.c.type).where(instrument.c.id.in_(list(instrument_ids)))
    )
    types = {r.id: r.type for r in rows}
    for iid in dict.fromkeys(instrument_ids):
        ticker = ticker_of(conn, iid) if iid in types else None
        if ticker is None:
            unresolved.append(iid)
            continue
        targets.append(Target(iid, ticker, types[iid] == "index", types[iid] == "fund"))
    return targets, unresolved


# --------------------------------------------------------------------------- fetch + store


def collect(
    targets: Sequence[Target],
    start: date,
    end: date,
    *,
    codes: Sequence[str],
    client: httpx.Client | None = None,
    throttle: Throttle | None = None,
) -> list[Fetched]:
    """Network phase: every target from every source, strictly sequential and throttled.
    Funds go to TEFAS only; the configured (BIST) sources get equities and indices."""
    own_client = client is None
    client = client or make_client()
    throttle = throttle or default_throttle()
    out: list[Fetched] = []
    market = [t for t in targets if not t.is_fund]
    try:
        for code in codes:
            fetcher = FETCHERS[code]
            for t in market:
                out.extend(fetcher(client, throttle, t.ticker, is_index=t.is_index, start=start, end=end))
        for t in targets:
            if t.is_fund:
                out.extend(tefas.fetch(client, throttle, t.ticker, is_index=False, start=start, end=end))
    finally:
        if own_client:
            client.close()
    return out


def _upsert_bars(conn: Connection, instrument_id: str, source_id: str, f: Fetched, sha: str | None) -> int:
    now = now_utc()
    n = 0
    for bar in f.bars:
        values = {
            "instrument_id": instrument_id,
            "interval": INTERVAL,
            "ts": bar.ts,
            "source_id": source_id,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "value_traded": bar.value_traded,
            "cross_check": None,
            "recorded_at": now,
            "raw_sha256": sha,
        }
        stmt = insert(price_bar).values(**values)
        update = {
            k: stmt.excluded[k] for k in values if k not in ("instrument_id", "interval", "ts", "source_id")
        }
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    price_bar.c.instrument_id,
                    price_bar.c.interval,
                    price_bar.c.ts,
                    price_bar.c.source_id,
                ],
                set_=update,
            )
        )
        n += 1
    return n


def store(
    conn: Connection,
    targets: Sequence[Target],
    fetched: Sequence[Fetched],
    start: date,
    end: date,
    *,
    settings: Settings | None = None,
    alert: bool = True,
) -> dict[str, Any]:
    """Write phase: raw objects, fetch log, bars, cross-check, metrics, ops alert. Caller commits."""
    s = settings or get_settings()
    codes = source_codes(s)
    if any(t.is_fund for t in targets):
        codes = [*codes, tefas.SOURCE_CODE]
    by_ticker = {t.ticker: t for t in targets}
    source_ids = {code: rawstore.source_id_for(conn, code) for code in codes}
    rows: dict[str, int] = {code: 0 for code in codes}
    errors: list[dict[str, Any]] = []
    for f in fetched:
        target = by_ticker[f.ticker]
        sha: str | None = None
        if f.body is not None:
            sha = rawstore.put_bytes(
                f.body,
                source_code=f.source_code,
                url=f.url,
                http_status=f.http_status,
                content_type=f.content_type,
                request_meta=f.request_meta,
                response_headers=f.response_headers,
                fetcher_version=FETCHER_VERSION,
                started_at=f.started_at,
                conn=conn,
            )
        else:
            rawstore.log_fetch_failure(
                source_code=f.source_code,
                url=f.url,
                outcome=f.outcome,
                error=f.error,
                http_status=f.http_status,
                fetcher_version=FETCHER_VERSION,
                started_at=f.started_at,
                conn=conn,
            )
        if not f.ok:
            errors.append(
                {"source": f.source_code, "ticker": f.ticker, "outcome": f.outcome, "error": f.error}
            )
            continue
        rows[f.source_code] += _upsert_bars(conn, target.instrument_id, source_ids[f.source_code], f, sha)
    for code, n in rows.items():
        metrics.record("prices.sync.rows", n, conn=conn, source=code)
    if errors:
        metrics.record("prices.sync.errors", len(errors), conn=conn)
    report = cross_check(conn, [t.instrument_id for t in targets if not t.is_fund], start, end, settings=s)
    metrics.record("prices.cross_check.mismatch", len(report.mismatches), conn=conn)
    stale_missing = _stale_missing(report)
    metrics.record("prices.cross_check.missing", len(stale_missing), conn=conn)
    if alert and (report.mismatches or stale_missing or errors):
        text = alert_text(report, stale_missing, errors)
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        try:
            _ops_alert(conn, text, level="warning", dedupe_key=f"prices:cross_check:{digest}")
        except Exception:  # alert delivery problems must not roll back the stored bars
            log.exception("ops alert for price check failed")
            metrics.record("prices.alert_failed", 1, conn=conn)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "instruments": [t.ticker for t in targets],
        "rows": rows,
        "errors": errors,
        "cross_check": report.as_dict(),
    }


def _stale_missing(report: CrossCheckReport) -> list[tuple[str, str, date]]:
    """Missing days that are not the newest day of the window (a source may publish later)."""
    if not report.secondary:
        return []
    newest = report.newest_day
    out = [(report.primary, t, d) for t, d in report.missing_primary if d != newest]
    out += [(report.secondary, t, d) for t, d in report.missing_secondary if d != newest]
    return out


def _fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def alert_text(
    report: CrossCheckReport, stale_missing: list[tuple[str, str, date]], errors: list[dict[str, Any]]
) -> str:
    lines = ["Fiyat verisi kontrolü:"]
    if report.mismatches:
        lines.append(
            f"{len(report.mismatches)} kapanış uyumsuzluğu (tolerans %{_fmt(report.tolerance_pct)}, "
            f"{report.primary} / {report.secondary}):"
        )
        for m in report.mismatches[:10]:
            lines.append(
                f"• {m.ticker} {m.ts.isoformat()}: {_fmt(m.primary_close)} / {_fmt(m.secondary_close)} "
                f"(%{_fmt(m.diff_pct)})"
            )
        if len(report.mismatches) > 10:
            lines.append(f"… ve {len(report.mismatches) - 10} tane daha")
    if stale_missing:
        lines.append(f"{len(stale_missing)} eksik gün:")
        for code, t, d in stale_missing[:10]:
            lines.append(f"• {t} {d.isoformat()}: {code} kaynağında yok")
    if errors:
        lines.append(f"{len(errors)} indirme hatası:")
        for e in errors[:5]:
            lines.append(f"• {e['ticker']} ({e['source']}): {e['outcome']} — {(e['error'] or '')[:120]}")
    return "\n".join(lines)


def _ops_alert(conn: Connection, text: str, *, level: str, dedupe_key: str) -> None:
    from papi.notify.ops import ops_alert  # C4; imported lazily (notify depends on nothing here)

    ops_alert(conn, text, level=level, dedupe_key=dedupe_key)


# --------------------------------------------------------------------------- cross-check


def cross_check(
    conn: Connection,
    instrument_ids: Sequence[str],
    start: date,
    end: date,
    *,
    settings: Settings | None = None,
) -> CrossCheckReport:
    """Compare primary vs secondary closes per day on ``[start, end]`` and set ``price_bar.cross_check``.

    Both present: ``ok`` if ``|p − s| / p × 100 ≤ mismatch_tolerance_pct`` else ``mismatch`` (both rows).
    Only one present (or no secondary configured): ``single_source``; the gap is listed in the report.
    """
    s = settings or get_settings()
    codes = source_codes(s)
    primary, secondary = codes[0], (codes[1] if len(codes) > 1 else "")
    tol = float(s.prices.mismatch_tolerance_pct)
    report = CrossCheckReport(primary, secondary, tol)
    src_ids = {
        r.code: r.id for r in conn.execute(select(source.c.code, source.c.id).where(source.c.code.in_(codes)))
    }
    pid, sid = src_ids.get(primary), src_ids.get(secondary)
    for iid in dict.fromkeys(instrument_ids):
        ticker = ticker_of(conn, iid) or iid
        rows = conn.execute(
            select(price_bar.c.ts, price_bar.c.source_id, price_bar.c.close).where(
                price_bar.c.instrument_id == iid,
                price_bar.c.interval == INTERVAL,
                price_bar.c.ts >= start,
                price_bar.c.ts <= end,
                price_bar.c.source_id.in_([x for x in (pid, sid) if x]),
                price_bar.c.close.is_not(None),
            )
        ).all()
        days: dict[date, dict[str, float]] = {}
        for r in rows:
            days.setdefault(r.ts, {})[r.source_id] = r.close
        for ts in sorted(days):
            if report.newest_day is None or ts > report.newest_day:
                report.newest_day = ts
            closes = days[ts]
            p, sc = closes.get(pid), closes.get(sid) if sid else None
            if p is not None and sc is not None:
                diff = abs(p - sc) / p * 100 if p else float("inf")
                status = "ok" if diff <= tol + 1e-9 else "mismatch"
                if status == "mismatch":
                    report.mismatches.append(Mismatch(ticker, ts, p, sc, diff))
                _set_status(conn, iid, ts, [pid, sid], status)
                report.counts[status] += 1
            else:
                present = pid if p is not None else sid
                _set_status(conn, iid, ts, [present], "single_source")
                report.counts["single_source"] += 1
                if secondary:
                    (report.missing_secondary if p is not None else report.missing_primary).append(
                        (ticker, ts)
                    )
    return report


def _set_status(conn: Connection, iid: str, ts: date, source_ids: list[str | None], status: str) -> None:
    conn.execute(
        price_bar.update()
        .where(
            price_bar.c.instrument_id == iid,
            price_bar.c.interval == INTERVAL,
            price_bar.c.ts == ts,
            price_bar.c.source_id.in_([x for x in source_ids if x]),
        )
        .values(cross_check=status)
    )


# --------------------------------------------------------------------------- public API (C7)


def sync_prices(
    conn: Connection,
    instrument_ids: list[str],
    start: date,
    end: date | None = None,
    *,
    client: httpx.Client | None = None,
    throttle: Throttle | None = None,
    alert: bool = True,
) -> dict[str, Any]:
    """Fetch raw daily bars for ``instrument_ids`` on ``[start, end]`` from every configured source,
    upsert them into ``price_bar`` and cross-check. Uses ``conn`` throughout; caller commits.

    Returns ``{start, end, instruments, unresolved, rows: {source: n}, errors, cross_check}``.
    """
    end = end or ist_date()
    codes = source_codes()
    targets, unresolved = resolve_targets(conn, instrument_ids)
    fetched = collect(targets, start, end, codes=codes, client=client, throttle=throttle)
    result = store(conn, targets, fetched, start, end, alert=alert)
    result["unresolved"] = unresolved
    return result


def sync_prices_isolated(
    instrument_ids: list[str],
    start: date,
    end: date | None = None,
    *,
    client: httpx.Client | None = None,
    throttle: Throttle | None = None,
    alert: bool = True,
) -> dict[str, Any]:
    """Same as :func:`sync_prices`, but reads, fetches and writes in separate transactions so no DB
    transaction is open during the network phase (use from workers and the CLI)."""
    end = end or ist_date()
    codes = source_codes()
    with db.connect() as conn:
        targets, unresolved = resolve_targets(conn, instrument_ids)
    fetched = collect(targets, start, end, codes=codes, client=client, throttle=throttle)
    with db.transaction() as conn:
        result = store(conn, targets, fetched, start, end, alert=alert)
    result["unresolved"] = unresolved
    return result


def last_close(
    conn: Connection, instrument_id: str, on_or_before: date | None = None
) -> tuple[date, float] | None:
    """Latest daily close at or before ``on_or_before`` (default: today, Istanbul).

    Takes the newest session date any source has; on that date the primary source's close wins,
    then the secondary, then any other source.
    """
    day = on_or_before or ist_date()
    newest = conn.execute(
        select(price_bar.c.ts)
        .where(
            price_bar.c.instrument_id == instrument_id,
            price_bar.c.interval == INTERVAL,
            price_bar.c.ts <= day,
            price_bar.c.close.is_not(None),
        )
        .order_by(price_bar.c.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    if newest is None:
        return None
    rows = conn.execute(
        select(source.c.code, price_bar.c.close)
        .join(source, source.c.id == price_bar.c.source_id)
        .where(
            price_bar.c.instrument_id == instrument_id,
            price_bar.c.interval == INTERVAL,
            price_bar.c.ts == newest,
            price_bar.c.close.is_not(None),
        )
    ).all()
    s = get_settings()
    rank = {s.prices.primary: 0, s.prices.secondary: 1}
    best = min(rows, key=lambda r: (rank.get(r.code, 2), r.code))
    return newest, float(best.close)


def priced_instrument_ids(conn: Connection, start: date, end: date) -> list[str]:
    """Instruments that have at least one daily bar on ``[start, end]``."""
    return list(
        conn.execute(
            select(price_bar.c.instrument_id)
            .where(price_bar.c.interval == INTERVAL, price_bar.c.ts >= start, price_bar.c.ts <= end)
            .distinct()
        ).scalars()
    )


def bars(
    conn: Connection, instrument_id: str, start: date, end: date
) -> dict[date, dict[str, dict[str, Any]]]:
    """``{date: {source_code: {open, high, low, close, volume, value_traded, cross_check}}}``."""
    rows = conn.execute(
        select(price_bar, source.c.code)
        .join(source, source.c.id == price_bar.c.source_id)
        .where(
            price_bar.c.instrument_id == instrument_id,
            price_bar.c.interval == INTERVAL,
            price_bar.c.ts >= start,
            price_bar.c.ts <= end,
        )
        .order_by(price_bar.c.ts)
    ).all()
    out: dict[date, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r.ts, {})[r.code] = {
            k: getattr(r, k)
            for k in ("open", "high", "low", "close", "volume", "value_traded", "cross_check")
        }
    return out


# --------------------------------------------------------------------------- daily periodic


def _abs(q: Decimal | None) -> Decimal:
    return abs(q) if q is not None else Decimal(0)


def holding_instrument_ids(conn: Connection, today: date, recently_sold_days: int) -> list[str]:
    """Instruments with a positive net quantity, or traded within ``recently_sold_days`` (read from
    ``txn``; reversed pairs ignored)."""
    rows = conn.execute(
        select(
            txn.c.id, txn.c.instrument_id, txn.c.type, txn.c.quantity, txn.c.trade_date, txn.c.reverses_id
        ).where(txn.c.instrument_id.is_not(None))
    ).all()
    reversed_ids = {r.reverses_id for r in rows if r.reverses_id}
    net: dict[str, Decimal] = {}
    recent: set[str] = set()
    cutoff = today - timedelta(days=recently_sold_days)
    for r in rows:
        if r.reverses_id or r.id in reversed_ids:
            continue
        if r.type in _TXN_PLUS:
            delta = _abs(r.quantity)
        elif r.type in _TXN_MINUS:
            delta = -_abs(r.quantity)
        elif r.type in _TXN_SIGNED:
            delta = r.quantity or Decimal(0)
        else:
            delta = Decimal(0)
        net[r.instrument_id] = net.get(r.instrument_id, Decimal(0)) + delta
        if r.trade_date >= cutoff:
            recent.add(r.instrument_id)
    return [iid for iid in dict.fromkeys(list(net) + sorted(recent)) if net.get(iid, 0) > 0 or iid in recent]


def watchlist_instrument_ids(conn: Connection) -> list[str]:
    out: list[str] = []
    for ids in conn.execute(select(watchlist.c.instrument_ids)).scalars():
        out.extend(i for i in (ids or []) if isinstance(i, str))
    return list(dict.fromkeys(out))


def daily_instrument_ids(conn: Connection, today: date, settings: Settings | None = None) -> list[str]:
    """Holdings + watchlist + XU100 (created if missing). Caller commits."""
    s = settings or get_settings()
    ids = holding_instrument_ids(conn, today, s.portfolio.recently_sold_days)
    ids += watchlist_instrument_ids(conn)
    ids.append(ensure_index_instrument(conn, MARKET_INDEX))
    return list(dict.fromkeys(ids))


def is_closed_day(conn: Connection, day: date) -> bool:
    """``True`` on weekends and on days the XIST calendar marks ``closed``."""
    if day.weekday() >= 5:
        return True
    row = conn.execute(
        select(trading_calendar.c.id).where(
            and_(
                trading_calendar.c.calendar_id == CALENDAR_ID,
                trading_calendar.c.date == day,
                trading_calendar.c.session_type == "closed",
            )
        )
    ).first()
    return row is not None


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def daily_due(now_ist: datetime, fetch_time: str, last_done: date | None, closed: bool) -> bool:
    """The daily sync runs once per trading day, at or after ``fetch_time`` (Istanbul)."""
    today = now_ist.date()
    return not closed and now_ist.time() >= parse_hhmm(fetch_time) and last_done != today
