"""L1 mechanical calculators (05 §2) — pure, deterministic, ``Decimal`` throughout.

Every calculator has a ``*_ID`` constant (``formula_id@version``) and returns a :class:`Mechanical`
dict ``{"formula_id", "inputs", "result"}`` — one entry of the 05 §6 ``mechanical`` array. Values are
kept as ``Decimal`` (exact); :func:`to_json` converts an entry for JSON output (Decimal → float).
Bump the version suffix whenever a formula's semantics change.

Conventions (TR pack):
- Ratios are fractions of *existing* shares: ``b`` bonus (bedelsiz) shares per held share
  (%100 bedelsiz → ``b = 1``), ``r`` new rights (bedelli) shares per held share, ``S`` the
  subscription price per new share (usually nominal 1 TL).
- Combined bedelli + bedelsiz follows Borsa İstanbul's theoretical-price convention, where both ratios
  are applied to the same cum price in a single step:
  ``P_ex = (P_cum − D + r·S) / (1 + r + b)`` (``D`` = gross cash dividend per share paid the same day,
  0 if none).
- Money is never rounded inside a calculator; callers present values with their own rounding.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, TypedDict

Num = Decimal | int | str | float

BONUS_TERP_ID = "bonus_issue_terp@1"
RIGHTS_TERP_ID = "rights_issue_terp@1"
COMBINED_TERP_ID = "combined_rights_bonus_terp@1"
CASH_DIVIDEND_ID = "cash_dividend_ex@1"
DILUTION_ID = "dilution@1"
POSITION_BONUS_ID = "position_after_bonus@1"
POSITION_RIGHTS_ID = "position_after_rights@1"
BUYBACK_CAPACITY_ID = "buyback_capacity@1"
TENDER_OFFER_SPREAD_ID = "tender_offer_spread@1"
CONTRACT_MATERIALITY_ID = "contract_materiality@1"
PRICE_LIMIT_PATH_ID = "price_limit_path@1"

ALL_FORMULA_IDS: tuple[str, ...] = (
    BONUS_TERP_ID,
    RIGHTS_TERP_ID,
    COMBINED_TERP_ID,
    CASH_DIVIDEND_ID,
    DILUTION_ID,
    POSITION_BONUS_ID,
    POSITION_RIGHTS_ID,
    BUYBACK_CAPACITY_ID,
    TENDER_OFFER_SPREAD_ID,
    CONTRACT_MATERIALITY_ID,
    PRICE_LIMIT_PATH_ID,
)

# Dividend withholding (stopaj) on cash dividends to resident individuals: 15 % since the
# Presidential Decision of 22.12.2024 (previously 10 %). Pass the rate explicitly when it differs.
TR_DIVIDEND_WITHHOLDING = Decimal("0.15")

# Borsa İstanbul equity market price ticks: (lower bound inclusive, tick). Used by price_limit_path
# when ``round_to_tick=True``. Daily limit prices are rounded to the tick toward the base price.
BIST_TICK_TABLE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("0"), Decimal("0.01")),
    (Decimal("20"), Decimal("0.02")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.10")),
    (Decimal("250"), Decimal("0.25")),
    (Decimal("500"), Decimal("0.50")),
    (Decimal("1000"), Decimal("1.00")),
    (Decimal("2500"), Decimal("2.50")),
)

MAX_LIMIT_SESSIONS = 1000

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


class Mechanical(TypedDict):
    formula_id: str
    inputs: dict[str, Any]
    result: dict[str, Any]


def D(value: Num) -> Decimal:
    """Exact ``Decimal`` from int/str/Decimal; floats go through ``repr`` (42.10 → Decimal('42.1'))."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def _entry(formula_id: str, inputs: dict[str, Any], result: dict[str, Any]) -> Mechanical:
    return {"formula_id": formula_id, "inputs": inputs, "result": result}


def _positive(name: str, value: Decimal) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0 (got {value})")


def _non_negative(name: str, value: Decimal) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0 (got {value})")


