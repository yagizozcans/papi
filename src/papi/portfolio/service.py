"""Portfolio service (contract C3 in docs/dev/wave1-contracts.md; PRD docs/prd/12-portfolio.md).

Ledger rules
------------
* ``txn`` is append-only. A correction is a *reversing entry*: a new row with ``reverses_id`` pointing
  at the original, the same type/date and negated ``quantity``/``fees``/``tax``. The position engine
  drops both rows of a reversed pair, so history replays as if the original never happened.
* ``quantity`` is a positive magnitude whose direction comes from ``type`` (buy, sell, transfer_in/out,
  opening_balance, bonus_shares, rights_subscription, rights_sale, dividend_cash). It is signed only for
  ``reconcile_adjustment`` and ``split`` (share delta; negative for a reverse split). ``fee``/``tax``
  rows carry their amount in ``fees``/``tax`` and may have quantity 0.

Cost basis: weighted average, per portfolio and instrument, replayed in (trade_date, entry) order
---------------------------------------------------------------------------------------------------
* buy / opening_balance / transfer_in / rights_subscription: quantity += q; cost += q·price + fees + tax.
* sell / transfer_out: the sold shares leave at the current average cost (cost −= avg·q, average
  unchanged). For ``sell`` the realised P&L is q·price − fees − tax − avg·q (computed, not stored).
* bonus_shares / split: quantity += q; total cost unchanged, so the per-share cost falls (12 §3).
* reconcile_adjustment: q > 0 adds shares at ``price`` if given, else at the current average cost;
  q < 0 removes shares at the average cost (no realised P&L).
* dividend_cash: income += q·price − tax − fees (q = eligible shares, price = gross per share,
  tax = withholding); rights_sale: realised += q·price − fees − tax. Neither changes quantity or cost.
* fee / tax rows tied to an instrument reduce realised P&L.

Functions take an open connection inside a transaction and never commit.
"""

from __future__ import annotations

import difflib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Connection, case, delete, select, update

from papi.core import schema as s
from papi.core.config import get_settings
from papi.core.events import PORTFOLIO_CHANGED, PortfolioChanged, publish
from papi.core.ids import uuid7
from papi.core.jobs import PRIORITY_P1
from papi.core.text import fold_diacritics, normalize_tr
from papi.core.timeutil import ist_date, now_utc
from papi.portfolio.fmt import fmt_date, fmt_num, fmt_qty

log = logging.getLogger("papi.portfolio")

ZERO = Decimal(0)
TxnType = Literal[
    "buy",
    "sell",
    "dividend_cash",
    "bonus_shares",
    "rights_subscription",
    "rights_sale",
    "split",
    "fee",
    "tax",
    "transfer_in",
    "transfer_out",
    "opening_balance",
    "reconcile_adjustment",
]
TXN_TYPES: tuple[str, ...] = TxnType.__args__  # type: ignore[attr-defined]

_ADD_AT_PRICE = {"buy", "opening_balance", "transfer_in", "rights_subscription"}
_REMOVE = {"sell", "transfer_out"}
_NO_COST = {"bonus_shares", "split"}
_POSITIVE_QTY = {
    "buy",
    "sell",
    "dividend_cash",
    "bonus_shares",
    "rights_subscription",
    "rights_sale",
    "transfer_in",
    "transfer_out",
    "opening_balance",
}
_PRICED = {
    "buy",
    "sell",
    "opening_balance",
    "transfer_in",
    "rights_subscription",
    "rights_sale",
    "dividend_cash",
}
_CASH_ONLY = {"fee", "tax"}

# Turkish/English spellings (normalize_tr keys) → txn type; used by the CLI and CSV import.
TXN_TYPE_ALIASES: dict[str, str] = {
    **{normalize_tr(t): t for t in TXN_TYPES},
    "al": "buy",
    "alis": "buy",
    "alim": "buy",
    "sat": "sell",
    "satis": "sell",
    "acilis": "opening_balance",
    "acilis bakiyesi": "opening_balance",
    "devir": "opening_balance",
    "temettu": "dividend_cash",
    "nakit temettu": "dividend_cash",
    "bedelsiz": "bonus_shares",
    "bedelli": "rights_subscription",
    "ruchan satisi": "rights_sale",
    "bolunme": "split",
    "komisyon": "fee",
    "masraf": "fee",
    "vergi": "tax",
    "virman giris": "transfer_in",
    "virman cikis": "transfer_out",
    "mutabakat": "reconcile_adjustment",
}

TYPE_LABELS_TR: dict[str, str] = {
    "buy": "ALIŞ",
    "sell": "SATIŞ",
    "dividend_cash": "TEMETTÜ",
    "bonus_shares": "BEDELSİZ",
    "rights_subscription": "BEDELLİ",
    "rights_sale": "RÜÇHAN SATIŞI",
    "split": "BÖLÜNME",
    "fee": "MASRAF",
    "tax": "VERGİ",
    "transfer_in": "VİRMAN GİRİŞ",
    "transfer_out": "VİRMAN ÇIKIŞ",
    "opening_balance": "AÇILIŞ",
    "reconcile_adjustment": "MUTABAKAT",
}


def parse_txn_type(text: str) -> str:
    """``BUY``/``AL``/``ALIŞ`` → ``buy`` etc.; raises ``ValueError`` for unknown words."""
    key = normalize_tr(text)
    try:
        return TXN_TYPE_ALIASES[key]
    except KeyError:
        raise ValueError(f"bilinmeyen işlem türü: {text!r}") from None


# --------------------------------------------------------------------------------------------- models


@dataclass
class InstrumentMatch:
    instrument_id: str
    ticker: str
    issuer_name: str | None
    score: float


@dataclass
class Position:
    instrument_id: str
    ticker: str
    quantity: Decimal
    avg_cost: Decimal | None
    cost_total: Decimal
    last_close: Decimal | None
    last_close_date: date | None
    market_value: Decimal | None
    weight: float | None


class ProposedTxn(BaseModel):
    portfolio_id: str | None = None
    instrument_id: str | None = None
    ticker: str | None = None
    type: TxnType
    quantity: Decimal
    price: Decimal | None = None
    fees: Decimal = ZERO
    trade_date: date
    traded_at: datetime | None = None
    source: str = "manual"
    chat_message_id: str | None = None
    note: str | None = None
    warnings: list[str] = []


@dataclass
class ReconcileDiff:
    portfolio_id: str
    instrument_id: str | None  # None: ticker unknown to papi (cannot be applied)
    ticker: str
    papi_quantity: Decimal
    observed_quantity: Decimal
    hint: str | None = None

    @property
    def delta(self) -> Decimal:
        """İşCep minus papi; the ``reconcile_adjustment`` quantity ``apply_reconcile`` records."""
        return self.observed_quantity - self.papi_quantity


