"""Notebook görevlerinin kontrolleri. Her biri senin fonksiyonunu çağırır; hata varsa ne beklendiğini söyler.

Kontroller çözümü vermez: bilinen sonuçları (sentetik veri, gerçek bedelsiz tarihleri) ve matematiksel
özellikleri (ölçekleme, sınırlar) test eder. Geçince "✅" basar.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd


def _ok(name: str) -> None:
    print(f"✅ {name}")


# --------------------------------------------------------------------------- 01 getiri & risk


def find_corporate_actions(fn: Callable, px: pd.DataFrame) -> None:
    """Bilinen bedelsizler: TUPRS 2023-04-04 (%587,8 → oran ≈ 6,88), CCOLA 2024-08-13 (%1000 → ≈ 11)."""
    tup = fn(px["TUPRS"], px["XU100"])
    cco = fn(px["CCOLA"], px["XU100"])
    thy = fn(px["THYAO"], px["XU100"])
    for name, df in (("TUPRS", tup), ("CCOLA", cco), ("THYAO", thy)):
        assert isinstance(df, pd.DataFrame) and {"log_ret", "ratio"} <= set(df.columns), (
            f"{name}: index=tarih, sütunlar 'log_ret' ve 'ratio' olan bir DataFrame döndür"
        )
    assert pd.Timestamp("2023-04-04") in tup.index, "TUPRS 2023-04-04 bedelsizini bulamadın"
    assert 6.5 < tup.loc["2023-04-04", "ratio"] < 7.3, "TUPRS oranı ≈ 6,88 olmalı (eski fiyat / yeni fiyat)"
    assert pd.Timestamp("2024-08-13") in cco.index, "CCOLA 2024-08-13 bedelsizini bulamadın"
    assert 10.3 < cco.loc["2024-08-13", "ratio"] < 11.7, "CCOLA oranı ≈ 11 olmalı"
    assert len(thy) == 0, (
        f"THYAO'da bedelsiz yok ama {list(thy.index.date)} işaretledin: gerçek çöküşleri (ör. 2016-07-18 "
        "darbe girişimi) bedelsizden ayırmak için eşiği ve piyasa (XU100) hareketini düşün"
    )
    _ok("find_corporate_actions")


def adjust_prices(fn: Callable, find: Callable, px: pd.DataFrame) -> None:
    for t in ("TUPRS", "CCOLA"):
        raw = px[t].dropna()
        adj = fn(raw, find(raw, px["XU100"]))
        assert adj.index.equals(raw.index), f"{t}: tarihler değişmemeli"
        assert np.isclose(adj.iloc[-1], raw.iloc[-1]), f"{t}: son fiyat aynı kalmalı (geriye doğru düzelt)"
        jumps = np.log(adj).diff().abs().max()
        assert jumps < 0.25, f"{t}: düzeltmeden sonra hâlâ %{jumps * 100:.0f}'lik log sıçrama var"
    _ok("adjust_prices")


def log_returns(fn: Callable, px: pd.DataFrame) -> None:
    r = fn(px)
    assert r.shape == px.shape, "aynı şekilde DataFrame döndür (ilk satır NaN)"
    s = px["XU100"].dropna()
    total = r["XU100"].dropna().sum()
    assert np.isclose(total, np.log(s.iloc[-1] / s.iloc[0])), "log getirilerin toplamı log(son/ilk) olmalı"
    _ok("log_returns")


def annual_vol(fn: Callable, r: pd.DataFrame) -> None:
    v = fn(r["XU100"])
    assert 0.12 < v < 0.45, f"XU100 yıllık oynaklığı %12–45 arası beklenir, sen %{v * 100:.1f} buldun"
    assert np.isclose(fn(2 * r["XU100"]), 2 * v), "getiri 2 katına çıkınca oynaklık da 2 katına çıkmalı"
    _ok("annual_vol")


def to_usd(fn: Callable, px: pd.DataFrame, usdtry: pd.Series) -> None:
    u = fn(px, usdtry)
    assert u.index.equals(px.index), "fiyat tarihleri korunmalı (eksik kur günlerinde son kuru kullan)"
    got = u.loc["2026-10-30", "THYAO"]
    assert abs(got - 242.2 / 49.94) < 0.1, f"THYAO 30.10.2026 ≈ 4,85 $ olmalı, sen {got:.2f} buldun"
    _ok("to_usd")


def beta(fn: Callable, r: pd.DataFrame) -> None:
    m = r["XU100"].dropna()
    assert np.isclose(fn(m, m), 1.0), "piyasanın kendine göre betası 1"
    assert np.isclose(fn(2 * m, m), 2.0), "2 × piyasa getirisinin betası 2"
    b = fn(r["THYAO"], r["XU100"])
    assert 0.5 < b < 2.0, f"THYAO betası makul aralıkta olmalı, sen {b:.2f} buldun"
    _ok("beta")


def max_drawdown(fn: Callable) -> None:
    s = pd.Series([100, 120, 60, 130, 65], index=pd.date_range("2020-01-01", periods=5))
    mdd, peak, trough = fn(s)
    assert np.isclose(mdd, -0.5), f"[100, 120, 60, 130, 65] için en büyük düşüş -%50, sen {mdd:.3f}"
    assert peak == s.index[1] and trough == s.index[2], (
        "zirve 2. gün (120), dip 3. gün (60) olmalı (eşit düşüşlerde ilki)"
    )
    _ok("max_drawdown")


# --------------------------------------------------------------------------- 02 monte carlo


def gbm_paths(fn: Callable) -> None:
    rng = np.random.default_rng(0)
    s0, mu, sigma, days, n = 100.0, 0.30, 0.40, 252, 200_000
    p = fn(s0, mu, sigma, days, n, rng)
    assert p.shape == (n, days + 1), f"şekil (n_paths, days + 1) olmalı, sen {p.shape}"
    assert np.allclose(p[:, 0], s0), "her yol s0'dan başlamalı"
    lr = np.log(p[:, -1] / s0)
    assert abs(lr.mean() - (mu - sigma**2 / 2)) < 0.01, (
        f"log(S_T/S0) ortalaması (μ − σ²/2)·T = {mu - sigma**2 / 2:.3f} olmalı, sen {lr.mean():.3f} "
        "(Itô düzeltmesini unuttun mu?)"
    )
    assert abs(lr.std() - sigma) < 0.01, f"log(S_T/S0) std'si σ·√T = {sigma} olmalı, sen {lr.std():.3f}"
    assert abs(p[:, -1].mean() / s0 - np.exp(mu)) < 0.02, "E[S_T] = S0·e^(μT) olmalı"
    _ok("gbm_paths")


def correlated_normals(fn: Callable) -> None:
    rng = np.random.default_rng(1)
    cov = np.array([[0.04, 0.018, 0.0], [0.018, 0.09, -0.01], [0.0, -0.01, 0.01]])
    mean = np.array([0.001, 0.0, 0.0005])
    x = fn(mean, cov, 400, 500, rng)
    assert x.shape == (500, 400, 3), f"şekil (n_paths, n_days, n_assets) olmalı, sen {x.shape}"
    flat = x.reshape(-1, 3)
    assert np.allclose(flat.mean(axis=0), mean, atol=0.002), "ortalamalar mean vektörüne eşit olmalı"
    assert np.allclose(np.cov(flat.T), cov, atol=0.002), "örnek kovaryans cov'a eşit olmalı (Cholesky)"
    _ok("correlated_normals")


def bootstrap_rows(fn: Callable, hist: pd.DataFrame) -> None:
    rng = np.random.default_rng(2)
    h = hist.dropna()
    x = fn(h, 20, 50, rng)
    assert x.shape == (50, 20, h.shape[1]), f"şekil (n_paths, n_days, n_assets) olmalı, sen {x.shape}"
    rows = {tuple(np.round(r, 12)) for r in h.to_numpy()}
    assert all(tuple(np.round(r, 12)) in rows for r in x.reshape(-1, h.shape[1])), (
        "her simüle gün geçmişten BÜTÜN bir satır olmalı (varlıklar arası korelasyon böyle korunur)"
    )
    _ok("bootstrap_rows")


def var_cvar(fn: Callable) -> None:
    rng = np.random.default_rng(3)
    pnl = rng.normal(0, 1000, 1_000_000)
    v95, c95 = fn(pnl, 0.95)
    assert abs(v95 - 1644.9) < 15, f"N(0, 1000) için VaR95 ≈ 1645 (pozitif kayıp), sen {v95:.0f}"
    assert abs(c95 - 2062.7) < 15, f"N(0, 1000) için CVaR95 ≈ 2063, sen {c95:.0f}"
    v99, c99 = fn(pnl, 0.99)
    assert v99 > v95 and c99 > v99, "VaR99 > VaR95 ve CVaR ≥ VaR olmalı"
    _ok("var_cvar")


def var_backtest(fn: Callable, r: pd.Series) -> None:
    out = fn(r, window=250, level=0.95)
    assert {"var", "loss", "breach"} <= set(out.columns), "sütunlar: var, loss, breach"
    first = out["var"].first_valid_index()
    assert first is not None and r.index.get_loc(first) >= 250, (
        "ilk VaR tahmini en az 250 günlük geçmişle ve SADECE o günden önceki verilerle yapılmalı"
    )
    rate = out["breach"].dropna().mean()
    assert 0.02 < rate < 0.10, f"%95 VaR'da aşım oranı ≈ %5 civarı beklenir, sen %{rate * 100:.1f}"
    _ok(f"var_backtest (aşım oranı %{rate * 100:.1f})")


# --------------------------------------------------------------------------- 03 event study


def _synthetic_market(seed: int = 4) -> tuple[pd.Series, pd.Series, pd.Timestamp]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=400)
    m = pd.Series(rng.normal(0.0005, 0.015, len(idx)), index=idx)
    s = 0.0002 + 1.3 * m + pd.Series(rng.normal(0, 0.005, len(idx)), index=idx)
    event = idx[300]
    s.iloc[300:303] += 0.04  # olay günü ve sonraki 2 gün +%4 anormal getiri
    return s, m, event


def market_model(fn: Callable) -> None:
    s, m, event = _synthetic_market()
    alpha, b = fn(s, m, event, est_window=(-250, -30))
    assert abs(b - 1.3) < 0.1, f"sentetik hissenin betası 1,3; sen {b:.2f}"
    assert abs(alpha - 0.0002) < 0.002, f"alfa ≈ 0,0002; sen {alpha:.4f}"
    _ok("market_model")


def car(fn: Callable) -> None:
    s, m, event = _synthetic_market()
    c = fn(s, m, event, est_window=(-250, -30), window=(-1, 5))
    assert isinstance(c, pd.Series) and list(c.index) == list(range(-1, 6)), (
        "index = göreli gün (-1..5), değer = kümülatif anormal getiri olan Series döndür"
    )
    assert abs(c.loc[2] - 0.12) < 0.03, f"olaydan 2 gün sonra CAR ≈ %12 olmalı, sen %{c.loc[2] * 100:.1f}"
    assert abs(c.loc[-1]) < 0.02, "olaydan önceki gün CAR ≈ 0 olmalı"
    _ok("car")


# --------------------------------------------------------------------------- 04 xgboost


def make_target(fn: Callable) -> None:
    idx = pd.bdate_range("2024-01-01", periods=12)
    stock = pd.Series(np.log([100, 101, 103, 102, 112, 108, 107, 110, 111, 109, 112, 115]), index=idx)
    mkt = pd.Series(np.log([100] * 12), index=idx)
    y = fn(stock.diff(), mkt.diff(), horizon=5)
    assert y.index.equals(idx), "aynı tarihler"
    assert y.iloc[-5:].isna().all(), "son 5 gün NaN olmalı: geleceği henüz bilmiyoruz"
    assert y.iloc[0] == 1, "1. gün: sonraki 5 günde hisse 100 → 108, piyasa yatay → 1"
    assert y.iloc[4] == 0, "5. gün: sonraki 5 günde hisse 112 → 109 → 0"
    up = pd.Series(np.log(np.linspace(100, 130, 12)), index=idx)
    same = fn(up.diff(), up.diff(), horizon=5).dropna()
    assert (same == 0).all(), "hisse = piyasa ise fazla getiri 0 → hedef 0 (hedef PİYASAYA GÖRE olmalı)"
    _ok("make_target")


def no_lookahead(fn: Callable, px: pd.DataFrame, fx: pd.Series, ticker: str) -> None:
    """Özellikler t gününde SADECE t ve öncesini kullanmalı: veriyi t'de kesince t satırı değişmemeli."""
    full = fn(px, fx, ticker)
    assert isinstance(full, pd.DataFrame) and full.index.equals(px.index), "index = px.index olan DataFrame"
    for back in (300, 60, 1):
        d = px.index[-back]
        cut = fn(px.loc[:d], fx.loc[:d], ticker)
        a, b = full.loc[d], cut.loc[d]
        bad = [c for c in full.columns if not np.isclose(a[c], b[c], equal_nan=True)]
        assert not bad, f"{d.date()} satırı geleceğe bakıyor: {bad} (shift/rolling yönünü kontrol et)"
    _ok(f"no_lookahead ({full.shape[1]} özellik)")


def walk_forward_splits(fn: Callable) -> None:
    dates = pd.bdate_range("2016-01-01", "2026-10-30")
    splits = list(fn(dates, train_years=3, test_months=6, embargo_days=5))
    assert len(splits) >= 10, f"10 yılda 6 aylık en az 10 test dönemi beklenir, sen {len(splits)}"
    prev_test_end = None
    for tr, te in splits:
        assert len(tr) and len(te), "boş train/test olmamalı"
        assert dates[tr].max() < dates[te].min(), "train tamamen testten ÖNCE olmalı"
        gap = np.busday_count(dates[tr].max().date(), dates[te].min().date())
        assert gap >= 5, f"train ile test arasında ≥ 5 iş günü boşluk (embargo) olmalı, {gap} var"
        if prev_test_end is not None:
            assert dates[te].min() > prev_test_end, "test dönemleri çakışmamalı"
        prev_test_end = dates[te].max()
    _ok(f"walk_forward_splits ({len(splits)} dönem)")