def _fraction(name: str, value: Decimal) -> None:
    if not (ZERO <= value < ONE):
        raise ValueError(f"{name} must be in [0, 1) (got {value})")


def to_json(entry: Mechanical) -> dict[str, Any]:
    """JSON-ready copy of an entry: ``Decimal`` → ``float`` (recursively)."""

    def conv(v: Any) -> Any:
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [conv(x) for x in v]
        return v

    return conv(dict(entry))


# --------------------------------------------------------------------------- theoretical prices


def bonus_terp(p_cum: Num, b: Num) -> Mechanical:
    """Bedelsiz: ``P_ex = P_cum / (1 + b)``; ``factor`` = P_ex / P_cum (price adjustment factor)."""
    p, bb = D(p_cum), D(b)
    _positive("P_cum", p)
    _non_negative("b", bb)
    p_ex = p / (ONE + bb)
    return _entry(BONUS_TERP_ID, {"P_cum": p, "b": bb}, {"P_ex": p_ex, "factor": ONE / (ONE + bb)})


def rights_terp(p_cum: Num, r: Num, s: Num) -> Mechanical:
    """Bedelli: ``P_ex = (P_cum + r·S) / (1 + r)``.

    ``right_value`` = theoretical value of one subscription right (one new share): ``max(P_ex − S, 0)``.
    """
    p, rr, ss = D(p_cum), D(r), D(s)
    _positive("P_cum", p)
    _non_negative("r", rr)
    _non_negative("S", ss)
    p_ex = (p + rr * ss) / (ONE + rr)
    return _entry(
        RIGHTS_TERP_ID,
        {"P_cum": p, "r": rr, "S": ss},
        {"P_ex": p_ex, "factor": p_ex / p, "right_value": max(p_ex - ss, ZERO)},
    )


def combined_terp(p_cum: Num, r: Num, s: Num, b: Num, dividend: Num = 0) -> Mechanical:
    """Simultaneous bedelli + bedelsiz (+ optional same-day gross cash dividend), BIST convention.

    ``P_ex = (P_cum − D + r·S) / (1 + r + b)``. With ``b = 0`` and ``D = 0`` this equals
    :func:`rights_terp`; with ``r = 0`` and ``D = 0`` it equals :func:`bonus_terp`.
    """
    p, rr, ss, bb, dd = D(p_cum), D(r), D(s), D(b), D(dividend)
    _positive("P_cum", p)
    for name, v in (("r", rr), ("S", ss), ("b", bb), ("D", dd)):
        _non_negative(name, v)
    if dd >= p:
        raise ValueError("dividend must be below P_cum")
    p_ex = (p - dd + rr * ss) / (ONE + rr + bb)
    return _entry(
        COMBINED_TERP_ID,
        {"P_cum": p, "r": rr, "S": ss, "b": bb, "D": dd},
        {"P_ex": p_ex, "factor": p_ex / p, "right_value": max(p_ex - ss, ZERO) if rr > 0 else ZERO},
    )


def cash_dividend(p_cum: Num, d_gross: Num, withholding_rate: Num = TR_DIVIDEND_WITHHOLDING) -> Mechanical:
    """Nakit temettü: ``P_ex = P_cum − D_gross``; yields on the cum price, net after stopaj."""
    p, dg, w = D(p_cum), D(d_gross), D(withholding_rate)
    _positive("P_cum", p)
    _non_negative("D_gross", dg)
    _fraction("withholding_rate", w)
    if dg >= p:
        raise ValueError("D_gross must be below P_cum")
    d_net = dg * (ONE - w)
    return _entry(
        CASH_DIVIDEND_ID,
        {"P_cum": p, "D_gross": dg, "withholding_rate": w},
        {
            "P_ex": p - dg,
            "D_net": d_net,
            "withholding_per_share": dg - d_net,
            "gross_yield": dg / p,
            "net_yield": d_net / p,
            "factor": (p - dg) / p,
        },
    )