@dataclass
class Holding:
    """Replayed ledger state for one (portfolio, instrument)."""

    portfolio_id: str
    instrument_id: str
    quantity: Decimal = ZERO
    cost_total: Decimal = ZERO
    realised_pnl: Decimal = ZERO
    income: Decimal = ZERO
    txn_ids: list[str] = field(default_factory=list)

    @property
    def avg_cost(self) -> Decimal | None:
        return self.cost_total / self.quantity if self.quantity > 0 else None

    def apply(self, r: Any) -> None:
        t, q = r.type, r.quantity
        price = r.price
        fees, tax = r.fees or ZERO, r.tax or ZERO
        self.txn_ids.append(r.id)
        if t in _ADD_AT_PRICE:
            self.quantity += q
            self.cost_total += q * (price or ZERO) + fees + tax
        elif t in _REMOVE:
            released = self._release(q)
            if t == "sell":
                self.realised_pnl += q * (price or ZERO) - fees - tax - released
        elif t in _NO_COST:
            self.quantity += q
        elif t == "reconcile_adjustment":
            if q > 0:
                unit = price if price is not None else (self.avg_cost or ZERO)
                self.quantity += q
                self.cost_total += q * unit
            elif q < 0:
                self._release(-q)
        elif t == "dividend_cash":
            self.income += q * (price or ZERO) - tax - fees
        elif t == "rights_sale":
            self.realised_pnl += q * (price or ZERO) - fees - tax
        elif t in _CASH_ONLY:
            self.realised_pnl -= fees + tax
        if self.quantity <= 0:
            self.cost_total = ZERO

    def _release(self, q: Decimal) -> Decimal:
        """Remove ``q`` shares at the average cost; returns the cost released."""
        released = self.cost_total * min(q, self.quantity) / self.quantity if self.quantity > 0 else ZERO
        self.cost_total -= released
        self.quantity -= q
        return released


# --------------------------------------------------------------------------------------------- helpers


def _as_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ist_date(value)
    return value


def _valid_at(valid_from: date | None, valid_to: date | None, at: date) -> bool:
    return (valid_from is None or valid_from <= at) and (valid_to is None or valid_to >= at)


