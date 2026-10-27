from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from papi.sim import calculators as c

D = Decimal
GRID_P = ["0.37", "1", "8.5", "42.10", "286.25", "1250"]
GRID_RATIO = ["0", "0.1", "0.25", "0.5", "1", "2", "4.5"]
GRID_S = ["0.01", "1", "10", "50"]


def test_formula_ids_are_versioned_and_unique() -> None:
    assert len(set(c.ALL_FORMULA_IDS)) == len(c.ALL_FORMULA_IDS)
    assert all(re.fullmatch(r"[a-z_]+@\d+", f) for f in c.ALL_FORMULA_IDS)


def test_entry_shape_and_json() -> None:
    e = c.rights_terp(42.10, 1, 1)
    assert set(e) == {"formula_id", "inputs", "result"}
    j = c.to_json(e)
    assert j == json.loads(json.dumps(j))
    assert j["formula_id"] == "rights_issue_terp@1"
    assert j["inputs"] == {"P_cum": 42.1, "r": 1.0, "S": 1.0}
    assert j["result"]["P_ex"] == 21.55


def test_float_inputs_are_exact() -> None:
    assert c.D(42.10) == D("42.1")
    assert c.D("0.1") + c.D(0.2) == D("0.3")
    with pytest.raises(TypeError):
        c.D(True)


# ---------------------------------------------------------------- worked examples


def test_rights_worked_example_prd() -> None:
    r = c.rights_terp(D("42.10"), 1, 1)["result"]
    assert r["P_ex"] == D("21.55")
    assert r["right_value"] == D("20.55")


def test_bonus_100pct_halves_price() -> None:
    r = c.bonus_terp(D("74.00"), 1)["result"]
    assert r["P_ex"] == D("37.00")
    assert r["factor"] == D("0.5")


def test_bonus_asels_2023_real() -> None:
    # ASELS %100 bedelsiz, cum close 74.00 (2023-08-24) → theoretical 37.00; first ex close was 37.78
    assert c.bonus_terp("74.00", "1")["result"]["P_ex"] == D("37")


def test_combined_bist_convention() -> None:
    # %50 bedelli at 1 TL + %100 bedelsiz on a 20 TL stock: (20 + 0.5·1) / (1 + 0.5 + 1) = 8.20
    r = c.combined_terp("20", "0.5", "1", "1")["result"]
    assert r["P_ex"] == D("8.2")
    assert r["right_value"] == D("7.2")
    # with a same-day 0.50 gross dividend: (20 − 0.5 + 0.5) / 2.5 = 8.00
    assert c.combined_terp("20", "0.5", "1", "1", dividend="0.5")["result"]["P_ex"] == D("8")


def test_cash_dividend_net_yield() -> None:
    r = c.cash_dividend("100", "5", withholding_rate="0.15")["result"]
    assert r["P_ex"] == D("95")
    assert r["D_net"] == D("4.25")
    assert r["withholding_per_share"] == D("0.75")
    assert r["gross_yield"] == D("0.05")
    assert r["net_yield"] == D("0.0425")
    assert c.cash_dividend("100", "5")["inputs"]["withholding_rate"] == c.TR_DIVIDEND_WITHHOLDING


def test_dilution() -> None:
    r = c.dilution(1000, 250, my_shares=100, net_income="500")["result"]
    assert r["shares_after"] == 1250
    assert r["dilution_pct"] == D("20")
    assert r["ownership_before_pct"] == D("10")
    assert r["ownership_after_pct"] == D("8")
    assert r["eps_before"] == D("0.5") and r["eps_after"] == D("0.4")
    assert r["eps_change_pct"] == D("-20")


def test_position_after_bonus_with_fraction() -> None:
    r = c.position_after_bonus(1201, "0.25", avg_cost="40")["result"]
    assert r["shares_after_exact"] == D("1501.25")
    assert r["new_shares"] == 300 and r["fraction"] == D("0.25")
    assert r["shares_after"] == 1501
    assert r["cost_total"] == D("48040")
    assert r["avg_cost_after"] == D("48040") / D("1501.25")


def test_position_after_rights_subscribe_matches_prd_example() -> None:
    r = c.position_after_rights(1200, 1, 1, subscribe=True, p_cum="42.10", avg_cost="30")["result"]
    assert r["shares_after"] == 2400 and r["cash_needed"] == D("1200")
    assert r["P_ex"] == D("21.55")
    assert r["avg_cost_after"] == (D("36000") + D("1200")) / 2400


def test_position_after_rights_not_subscribing() -> None:
    r = c.position_after_rights(1200, "0.5", 1, subscribe=False, p_cum="42.10", avg_cost="30")["result"]
    p_ex = (D("42.10") + D("0.5")) / D("1.5")
    assert r["shares_after"] == 1200 and r["cash_needed"] == 0
    assert r["rights"] == 600
    assert r["rights_value_total"] == 600 * (p_ex - 1)
    assert r["avg_cost_after"] == D("30")