# --------------------------------------------------------------------------- share counts


def dilution(
    shares_before: Num,
    new_shares: Num,
    *,
    my_shares: Num | None = None,
    net_income: Num | None = None,
) -> Mechanical:
    """Ownership and EPS dilution from issuing ``new_shares`` to others (holder does not take part).

    ``dilution_pct`` = new / (before + new). With ``my_shares``: ownership before/after.
    With ``net_income``: EPS before/after on the same earnings.
    """
    nb, nn = D(shares_before), D(new_shares)
    _positive("shares_before", nb)
    _non_negative("new_shares", nn)
    after = nb + nn
    inputs: dict[str, Any] = {"shares_before": nb, "new_shares": nn}
    result: dict[str, Any] = {"shares_after": after, "dilution_pct": nn / after * HUNDRED}
    if my_shares is not None:
        m = D(my_shares)
        _non_negative("my_shares", m)
        inputs["my_shares"] = m
        result["ownership_before_pct"] = m / nb * HUNDRED
        result["ownership_after_pct"] = m / after * HUNDRED
    if net_income is not None:
        ni = D(net_income)
        inputs["net_income"] = ni
        result["eps_before"] = ni / nb
        result["eps_after"] = ni / after
        result["eps_change_pct"] = (nb / after - ONE) * HUNDRED
    return _entry(DILUTION_ID, inputs, result)