def _ticker_key(query: str) -> str:
    key = fold_diacritics(query.strip()).upper().replace(" ", "")
    for suffix in (".IS", ".E"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


_NAME_SUFFIXES = ("anonim sirketi", "a s", "t a s", "as", "tas")


def _name_key(name: str | None) -> str:
    n = normalize_tr(name or "")
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_SUFFIXES:
            if n.endswith(" " + suffix):
                n = n[: -len(suffix) - 1].rstrip()
                changed = True
    return n


def _name_score(qn: str, name: str) -> float:
    if not name or len(qn) < 2:
        return 0.0
    if name == qn:
        return 0.95
    if name.startswith(qn):
        return 0.85 + 0.05 * len(qn) / len(name)
    q_tokens, n_tokens = qn.split(), name.split()
    if all(any(t.startswith(qt) for t in n_tokens) for qt in q_tokens):
        return 0.75 + 0.05 * len(qn) / len(name)
    best = difflib.SequenceMatcher(None, qn, name).ratio()
    k = len(q_tokens)
    for i in range(len(n_tokens) - k + 1):
        best = max(best, difflib.SequenceMatcher(None, qn, " ".join(n_tokens[i : i + k])).ratio())
    return 0.7 * best if best >= 0.75 else 0.0


def _txn_rows(
    conn: Connection,
    *,
    portfolio_id: str | None = None,
    instrument_ids: list[str] | None = None,
    as_of: date | None = None,
) -> list[Any]:
    """Instrument txns in replay order, with reversed pairs removed."""
    q = select(s.txn).where(s.txn.c.instrument_id.is_not(None))
    if portfolio_id is not None:
        q = q.where(s.txn.c.portfolio_id == portfolio_id)
    if instrument_ids is not None:
        q = q.where(s.txn.c.instrument_id.in_(instrument_ids))
    if as_of is not None:
        q = q.where(s.txn.c.trade_date <= as_of)
    rows = conn.execute(q.order_by(s.txn.c.trade_date, s.txn.c.created_at, s.txn.c.id)).all()
    reversed_ids = {r.reverses_id for r in rows if r.reverses_id}
    return [r for r in rows if not r.reverses_id and r.id not in reversed_ids]


def ledger(
    conn: Connection,
    portfolio_id: str | None = None,
    as_of: date | None = None,
    instrument_ids: list[str] | None = None,
) -> dict[tuple[str, str], Holding]:
    """Replay transactions (all portfolios when ``portfolio_id`` is None) up to ``as_of`` inclusive."""
    out: dict[tuple[str, str], Holding] = {}
    for r in _txn_rows(conn, portfolio_id=portfolio_id, instrument_ids=instrument_ids, as_of=as_of):
        key = (r.portfolio_id, r.instrument_id)
        h = out.get(key)
        if h is None:
            h = out[key] = Holding(r.portfolio_id, r.instrument_id)
        h.apply(r)
    return out


def _held(conn: Connection, portfolio_id: str, instrument_id: str, as_of: date | None) -> Decimal:
    h = ledger(conn, portfolio_id, as_of, [instrument_id]).get((portfolio_id, instrument_id))
    return h.quantity if h else ZERO


def last_close(
    conn: Connection, instrument_id: str, on_or_before: date | None = None
) -> tuple[date, Decimal] | None:
    """Latest daily close at or before the date (any source; the primary source wins a tie)."""
    d = on_or_before or ist_date()
    primary = get_settings().prices.primary
    row = conn.execute(
        select(s.price_bar.c.ts, s.price_bar.c.close)
        .select_from(s.price_bar.join(s.source, s.source.c.id == s.price_bar.c.source_id))
        .where(
            s.price_bar.c.instrument_id == instrument_id,
            s.price_bar.c.interval == "1d",
            s.price_bar.c.ts <= d,
            s.price_bar.c.close.is_not(None),
        )
        .order_by(s.price_bar.c.ts.desc(), case((s.source.c.code == primary, 0), else_=1), s.source.c.code)
        .limit(1)
    ).first()
    if row is None:
        return None
    return row.ts, Decimal(str(row.close))


def _publish_changed(
    conn: Connection, portfolio_id: str, instrument_ids: list[str], txn_ids: list[str], reason: str
) -> None:
    data = PortfolioChanged(
        portfolio_id=portfolio_id,
        instrument_ids=list(dict.fromkeys(i for i in instrument_ids if i)),
        transaction_ids=txn_ids,
        reason=reason,
    )
    publish(conn, PORTFOLIO_CHANGED, f"portfolio/{portfolio_id}", data, "portfolio", priority=PRIORITY_P1)


def _reason_for_source(source: str) -> str:
    if source == "csv_template":
        return "import"
    if source == "reconcile":
        return "reconcile"
    if source.startswith("derived:"):
        return "corporate_action"
    return "trade"


# --------------------------------------------------------------------------------------------- C3 API


def default_portfolio_id(conn: Connection) -> str:
    """The ``is_default`` portfolio (oldest first); created from settings if no portfolio exists."""
    pid = conn.execute(
        select(s.portfolio.c.id).order_by(s.portfolio.c.is_default.desc(), s.portfolio.c.created_at).limit(1)
    ).scalar_one_or_none()
    if pid is not None:
        return pid
    cfg = get_settings().portfolio
    pid = uuid7()
    conn.execute(
        s.portfolio.insert().values(
            id=pid,
            name=cfg.default_name,
            base_currency=cfg.base_currency,
            account_label=cfg.account_label,
            is_default=True,
            created_at=now_utc(),
        )
    )
    return pid


def portfolio_id_by_name(conn: Connection, name: str | None) -> str:
    """Portfolio id for ``name`` (default portfolio when None); ``ValueError`` if unknown."""
    if name is None:
        return default_portfolio_id(conn)
    pid = conn.execute(select(s.portfolio.c.id).where(s.portfolio.c.name == name)).scalar_one_or_none()
    if pid is None:
        raise ValueError(f"portföy bulunamadı: {name}")
    return pid


def ticker_for(conn: Connection, instrument_id: str, at: date | None = None) -> str | None:
    """TICKER valid at ``at`` (default today); falls back to the most recent ticker ever held."""
    d = _as_date(at) or ist_date()
    rows = conn.execute(
        select(s.identifier.c.value, s.identifier.c.valid_from, s.identifier.c.valid_to)
        .where(
            s.identifier.c.scheme == "TICKER",
            s.identifier.c.entity_type == "instrument",
            s.identifier.c.entity_id == instrument_id,
            s.identifier.c.superseded_at.is_(None),
        )
        .order_by(s.identifier.c.valid_from.desc())
    ).all()
    for r in rows:
        if _valid_at(r.valid_from, r.valid_to, d):
            return r.value
    return rows[0].value if rows else None


def resolve_instrument(
    conn: Connection, query: str, at: date | None = None, limit: int = 5
) -> list[InstrumentMatch]:
    """Rank instruments for a human reference (ticker, ISIN or company name), best first.

    Scores: 1.0 exact TICKER valid at ``at`` (or exact ISIN); 0.8–0.9 ticker prefix; 0.95 exact issuer
    name; 0.85–0.9 name prefix; 0.75–0.8 every word is a word-prefix; ≤0.7 fuzzy (difflib) or a ticker
    that was valid only at another date. Names compare Turkish-normalised (``İŞ``/``iş``/``IS`` equal).
    """
    d = _as_date(at) or ist_date()
    key = _ticker_key(query)
    qn = _name_key(query)
    if not key and not qn:
        return []
    rows = conn.execute(
        select(
            s.identifier.c.entity_id,
            s.identifier.c.value,
            s.identifier.c.valid_from,
            s.identifier.c.valid_to,
            s.instrument.c.type,
            s.issuer.c.short_name,
            s.issuer.c.legal_name,
        )
        .select_from(
            s.identifier.join(s.instrument, s.instrument.c.id == s.identifier.c.entity_id).outerjoin(
                s.issuer, s.issuer.c.id == s.instrument.c.issuer_id
            )
        )
        .where(
            s.identifier.c.scheme == "TICKER",
            s.identifier.c.entity_type == "instrument",
            s.identifier.c.superseded_at.is_(None),
        )
    ).all()
    current: dict[str, tuple[date, str]] = {}
    latest: dict[str, tuple[date, str]] = {}
    info: dict[str, Any] = {}
    scores: dict[str, float] = defaultdict(float)
    for r in rows:
        iid = r.entity_id
        info[iid] = r
        valid = _valid_at(r.valid_from, r.valid_to, d)
        if iid not in latest or r.valid_from > latest[iid][0]:
            latest[iid] = (r.valid_from, r.value)
        if valid and (iid not in current or r.valid_from > current[iid][0]):
            current[iid] = (r.valid_from, r.value)
        if r.value == key:
            score = 1.0 if valid else 0.7
        elif valid and len(key) >= 2 and r.value.startswith(key):
            score = 0.8 + 0.1 * len(key) / len(r.value)
        else:
            score = 0.0
        scores[iid] = max(scores[iid], score)
    if 2 <= len(key) <= 12:
        for iid in conn.execute(
            select(s.identifier.c.entity_id).where(
                s.identifier.c.scheme == "ISIN",
                s.identifier.c.entity_type == "instrument",
                s.identifier.c.value == key,
            )
        ).scalars():
            if iid in info:
                scores[iid] = 1.0
    if qn:
        for iid in current:
            r = info[iid]
            name_score = max(
                _name_score(qn, _name_key(r.short_name)), _name_score(qn, _name_key(r.legal_name))
            )
            scores[iid] = max(scores[iid], name_score)
    matches = []
    for iid, score in scores.items():
        if score <= 0:
            continue
        r = info[iid]
        ticker = (current.get(iid) or latest[iid])[1]
        matches.append(
            (
                r.type != "equity",
                InstrumentMatch(iid, ticker, r.short_name or r.legal_name, round(score, 4)),
            )
        )
    matches.sort(key=lambda m: (-m[1].score, m[0], m[1].ticker))
    return [m for _, m in matches[:limit]]


def _exact_instrument(conn: Connection, ticker: str, at: date | None) -> InstrumentMatch | None:
    matches = resolve_instrument(conn, ticker, at=at)
    return matches[0] if matches and matches[0].score >= 1.0 else None


def _unknown_ticker_message(conn: Connection, ticker: str, at: date | None) -> str:
    suggestions = [m.ticker for m in resolve_instrument(conn, ticker, at=at, limit=3)]
    msg = f"Bilinmeyen hisse kodu: {ticker}."
    if suggestions:
        msg += f" Bunu mu demek istediniz: {', '.join(suggestions)}?"
    return msg


def validate_proposal(conn: Connection, p: ProposedTxn) -> ProposedTxn:
    """Copy of ``p`` with portfolio/instrument/ticker filled and Turkish warnings added (never raises)."""
    out = p.model_copy(deep=True)
    warnings = list(out.warnings)

    def warn(msg: str) -> None:
        if msg not in warnings:
            warnings.append(msg)

    if out.portfolio_id is None:
        out.portfolio_id = default_portfolio_id(conn)
    if out.instrument_id is None and out.ticker:
        match = _exact_instrument(conn, out.ticker, out.trade_date)
        if match:
            out.instrument_id, out.ticker = match.instrument_id, match.ticker
        else:
            warn(_unknown_ticker_message(conn, out.ticker, out.trade_date))
    elif out.instrument_id is not None and not out.ticker:
        out.ticker = ticker_for(conn, out.instrument_id, out.trade_date)
    if out.instrument_id is None and not out.ticker and out.type not in _CASH_ONLY:
        warn("Hisse belirtilmedi.")

    if out.trade_date > ist_date():
        warn(f"İşlem tarihi ileri bir tarih: {fmt_date(out.trade_date)}.")
    if out.type in _POSITIVE_QTY and out.quantity <= 0:
        warn("Miktar sıfırdan büyük olmalı.")
    if out.type in ("buy", "sell") and out.quantity != out.quantity.to_integral_value():
        warn(f"Miktar tam sayı değil: {fmt_qty(out.quantity)} (BIST'te 1 lot = 1 pay).")
    if out.type in _PRICED and out.price is None:
        warn("Fiyat belirtilmedi.")
    if out.price is not None and out.price < 0:
        warn("Fiyat negatif olamaz.")
    if out.fees < 0:
        warn("Komisyon negatif olamaz.")

    if out.instrument_id is not None:
        if out.type in _REMOVE:
            held = _held(conn, out.portfolio_id, out.instrument_id, out.trade_date)
            if out.quantity > held:
                warn(f"Satış miktarı ({fmt_qty(out.quantity)}) eldeki adetten ({fmt_qty(held)}) fazla.")
        if out.type in ("buy", "sell") and out.price is not None and out.price > 0:
            lc = last_close(conn, out.instrument_id, out.trade_date)
            if lc is None:
                warn("Son kapanış fiyatı yok; fiyat kontrol edilemedi.")
            else:
                close_date, close = lc
                if close > 0:
                    dev = abs(out.price - close) / close * 100
                    if dev > Decimal(str(get_settings().telegram.price_warn_pct)):
                        warn(
                            f"Fiyat {fmt_num(out.price)} son kapanıştan ({fmt_date(close_date)}: "
                            f"{fmt_num(close)}) %{fmt_num(dev, 1)} farklı."
                        )
    out.warnings = warnings
    return out


def _insert_txn(
    conn: Connection,
    p: ProposedTxn,
    *,
    dedupe_key: str | None = None,
    tax: Decimal = ZERO,
) -> tuple[str, str, str | None]:
    """Validate amounts strictly and insert one row; returns (txn_id, portfolio_id, instrument_id)."""
    pid = p.portfolio_id or default_portfolio_id(conn)
    if conn.execute(select(s.portfolio.c.id).where(s.portfolio.c.id == pid)).first() is None:
        raise ValueError(f"portföy bulunamadı: {pid}")
    iid = p.instrument_id
    if iid is None and p.ticker:
        match = _exact_instrument(conn, p.ticker, p.trade_date)
        if match is None:
            raise ValueError(_unknown_ticker_message(conn, p.ticker, p.trade_date))
        iid = match.instrument_id
    currency = conn.execute(select(s.portfolio.c.base_currency).where(s.portfolio.c.id == pid)).scalar_one()
    if iid is not None:
        inst_currency = conn.execute(
            select(s.instrument.c.currency).where(s.instrument.c.id == iid)
        ).scalar_one_or_none()
        if inst_currency is None:
            raise ValueError(f"enstrüman bulunamadı: {iid}")
        currency = inst_currency
    elif p.type not in _CASH_ONLY:
        raise ValueError("Hisse belirtilmedi.")

    fees = p.fees
    if p.type in _POSITIVE_QTY and p.quantity <= 0:
        raise ValueError("Miktar sıfırdan büyük olmalı.")
    if p.type in ("split", "reconcile_adjustment") and p.quantity == 0:
        raise ValueError("Miktar sıfır olamaz.")
    if p.type in _PRICED and p.price is None:
        raise ValueError(f"{TYPE_LABELS_TR[p.type]} için fiyat gerekli.")
    if (p.price is not None and p.price < 0) or fees < 0 or tax < 0:
        raise ValueError("Fiyat, komisyon ve vergi negatif olamaz.")
    if p.type == "tax":  # a tax row's amount travels in ProposedTxn.fees
        tax, fees = tax + fees, ZERO
    if p.type in _CASH_ONLY and fees + tax <= 0:
        raise ValueError("Masraf/vergi tutarı sıfırdan büyük olmalı.")

    tid = uuid7()
    conn.execute(
        s.txn.insert().values(
            id=tid,
            portfolio_id=pid,
            instrument_id=iid,
            type=p.type,
            trade_date=p.trade_date,
            traded_at=p.traded_at,
            quantity=p.quantity,
            price=p.price,
            fees=fees,
            tax=tax,
            currency=currency,
            source=p.source,
            chat_message_id=p.chat_message_id,
            dedupe_key=dedupe_key,
            note=p.note,
            created_at=now_utc(),
        )
    )
    return tid, pid, iid


def _txn_by_dedupe(conn: Connection, dedupe_key: str) -> str | None:
    return conn.execute(select(s.txn.c.id).where(s.txn.c.dedupe_key == dedupe_key)).scalar_one_or_none()


def record_transaction(conn: Connection, p: ProposedTxn, *, dedupe_key: str | None = None) -> str:
    """Append a transaction and publish ``portfolio.changed.v1``; returns the txn id.

    An existing ``dedupe_key`` returns that transaction's id and writes nothing. Raises ``ValueError``
    (Turkish message) when the ticker is not an exact match or amounts are invalid.
    """
    if dedupe_key is not None and (existing := _txn_by_dedupe(conn, dedupe_key)):
        return existing
    tid, pid, iid = _insert_txn(conn, p, dedupe_key=dedupe_key)
    _publish_changed(conn, pid, [iid] if iid else [], [tid], _reason_for_source(p.source))
    return tid


def reverse_transaction(conn: Connection, txn_id: str, *, reason: str | None = None) -> str:
    """Append the reversing entry for ``txn_id``; returns its id. An applied pending action that
    produced the transaction goes back to ``expected``."""
    row = conn.execute(select(s.txn).where(s.txn.c.id == txn_id)).first()
    if row is None:
        raise ValueError(f"işlem bulunamadı: {txn_id}")
    if row.reverses_id is not None:
        raise ValueError("Ters kayıt geri alınamaz.")
    if conn.execute(select(s.txn.c.id).where(s.txn.c.reverses_id == txn_id)).first() is not None:
        raise ValueError("Bu işlem zaten geri alınmış.")
    rid = uuid7()
    note = "Geri alındı" + (f": {reason}" if reason else "")
    conn.execute(
        s.txn.insert().values(
            id=rid,
            portfolio_id=row.portfolio_id,
            instrument_id=row.instrument_id,
            type=row.type,
            trade_date=row.trade_date,
            traded_at=row.traded_at,
            settle_date=row.settle_date,
            quantity=-row.quantity,
            price=row.price,
            fees=-(row.fees or ZERO),
            tax=-(row.tax or ZERO),
            currency=row.currency,
            source=row.source,
            chat_message_id=row.chat_message_id,
            reverses_id=row.id,
            note=note,
            created_at=now_utc(),
        )
    )
    conn.execute(
        update(s.pending_action)
        .where(s.pending_action.c.transaction_id == txn_id)
        .values(status="expected", transaction_id=None, resolved_at=None)
    )
    _publish_changed(conn, row.portfolio_id, [row.instrument_id] if row.instrument_id else [], [rid], "undo")
    return rid


def positions(conn: Connection, portfolio_id: str | None = None, as_of: date | None = None) -> list[Position]:
    """Non-zero positions of a portfolio (default portfolio when None) valued at the latest close
    ≤ ``as_of`` (default today). Sorted by market value, unpriced last; weights over priced value."""
    pid = portfolio_id or default_portfolio_id(conn)
    d = as_of or ist_date()
    out: list[Position] = []
    for h in ledger(conn, pid, d).values():
        if h.quantity == 0:
            continue
        lc = last_close(conn, h.instrument_id, d)
        close_date, close = lc if lc else (None, None)
        out.append(
            Position(
                instrument_id=h.instrument_id,
                ticker=ticker_for(conn, h.instrument_id, d) or h.instrument_id,
                quantity=h.quantity,
                avg_cost=h.avg_cost,
                cost_total=h.cost_total,
                last_close=close,
                last_close_date=close_date,
                market_value=h.quantity * close if close is not None else None,
                weight=None,
            )
        )
    total = sum((p.market_value for p in out if p.market_value is not None and p.market_value > 0), ZERO)
    for p in out:
        if total > 0 and p.market_value is not None:
            p.weight = float(p.market_value / total)
    out.sort(key=lambda p: (p.market_value is None, -(p.market_value or ZERO), p.ticker))
    return out


def holding(conn: Connection, instrument_id: str, portfolio_id: str | None = None) -> Position | None:
    """Today's position in one instrument (weight within the portfolio), or None if not held."""
    return next((p for p in positions(conn, portfolio_id) if p.instrument_id == instrument_id), None)


def realised_summary(
    conn: Connection, portfolio_id: str | None = None, as_of: date | None = None
) -> dict[str, Decimal]:
    """Realised P&L (sells, rights sales, fees/taxes) and net dividend income in portfolio currency."""
    pid = portfolio_id or default_portfolio_id(conn)
    hs = ledger(conn, pid, as_of or ist_date()).values()
    return {
        "realised_pnl": sum((h.realised_pnl for h in hs), ZERO),
        "income": sum((h.income for h in hs), ZERO),
    }


def snapshot_positions(
    conn: Connection, as_of: date | None = None, *, portfolio_id: str | None = None
) -> int:
    """Rewrite ``position_snapshot`` rows for ``as_of`` (all portfolios unless one is given)."""
    d = as_of or ist_date()
    pids = (
        [portfolio_id]
        if portfolio_id
        else list(conn.execute(select(s.portfolio.c.id).order_by(s.portfolio.c.created_at)).scalars())
    )
    now = now_utc()
    n = 0
    for pid in pids:
        conn.execute(
            delete(s.position_snapshot).where(
                s.position_snapshot.c.portfolio_id == pid, s.position_snapshot.c.as_of == d
            )
        )
        for p in positions(conn, pid, d):
            conn.execute(
                s.position_snapshot.insert().values(
                    portfolio_id=pid,
                    instrument_id=p.instrument_id,
                    as_of=d,
                    quantity=p.quantity,
                    avg_cost_try=p.avg_cost,
                    market_value_try=p.market_value,
                    weight=p.weight,
                    computed_at=now,
                )
            )
            n += 1
    return n


def transactions(
    conn: Connection,
    portfolio_id: str | None = None,
    limit: int | None = None,
    instrument_id: str | None = None,
) -> list[dict[str, Any]]:
    """Ledger rows newest first (default portfolio when None), with ``ticker`` and ``reversed_by``."""
    pid = portfolio_id or default_portfolio_id(conn)
    q = select(s.txn).where(s.txn.c.portfolio_id == pid)
    if instrument_id is not None:
        q = q.where(s.txn.c.instrument_id == instrument_id)
    q = q.order_by(s.txn.c.trade_date.desc(), s.txn.c.created_at.desc(), s.txn.c.id.desc())
    if limit:
        q = q.limit(limit)
    rows = [dict(r._mapping) for r in conn.execute(q)]
    ids = [r["id"] for r in rows]
    reversed_by: dict[str, str] = {}
    if ids:
        q_rev = select(s.txn.c.reverses_id, s.txn.c.id).where(s.txn.c.reverses_id.in_(ids))
        reversed_by = {orig: rev for orig, rev in conn.execute(q_rev)}
    tickers: dict[str, str | None] = {}
    for r in rows:
        iid = r["instrument_id"]
        if iid and iid not in tickers:
            tickers[iid] = ticker_for(conn, iid, r["trade_date"])
        r["ticker"] = tickers.get(iid) if iid else None
        r["reversed_by"] = reversed_by.get(r["id"])
    return rows


# --------------------------------------------------------------------------------------------- priority


def _watchlist_rows(conn: Connection) -> list[Any]:
    return conn.execute(select(s.watchlist).order_by(s.watchlist.c.name)).all()


def holding_priority(
    conn: Connection, instrument_ids: list[str], at: datetime | None = None
) -> Literal["P1", "P2"] | None:
    """P1 if any instrument is held (quantity > 0) in any portfolio at ``at`` (Istanbul date); P2 if
    any is on a watchlist (directly, via issuer sector or via index membership valid at that date) or
    was held within ``portfolio.recently_sold_days``; else None."""
    ids = list(dict.fromkeys(i for i in instrument_ids if i))
    if not ids:
        return None
    d = _as_date(at) or ist_date()
    window_start = d - timedelta(days=get_settings().portfolio.recently_sold_days)
    states: dict[tuple[str, str], Holding] = {}
    entered_window: set[tuple[str, str]] = set()
    recently_held = False
    for r in _txn_rows(conn, instrument_ids=ids, as_of=d):
        key = (r.portfolio_id, r.instrument_id)
        h = states.setdefault(key, Holding(*key))
        if r.trade_date >= window_start:
            if key not in entered_window:
                entered_window.add(key)
                recently_held |= h.quantity > 0
            h.apply(r)
            recently_held |= h.quantity > 0
        else:
            h.apply(r)
    if any(h.quantity > 0 for h in states.values()):
        return "P1"
    if recently_held:
        return "P2"

    lists = _watchlist_rows(conn)
    if not lists:
        return None
    id_set = set(ids)
    sectors: set[str] = set()
    indices: set[str] = set()
    for wl in lists:
        if id_set & set(wl.instrument_ids or []):
            return "P2"
        sectors |= {normalize_tr(x) for x in wl.sectors or [] if x}
        indices |= {_ticker_key(x) for x in wl.indices or [] if x}
    if sectors:
        codes = conn.execute(
            select(s.issuer.c.sector_code)
            .select_from(s.instrument.join(s.issuer, s.issuer.c.id == s.instrument.c.issuer_id))
            .where(s.instrument.c.id.in_(ids), s.issuer.c.sector_code.is_not(None))
        ).scalars()
        if any(normalize_tr(c) in sectors for c in codes):
            return "P2"
    if indices:
        rows = conn.execute(
            select(s.index_membership.c.valid_from, s.index_membership.c.valid_to).where(
                s.index_membership.c.instrument_id.in_(ids),
                s.index_membership.c.index_code.in_(sorted(indices)),
            )
        ).all()
        if any(_valid_at(r.valid_from, r.valid_to, d) for r in rows):
            return "P2"
    return None


# --------------------------------------------------------------------------------------------- watchlists

_SECTOR_PREFIXES = ("sektor:", "sector:")
_INDEX_PREFIXES = ("endeks:", "index:")


def _parse_watch_ref(conn: Connection, ref: str) -> tuple[str, str]:
    """→ ("instrument", id) | ("sector", code) | ("index", code)."""
    raw = ref.strip()
    low = fold_diacritics(raw).lower()
    for prefix in _SECTOR_PREFIXES:
        if low.startswith(prefix):
            code = raw[len(prefix) :].strip()
            if not code:
                raise ValueError("Sektör kodu boş.")
            return "sector", code
    for prefix in _INDEX_PREFIXES:
        if low.startswith(prefix):
            code = _ticker_key(raw[len(prefix) :])
            if not code:
                raise ValueError("Endeks kodu boş.")
            return "index", code
    match = _exact_instrument(conn, raw, None)
    if match is not None:
        itype = conn.execute(
            select(s.instrument.c.type).where(s.instrument.c.id == match.instrument_id)
        ).scalar_one()
        if itype == "index":
            return "index", match.ticker
        return "instrument", match.instrument_id
    key = _ticker_key(raw)
    if conn.execute(
        select(s.index_membership.c.id).where(s.index_membership.c.index_code == key).limit(1)
    ).first():
        return "index", key
    raise ValueError(_unknown_ticker_message(conn, raw, None))


_WATCH_COLUMN = {"instrument": "instrument_ids", "sector": "sectors", "index": "indices"}


def watch(conn: Connection, ref: str, watchlist: str = "Ana") -> str:
    """Add a ticker, ``sektör:<code>`` or index (``XU030`` / ``endeks:XU030``) to a watchlist
    (created if missing); returns the watchlist id. ``ValueError`` for an unknown reference."""
    kind, value = _parse_watch_ref(conn, ref)
    row = conn.execute(select(s.watchlist).where(s.watchlist.c.name == watchlist)).first()
    now = now_utc()
    if row is None:
        wid = uuid7()
        values: dict[str, Any] = {"instrument_ids": [], "sectors": [], "indices": []}
        values[_WATCH_COLUMN[kind]] = [value]
        conn.execute(s.watchlist.insert().values(id=wid, name=watchlist, created_at=now, **values))
        return wid
    column = _WATCH_COLUMN[kind]
    current = list(getattr(row, column) or [])
    if value not in current:
        current.append(value)
        conn.execute(
            update(s.watchlist).where(s.watchlist.c.id == row.id).values({column: current, "updated_at": now})
        )
    return row.id


def unwatch(conn: Connection, ref: str, watchlist: str = "Ana") -> bool:
    """Remove a reference from a watchlist; True if something was removed."""
    kind, value = _parse_watch_ref(conn, ref)
    row = conn.execute(select(s.watchlist).where(s.watchlist.c.name == watchlist)).first()
    if row is None:
        return False
    column = _WATCH_COLUMN[kind]
    current = list(getattr(row, column) or [])
    if kind == "sector":
        kept = [x for x in current if normalize_tr(x) != normalize_tr(value)]
    else:
        kept = [x for x in current if x != value]
    if len(kept) == len(current):
        return False
    conn.execute(
        update(s.watchlist).where(s.watchlist.c.id == row.id).values({column: kept, "updated_at": now_utc()})
    )
    return True


def watchlists(conn: Connection) -> list[dict[str, Any]]:
    """All watchlists with tickers resolved for display."""
    out = []
    for wl in _watchlist_rows(conn):
        iids = list(wl.instrument_ids or [])
        out.append(
            {
                "id": wl.id,
                "name": wl.name,
                "instrument_ids": iids,
                "tickers": [ticker_for(conn, i) or i for i in iids],
                "sectors": list(wl.sectors or []),
                "indices": list(wl.indices or []),
            }
        )
    return out


# --------------------------------------------------------------------------------------------- reconcile


def reconcile(
    conn: Connection, observed: dict[str, Decimal], portfolio_id: str | None = None
) -> list[ReconcileDiff]:
    """Compare İşCep quantities (ticker → quantity) with today's papi quantities.

    Returns only differences. A held ticker missing from ``observed`` counts as 0 at İşCep; an unknown
    ticker yields a diff with ``instrument_id=None`` that ``apply_reconcile`` skips.
    """
    pid = portfolio_id or default_portfolio_id(conn)
    today = ist_date()
    held = {h.instrument_id: h.quantity for h in ledger(conn, pid, today).values() if h.quantity != 0}
    seen: dict[str, Decimal] = {}
    diffs: list[ReconcileDiff] = []
    for ticker, qty in observed.items():
        q = Decimal(qty)
        match = _exact_instrument(conn, ticker, today)
        if match is None:
            if q != 0:
                diffs.append(
                    ReconcileDiff(
                        pid, None, _ticker_key(ticker), ZERO, q, _unknown_ticker_message(conn, ticker, today)
                    )
                )
            continue
        seen[match.instrument_id] = seen.get(match.instrument_id, ZERO) + q
    for iid in list(dict.fromkeys([*held, *seen])):
        papi_q, obs_q = held.get(iid, ZERO), seen.get(iid, ZERO)
        if papi_q == obs_q:
            continue
        diff = ReconcileDiff(pid, iid, ticker_for(conn, iid, today) or iid, papi_q, obs_q)
        diff.hint = _reconcile_hint(conn, pid, iid, diff.delta)
        diffs.append(diff)
    diffs.sort(key=lambda x: x.ticker)
    return diffs


def _reconcile_hint(conn: Connection, portfolio_id: str, instrument_id: str, delta: Decimal) -> str:
    rows = conn.execute(
        select(s.pending_action.c.id, s.pending_action.c.kind, s.pending_action.c.proposal).where(
            s.pending_action.c.portfolio_id == portfolio_id,
            s.pending_action.c.instrument_id == instrument_id,
            s.pending_action.c.status == "expected",
        )
    ).all()
    for r in rows:
        prop = r.proposal or {}
        same_delta = Decimal(str(prop.get("quantity", "0"))) == delta
        if prop.get("txn_type") in ("bonus_shares", "rights_subscription", "split") and same_delta:
            return f"Fark bekleyen kurumsal işlemle açıklanıyor ({r.kind}): papi portfolio apply {r.id}"
    if rows:
        return f"Bekleyen kurumsal işlem var ({rows[0].kind}): papi portfolio pending"
    if delta > 0:
        return "İşCep'te papi'den fazla pay: kaydedilmemiş alış ya da bedelsiz olabilir."
    return "İşCep'te papi'den az pay: kaydedilmemiş satış olabilir."


def apply_reconcile(conn: Connection, diffs: list[ReconcileDiff]) -> list[str]:
    """Record a ``reconcile_adjustment`` (quantity = delta, today) per applicable diff; returns txn ids."""
    today = ist_date()
    by_portfolio: dict[str, tuple[list[str], list[str]]] = {}
    out: list[str] = []
    for diff in diffs:
        if diff.instrument_id is None or diff.delta == 0:
            continue
        p = ProposedTxn(
            portfolio_id=diff.portfolio_id,
            instrument_id=diff.instrument_id,
            type="reconcile_adjustment",
            quantity=diff.delta,
            trade_date=today,
            source="reconcile",
            note=(
                f"İşCep mutabakatı: papi {fmt_qty(diff.papi_quantity)} → "
                f"İşCep {fmt_qty(diff.observed_quantity)}"
            ),
        )
        tid, pid, iid = _insert_txn(conn, p)
        txns, iids = by_portfolio.setdefault(pid, ([], []))
        txns.append(tid)
        iids.append(iid or "")
        out.append(tid)
    for pid, (txns, iids) in by_portfolio.items():
        _publish_changed(conn, pid, iids, txns, "reconcile")
    return out


# ------------------------------------------------------------------------------------- corporate actions

_PROPOSABLE = {"bonus", "dividend_stock", "rights", "split", "reverse_split", "dividend_cash"}
_KIND_LABELS = {
    "bonus": "bedelsiz",
    "dividend_stock": "bedelsiz (pay temettüsü)",
    "rights": "bedelli",
    "split": "bölünme",
    "reverse_split": "birleşme (ters bölünme)",
    "dividend_cash": "nakit temettü",
}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _build_proposal(ca: Any, ticker: str, h: Holding) -> dict[str, Any] | None:
    """Proposal JSON (Decimals as strings) or None when the corporate action lacks the needed numbers."""
    held = h.quantity
    details = ca.details or {}
    factor = ca.factor
    ex = fmt_date(ca.ex_date)
    base: dict[str, Any] = {
        "ca_type": ca.type,
        "ex_date": ca.ex_date.isoformat() if ca.ex_date else None,
        "held": _s(held),
        "avg_cost_before": _s(h.avg_cost),
    }
    label = _KIND_LABELS[ca.type]
    if ca.type in ("bonus", "dividend_stock"):
        if factor is None or factor <= 0:
            return None
        exact = held * factor
        new = exact.to_integral_value(rounding=ROUND_DOWN)
        after = held + new
        avg_after = h.cost_total / after if after > 0 else None
        text = (
            f"{ticker} {label} %{fmt_num(factor * 100, 2, trim=True)} ({ex}): "
            f"{fmt_qty(held)} → {fmt_qty(after)} pay."
        )
        if h.avg_cost is not None:
            text += f" Ort. maliyet {fmt_num(h.avg_cost)} → {fmt_num(avg_after)}."
        if exact != new:
            text += f" Kesir ({fmt_qty(exact - new)} pay) aracı kurum uygulamasına göre nakde çevrilebilir."
        return {
            **base,
            "factor": _s(factor),
            "txn_type": "bonus_shares",
            "quantity": _s(new),
            "quantity_after": _s(after),
            "fraction": _s(exact - new),
            "avg_cost_after": _s(avg_after),
            "text_tr": text,
        }
    if ca.type == "rights":
        if factor is None or factor <= 0:
            return None
        price = ca.cash_amount if ca.cash_amount is not None else _dec(details.get("subscription_price"))
        assumed = price is None
        if assumed:
            price = Decimal(1)  # BIST bedelli sermaye artırımı: rüçhan hakkı nominal 1 TL'den kullanılır
        new = (held * factor).to_integral_value(rounding=ROUND_DOWN)
        cash = new * price
        after = held + new
        avg_after = (h.cost_total + cash) / after if after > 0 else None
        text = (
            f"{ticker} {label} %{fmt_num(factor * 100, 2, trim=True)} ({ex}): {fmt_qty(new)} yeni pay için "
            f"{fmt_num(cash)} TL gerekir (pay başı {fmt_num(price)} TL). Uygula = rüçhan hakkını kullan "
            f"({fmt_qty(held)} → {fmt_qty(after)} pay); kullanmayacaksanız reddedin."
        )
        if assumed:
            text += " Rüçhan fiyatı duyuruda yok; nominal 1 TL varsayıldı."
        return {
            **base,
            "factor": _s(factor),
            "txn_type": "rights_subscription",
            "quantity": _s(new),
            "quantity_after": _s(after),
            "price": _s(price),
            "price_assumed_nominal": assumed,
            "cash_needed": _s(cash),
            "avg_cost_after": _s(avg_after),
            "choices": ["subscribe", "sell_rights", "lapse"],
            "text_tr": text,
        }
    if ca.type in ("split", "reverse_split"):
        if factor is None or factor <= 0:
            return None
        after = (held * factor).to_integral_value(rounding=ROUND_DOWN)
        delta = after - held
        if delta == 0:
            return None
        avg_after = h.cost_total / after if after > 0 else None
        text = (
            f"{ticker} {label} ×{fmt_num(factor, 6, trim=True)} ({ex}): "
            f"{fmt_qty(held)} → {fmt_qty(after)} pay."
        )
        if h.avg_cost is not None:
            text += f" Ort. maliyet {fmt_num(h.avg_cost)} → {fmt_num(avg_after)}."
        return {
            **base,
            "factor": _s(factor),
            "txn_type": "split",
            "quantity": _s(delta),
            "quantity_after": _s(after),
            "avg_cost_after": _s(avg_after),
            "text_tr": text,
        }
    # dividend_cash
    gross_ps = ca.cash_amount
    if gross_ps is None or gross_ps <= 0:
        return None
    gross = held * gross_ps
    net_ps = _dec(details.get("net_amount"))
    rate = _dec(details.get("withholding_rate"))
    if net_ps is not None:
        withholding: Decimal | None = held * (gross_ps - net_ps)
    elif rate is not None:
        withholding = gross * rate
    else:
        withholding = None
    text = (
        f"{ticker} {label} ({ex}): {fmt_qty(held)} pay × {fmt_num(gross_ps, 4, trim=True)} TL "
        f"= brüt {fmt_num(gross)} TL"
    )
    if withholding is not None:
        text += f", stopaj {fmt_num(withholding)} TL, net {fmt_num(gross - withholding)} TL."
    else:
        text += "; stopaj oranı duyuruda yok, brüt kaydedilir."
    return {
        **base,
        "txn_type": "dividend_cash",
        "quantity": _s(held),
        "price": _s(gross_ps),
        "gross": _s(gross),
        "tax": _s(withholding),
        "net": _s(gross - withholding) if withholding is not None else None,
        "text_tr": text,
    }


def propose_for_corporate_action(conn: Connection, corporate_action_id: str) -> list[str]:
    """Create one ``pending_action`` per portfolio holding the instrument before ``ex_date``
    (today when the date is unknown). Idempotent; returns the pending action ids for this action."""
    ca = conn.execute(
        select(s.corporate_action).where(s.corporate_action.c.id == corporate_action_id)
    ).first()
    if ca is None:
        raise ValueError(f"kurumsal işlem bulunamadı: {corporate_action_id}")
    if ca.status == "cancelled" or ca.type not in _PROPOSABLE:
        return []
    as_of = ca.ex_date - timedelta(days=1) if ca.ex_date else ist_date()
    ticker = ticker_for(conn, ca.instrument_id, ca.ex_date) or ca.instrument_id
    ids: list[str] = []
    for (pid, _iid), h in sorted(ledger(conn, None, as_of, [ca.instrument_id]).items()):
        existing = conn.execute(
            select(s.pending_action.c.id).where(
                s.pending_action.c.portfolio_id == pid,
                s.pending_action.c.corporate_action_id == ca.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            ids.append(existing)
            continue
        if h.quantity <= 0:
            continue
        proposal = _build_proposal(ca, ticker, h)
        if proposal is None:
            log.warning("corporate action %s (%s) lacks factor/amount; no proposal", ca.id, ca.type)
            continue
        paid = uuid7()
        conn.execute(
            s.pending_action.insert().values(
                id=paid,
                portfolio_id=pid,
                instrument_id=ca.instrument_id,
                corporate_action_id=ca.id,
                kind=ca.type,
                status="expected",
                proposal=proposal,
                created_at=now_utc(),
            )
        )
        ids.append(paid)
    return ids


def pending_actions(conn: Connection, status: str = "expected") -> list[dict[str, Any]]:
    """Pending actions with ticker and ex_date (``status="all"`` for every state), oldest first."""
    pa, ca = s.pending_action, s.corporate_action
    q = select(pa, ca.c.ex_date).select_from(pa.outerjoin(ca, ca.c.id == pa.c.corporate_action_id))
    if status != "all":
        q = q.where(pa.c.status == status)
    rows = conn.execute(q.order_by(pa.c.created_at, pa.c.id)).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["ticker"] = ticker_for(conn, r.instrument_id, r.ex_date)
        out.append(d)
    return out


def _pending_row(conn: Connection, pending_action_id: str) -> Any:
    row = conn.execute(select(s.pending_action).where(s.pending_action.c.id == pending_action_id)).first()
    if row is None:
        raise ValueError(f"bekleyen işlem bulunamadı: {pending_action_id}")
    if row.status != "expected":
        raise ValueError(f"bekleyen işlem zaten sonuçlanmış: {row.status}")
    return row


def apply_pending_action(
    conn: Connection, pending_action_id: str, *, quantity_override: Decimal | None = None
) -> str:
    """Record the derived transaction (``source=derived:<corporate_action_id>``) and mark it applied.

    ``quantity_override`` replaces the transaction quantity: shares received (bonus), shares subscribed
    (rights), share delta (split) or eligible shares (dividend; withholding scales with it).
    """
    pa = _pending_row(conn, pending_action_id)
    prop = pa.proposal or {}
    ca = conn.execute(
        select(s.corporate_action).where(s.corporate_action.c.id == pa.corporate_action_id)
    ).first()
    quantity = quantity_override if quantity_override is not None else Decimal(prop["quantity"])
    tax = _dec(prop.get("tax")) or ZERO
    if (
        quantity_override is not None
        and prop["txn_type"] == "dividend_cash"
        and Decimal(prop["quantity"]) > 0
    ):
        tax = tax * quantity_override / Decimal(prop["quantity"])
    source = f"derived:{pa.corporate_action_id}" if pa.corporate_action_id else "manual"
    p = ProposedTxn(
        portfolio_id=pa.portfolio_id,
        instrument_id=pa.instrument_id,
        type=prop["txn_type"],
        quantity=quantity,
        price=_dec(prop.get("price")),
        trade_date=(ca.ex_date if ca is not None and ca.ex_date else None) or ist_date(),
        source=source,
        note=prop.get("text_tr"),
    )
    tid, pid, iid = _insert_txn(conn, p, tax=tax)
    conn.execute(
        update(s.pending_action)
        .where(s.pending_action.c.id == pa.id)
        .values(status="applied", transaction_id=tid, resolved_at=now_utc())
    )
    _publish_changed(conn, pid, [iid or ""], [tid], "corporate_action")
    return tid


def dismiss_pending_action(conn: Connection, pending_action_id: str) -> None:
    """Mark an expected pending action dismissed (e.g. rights not used)."""
    pa = _pending_row(conn, pending_action_id)
    conn.execute(
        update(s.pending_action)
        .where(s.pending_action.c.id == pa.id)
        .values(status="dismissed", resolved_at=now_utc())
    )


def scan_corporate_actions(conn: Connection, *, lookback_days: int = 60) -> list[str]:
    """Propose pending actions for non-cancelled corporate actions whose ex_date lies within the last
    ``lookback_days`` up to today (holders are fixed by then). Returns all pending action ids touched."""
    today = ist_date()
    ca_ids = conn.execute(
        select(s.corporate_action.c.id).where(
            s.corporate_action.c.type.in_(sorted(_PROPOSABLE)),
            s.corporate_action.c.status != "cancelled",
            s.corporate_action.c.ex_date.is_not(None),
            s.corporate_action.c.ex_date <= today,
            s.corporate_action.c.ex_date >= today - timedelta(days=lookback_days),
        )
    ).scalars()
    out: list[str] = []
    for ca_id in list(ca_ids):
        out.extend(propose_for_corporate_action(conn, ca_id))
    return out