def test_buyback_capacity() -> None:
    r = c.buyback_capacity(
        "50000000", "25", free_float_shares="40000000", total_shares="100000000", adv_shares="500000"
    )["result"]
    assert r["shares"] == D("2000000")
    assert r["pct_free_float"] == D("5")
    assert r["pct_capital"] == D("2")
    assert r["days_of_adv"] == D("4")


def test_tender_offer_spread() -> None:
    r = c.tender_offer_spread("55", "50")["result"]
    assert r["spread"] == D("5") and r["spread_pct"] == D("10") and r["is_premium"] is True
    assert c.tender_offer_spread("45", "50")["result"]["is_premium"] is False


def test_contract_materiality_fx_and_vat() -> None:
    r = c.contract_materiality(
        "10000000",
        currency="usd",
        fx_rate="34.20",
        vat_included=True,
        vat_rate="0.20",
        ttm_revenue_try="5700000000",
        market_cap_try="11400000000",
    )["result"]
    assert r["value_try"] == D("342000000")
    assert r["value_try_ex_vat"] == D("285000000")
    assert r["fx_applied"] is True and r["vat"] == "included"
    assert r["pct_ttm_revenue"] == D("5")
    assert r["pct_market_cap"] == D("2.5")


def test_contract_materiality_flags_and_validation() -> None:
    assert c.contract_materiality("100")["result"]["vat"] == "unknown"
    assert c.contract_materiality("100", vat_included=False)["result"]["vat"] == "excluded"
    with pytest.raises(ValueError):
        c.contract_materiality("100", currency="EUR")
    with pytest.raises(ValueError):
        c.contract_materiality("100", vat_included=True)
    with pytest.raises(ValueError):
        c.contract_materiality("100", fx_rate="30")


def test_price_limit_path_up_and_down() -> None:
    r = c.price_limit_path("100", "130")["result"]
    assert r["sessions"] == 3 and r["direction"] == "up"
    assert r["path"] == [D("110.0"), D("121.00"), D("133.100")]
    d = c.price_limit_path("100", "80")["result"]
    assert d["sessions"] == 3 and d["direction"] == "down"
    assert c.price_limit_path("100", "100")["result"]["sessions"] == 0


def test_price_limit_path_tick_rounding() -> None:
    r = c.price_limit_path("19.99", "30", round_to_tick=True)["result"]
    # 19.99·1.1 = 21.989 → tick 0.02 → 21.98 (rounded down toward base)
    assert r["path"][0] == D("21.98")
    assert all((p / c.bist_tick(p)) == (p / c.bist_tick(p)).to_integral_value() for p in r["path"])
    d = c.price_limit_path("0.05", "0.01", round_to_tick=True)["result"]
    # 0.05·0.9 = 0.045 → rounded up to 0.05: the lower limit cannot move the price
    assert d["reachable"] is False and d["sessions"] is None


@pytest.mark.parametrize(
    ("price", "tick"),
    [("19.99", "0.01"), ("20", "0.02"), ("99.95", "0.05"), ("250", "0.25"), ("2500", "2.50")],
)
def test_bist_tick(price: str, tick: str) -> None:
    assert c.bist_tick(D(price)) == D(tick)


@pytest.mark.parametrize(
    "call",
    [
        lambda: c.bonus_terp(0, 1),
        lambda: c.bonus_terp(10, -1),
        lambda: c.rights_terp(10, 1, -1),
        lambda: c.cash_dividend(10, 10),
        lambda: c.cash_dividend(10, 1, withholding_rate=1),
        lambda: c.combined_terp(10, 1, 1, 1, dividend=10),
        lambda: c.dilution(0, 1),
        lambda: c.buyback_capacity(1, 0),
        lambda: c.tender_offer_spread(0, 1),
        lambda: c.price_limit_path(10, 20, limit_pct=0),
        lambda: c.position_after_bonus(10, 1, lot_size=0),
    ],
)
def test_invalid_inputs_raise(call) -> None:
    with pytest.raises(ValueError):
        call()


# ---------------------------------------------------------------- property grids


@pytest.mark.parametrize("p", GRID_P)
@pytest.mark.parametrize("b", GRID_RATIO)
def test_bonus_properties(p: str, b: str) -> None:
    r = c.bonus_terp(p, b)["result"]
    # market value preserved: holding 1 share before == (1 + b) shares after
    assert abs(r["P_ex"] * (1 + D(b)) - D(p)) < D("1e-20")
    assert r["P_ex"] <= D(p)
    assert r["factor"] == r["P_ex"] / D(p) or abs(r["factor"] - r["P_ex"] / D(p)) < D("1e-25")