def _split_whole(x: Decimal, lot: Decimal) -> tuple[Decimal, Decimal]:
    whole = (x / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
    return whole, x - whole


def position_after_bonus(
    shares: Num, b: Num, *, avg_cost: Num | None = None, lot_size: Num = 1
) -> Mechanical:
    """My shares after bedelsiz: ``n·(1+b)``. Fractions below ``lot_size`` are reported separately
    (``fraction``), as the broker credits whole shares; cost basis is unchanged, so the average cost
    is spread over the exact new count."""
    n, bb, lot = D(shares), D(b), D(lot_size)
    _non_negative("shares", n)
    _non_negative("b", bb)
    _positive("lot_size", lot)
    exact_after = n * (ONE + bb)
    new_exact = n * bb
    new_whole, fraction = _split_whole(new_exact, lot)
    inputs: dict[str, Any] = {"shares": n, "b": bb, "lot_size": lot}
    result: dict[str, Any] = {
        "shares_after": n + new_whole,
        "shares_after_exact": exact_after,
        "new_shares": new_whole,
        "fraction": fraction,
    }
    if avg_cost is not None:
        ac = D(avg_cost)
        inputs["avg_cost"] = ac
        result["cost_total"] = ac * n
        result["avg_cost_after"] = ac * n / exact_after if exact_after else None
    return _entry(POSITION_BONUS_ID, inputs, result)


def position_after_rights(
    shares: Num,
    r: Num,
    s: Num,
    *,
    subscribe: bool,
    p_cum: Num | None = None,
    avg_cost: Num | None = None,
    lot_size: Num = 1,
) -> Mechanical:
    """My shares after bedelli.

    Subscribe: ``n·(1+r)`` shares (new shares floored to ``lot_size``), ``cash_needed = new·S``.
    Do not subscribe: shares stay ``n``; I hold ``n·r`` rights worth ``n·r·(P_ex − S)`` at the
    theoretical price (needs ``p_cum``), which is also the value given up if they lapse.
    """
    n, rr, ss, lot = D(shares), D(r), D(s), D(lot_size)
    _non_negative("shares", n)
    _non_negative("r", rr)
    _non_negative("S", ss)
    _positive("lot_size", lot)
    rights = n * rr
    inputs: dict[str, Any] = {"shares": n, "r": rr, "S": ss, "subscribe": subscribe, "lot_size": lot}
    result: dict[str, Any] = {"rights": rights}
    p_ex: Decimal | None = None
    if p_cum is not None:
        terp = rights_terp(p_cum, rr, ss)["result"]
        p_ex = terp["P_ex"]
        inputs["P_cum"] = D(p_cum)
        result["P_ex"] = p_ex
        result["right_value"] = terp["right_value"]
        result["rights_value_total"] = rights * terp["right_value"]
    if subscribe:
        new_whole, fraction = _split_whole(rights, lot)
        cash = new_whole * ss
        result.update(shares_after=n + new_whole, new_shares=new_whole, fraction=fraction, cash_needed=cash)
        if avg_cost is not None:
            ac = D(avg_cost)
            inputs["avg_cost"] = ac
            total = ac * n + cash
            result["cost_total"] = total
            result["avg_cost_after"] = total / (n + new_whole) if n + new_whole else None
    else:
        result.update(shares_after=n, new_shares=ZERO, fraction=ZERO, cash_needed=ZERO)
        if avg_cost is not None:
            ac = D(avg_cost)
            inputs["avg_cost"] = ac
            result["cost_total"] = ac * n
            result["avg_cost_after"] = ac
    return _entry(POSITION_RIGHTS_ID, inputs, result)


# --------------------------------------------------------------------------- programmes & offers


def buyback_capacity(
    amount_try: Num,
    price: Num,
    *,
    free_float_shares: Num | None = None,
    total_shares: Num | None = None,
    adv_shares: Num | None = None,
) -> Mechanical:
    """Geri alım kapasitesi: shares = amount / price; % of free float / of capital; days of ADV
    (``adv_shares`` = average daily traded volume in shares)."""
    amt, p = D(amount_try), D(price)
    _non_negative("amount_try", amt)
    _positive("price", p)
    shares = amt / p
    inputs: dict[str, Any] = {"amount_try": amt, "price": p}
    result: dict[str, Any] = {"shares": shares}
    if free_float_shares is not None:
        ff = D(free_float_shares)
        _positive("free_float_shares", ff)
        inputs["free_float_shares"] = ff
        result["pct_free_float"] = shares / ff * HUNDRED
    if total_shares is not None:
        ts = D(total_shares)
        _positive("total_shares", ts)
        inputs["total_shares"] = ts
        result["pct_capital"] = shares / ts * HUNDRED
    if adv_shares is not None:
        adv = D(adv_shares)
        _positive("adv_shares", adv)
        inputs["adv_shares"] = adv
        result["days_of_adv"] = shares / adv
    return _entry(BUYBACK_CAPACITY_ID, inputs, result)


def tender_offer_spread(offer_price: Num, current_price: Num) -> Mechanical:
    """Pay alım teklifi: offer (or KB-rule floor) price vs. current price.

    ``spread`` = offer − current; ``spread_pct`` = spread / current × 100 (positive = premium).
    """
    o, c = D(offer_price), D(current_price)
    _positive("offer_price", o)
    _positive("current_price", c)
    spread = o - c
    return _entry(
        TENDER_OFFER_SPREAD_ID,
        {"offer_price": o, "current_price": c},
        {"spread": spread, "spread_pct": spread / c * HUNDRED, "is_premium": spread > 0},
    )


def contract_materiality(
    value: Num,
    *,
    currency: str = "TRY",
    fx_rate: Num | None = None,
    vat_included: bool | None = None,
    vat_rate: Num | None = None,
    ttm_revenue_try: Num | None = None,
    market_cap_try: Num | None = None,
) -> Mechanical:
    """İş ilişkisi / sözleşme büyüklüğü: ``value_TRY / TTM revenue`` and ``/ market cap`` (in %).

    Non-TRY values need ``fx_rate`` (TRY per unit at the publish-date rate). VAT is explicit:
    ``vat_included=True`` with ``vat_rate`` strips VAT; ``True`` without a rate is rejected;
    ``None`` means the disclosure does not say (flag ``vat`` = ``unknown``; value used as given).
    """
    v = D(value)
    _non_negative("value", v)
    cur = currency.upper()
    inputs: dict[str, Any] = {"value": v, "currency": cur, "vat_included": vat_included}
    if cur == "TRY":
        if fx_rate is not None and D(fx_rate) != ONE:
            raise ValueError("fx_rate given for a TRY amount")
        value_try = v
        fx_applied = False
    else:
        if fx_rate is None:
            raise ValueError(f"fx_rate required for {cur}")
        fx = D(fx_rate)
        _positive("fx_rate", fx)
        inputs["fx_rate"] = fx
        value_try = v * fx
        fx_applied = True
    if vat_included is True:
        if vat_rate is None:
            raise ValueError("vat_rate required when vat_included=True")
        vr = D(vat_rate)
        _fraction("vat_rate", vr)
        inputs["vat_rate"] = vr
        value_ex_vat = value_try / (ONE + vr)
        vat_flag = "included"
    else:
        value_ex_vat = value_try
        vat_flag = "excluded" if vat_included is False else "unknown"
    result: dict[str, Any] = {
        "value_try": value_try,
        "value_try_ex_vat": value_ex_vat,
        "fx_applied": fx_applied,
        "vat": vat_flag,
    }
    if ttm_revenue_try is not None:
        rev = D(ttm_revenue_try)
        _positive("ttm_revenue_try", rev)
        inputs["ttm_revenue_try"] = rev
        result["pct_ttm_revenue"] = value_ex_vat / rev * HUNDRED
    if market_cap_try is not None:
        mc = D(market_cap_try)
        _positive("market_cap_try", mc)
        inputs["market_cap_try"] = mc
        result["pct_market_cap"] = value_ex_vat / mc * HUNDRED
    return _entry(CONTRACT_MATERIALITY_ID, inputs, result)


# --------------------------------------------------------------------------- price limits


def bist_tick(price: Decimal) -> Decimal:
    """Price tick for ``price`` from :data:`BIST_TICK_TABLE`."""
    tick = BIST_TICK_TABLE[0][1]
    for lower, t in BIST_TICK_TABLE:
        if price >= lower:
            tick = t
    return tick


def _round_to_tick(price: Decimal, *, up_limit: bool) -> Decimal:
    tick = bist_tick(price)
    rounding = ROUND_FLOOR if up_limit else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=rounding) * tick


def price_limit_path(
    current: Num, target: Num, *, limit_pct: Num = 10, round_to_tick: bool = False
) -> Mechanical:
    """Fewest sessions to reach ``target`` from ``current`` if every session closes at its daily
    limit (base price of each session = previous limit close).

    ``limit_pct`` comes from the segment config of the pack (Yıldız/Ana Pazar 10 %). With
    ``round_to_tick`` each limit price is rounded to the BIST tick toward the base price (upper limit
    down, lower limit up). ``path`` lists the limit closes, ``sessions`` = len(path) (0 when already
    there); ``reachable`` is False when tick rounding stalls the path or it exceeds
    :data:`MAX_LIMIT_SESSIONS`.
    """
    c, t, lim = D(current), D(target), D(limit_pct)
    _positive("current", c)
    _positive("target", t)
    if not (ZERO < lim < HUNDRED):
        raise ValueError("limit_pct must be in (0, 100)")
    up = t >= c
    step = ONE + lim / HUNDRED if up else ONE - lim / HUNDRED
    path: list[Decimal] = []
    price = c
    reachable = True
    while (price < t) if up else (price > t):
        nxt = price * step
        if round_to_tick:
            nxt = _round_to_tick(nxt, up_limit=up)
        if nxt == price or len(path) >= MAX_LIMIT_SESSIONS:
            reachable = False
            break
        path.append(nxt)
        price = nxt
    return _entry(
        PRICE_LIMIT_PATH_ID,
        {"current": c, "target": t, "limit_pct": lim, "round_to_tick": round_to_tick},
        {
            "direction": "up" if up else "down",
            "sessions": len(path) if reachable else None,
            "path": path,
            "reachable": reachable,
            "max_move_pct": (price / c - ONE) * HUNDRED,
        },
    )