@pytest.mark.parametrize("p", GRID_P)
@pytest.mark.parametrize("r_", GRID_RATIO)
@pytest.mark.parametrize("s", GRID_S)
def test_rights_properties(p: str, r_: str, s: str) -> None:
    res = c.rights_terp(p, r_, s)["result"]
    pp, rr, ss = D(p), D(r_), D(s)
    # value conservation: old holding + cash paid == new holding at P_ex
    assert abs(res["P_ex"] * (1 + rr) - (pp + rr * ss)) < D("1e-20")
    # P_ex lies between S and P_cum
    assert min(pp, ss) - D("1e-20") <= res["P_ex"] <= max(pp, ss) + D("1e-20")
    if rr == 0:
        assert res["P_ex"] == pp
    # combined with b = 0 equals rights; combined with r = 0 equals bonus
    assert abs(c.combined_terp(p, r_, s, 0)["result"]["P_ex"] - res["P_ex"]) < D("1e-20")
    assert abs(c.combined_terp(p, 0, s, r_)["result"]["P_ex"] - c.bonus_terp(p, r_)["result"]["P_ex"]) < D(
        "1e-20"
    )


@pytest.mark.parametrize("n", [0, 1, 7, 100, 1201])
@pytest.mark.parametrize("r_", GRID_RATIO)
@pytest.mark.parametrize("s", GRID_S)
def test_position_rights_properties(n: int, r_: str, s: str) -> None:
    p_cum = "42.10"
    sub = c.position_after_rights(n, r_, s, subscribe=True, p_cum=p_cum)["result"]
    no = c.position_after_rights(n, r_, s, subscribe=False, p_cum=p_cum)["result"]
    assert sub["new_shares"] + sub["fraction"] == n * D(r_)
    assert sub["new_shares"] == int(sub["new_shares"]) and 0 <= sub["fraction"] < 1
    assert sub["cash_needed"] == sub["new_shares"] * D(s)
    assert no["shares_after"] == n and no["cash_needed"] == 0
    # wealth equivalence at theoretical prices (no fractions): subscribe == keep + sell rights
    p_ex = sub["P_ex"]
    if sub["fraction"] == 0 and D(s) <= p_ex:
        lhs = sub["shares_after"] * p_ex - sub["cash_needed"]
        rhs = no["shares_after"] * p_ex + no["rights_value_total"]
        assert abs(lhs - rhs) < D("1e-18")


@pytest.mark.parametrize("n", [0, 1, 3, 1000, 1201])
@pytest.mark.parametrize("b", GRID_RATIO)
def test_position_bonus_properties(n: int, b: str) -> None:
    r = c.position_after_bonus(n, b, avg_cost="10")["result"]
    assert r["shares_after"] + r["fraction"] == r["shares_after_exact"]
    assert r["shares_after"] >= n
    if r["shares_after_exact"]:
        assert abs(r["avg_cost_after"] * r["shares_after_exact"] - r["cost_total"]) < D("1e-20")


@pytest.mark.parametrize("p", GRID_P)
@pytest.mark.parametrize("frac", ["0", "0.01", "0.1", "0.5"])
@pytest.mark.parametrize("w", ["0", "0.1", "0.15"])
def test_dividend_properties(p: str, frac: str, w: str) -> None:
    d = D(p) * D(frac)
    r = c.cash_dividend(p, d, withholding_rate=w)["result"]
    assert r["P_ex"] + d == D(p)
    assert r["D_net"] + r["withholding_per_share"] == d
    assert r["net_yield"] <= r["gross_yield"]


@pytest.mark.parametrize("current", ["0.5", "9.99", "42.10", "286.25", "1250"])
@pytest.mark.parametrize("mult", ["0.3", "0.75", "1", "1.05", "2", "5"])
@pytest.mark.parametrize("limit", ["10", "20"])
def test_price_limit_path_properties(current: str, mult: str, limit: str) -> None:
    cur, tgt = D(current), D(current) * D(mult)
    r = c.price_limit_path(cur, tgt, limit_pct=limit)["result"]
    n, path = r["sessions"], r["path"]
    assert r["reachable"] and n == len(path)
    if n:
        last = path[-1]
        assert (last >= tgt) if tgt >= cur else (last <= tgt)
        before = path[-2] if n > 1 else cur
        assert (before < tgt) if tgt >= cur else (before > tgt)  # minimal
        step = 1 + D(limit) / 100 if tgt >= cur else 1 - D(limit) / 100
        assert all(abs(b - a * step) < D("1e-20") for a, b in zip([cur, *path], path, strict=False))
    # tick-rounded path never moves faster than the unrounded one
    rt = c.price_limit_path(cur, tgt, limit_pct=limit, round_to_tick=True)["result"]
    if rt["reachable"]:
        assert rt["sessions"] >= n


@pytest.mark.parametrize("before", ["1000", "123456"])
@pytest.mark.parametrize("new", ["0", "1", "500", "1000000"])
def test_dilution_properties(before: str, new: str) -> None:
    r = c.dilution(before, new, my_shares="100", net_income="1000")["result"]
    assert r["ownership_after_pct"] <= r["ownership_before_pct"]
    assert r["eps_after"] <= r["eps_before"]
    assert abs(r["ownership_after_pct"] / r["ownership_before_pct"] - (1 - r["dilution_pct"] / 100)) < D(
        "1e-20"
    )
