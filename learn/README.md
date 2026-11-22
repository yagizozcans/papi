# learn/ — papi verisiyle finans + ML öğrenme yolu

Her modül önce burada notebook olarak öğrenilir, sonra sağlam kısmı papi'ye modül + test olarak taşınır.
Notebook'lar papi'nin veritabanını **salt-okunur** açar (`papidata.py`); papi'yi bozamazsın.

```bash
uv run --group learn jupyter lab learn/      # repo kökünden
```

`TODO` etiketli hücreleri sen yazarsın; altındaki `K.…` hücresi kontrol eder (`kontrol.py`) ve ✅ basar.
Kontroller çözümü söylemez, sadece neyin yanlış olduğunu söyler. Takılınca ipucu iste.

| Modül | Konu | Ön koşul | Süre (tahmini) | papi'deki karşılığı |
|---|---|---|---|---|
| [00](00_analist_gozu.md) | Analistin gözü: 7 soru, sektör göstergeleri, tez yazmak (kodsuz) | — | 4–6 akşam | analist ajanının bağlam paketi |
| [01](01_getiri_risk.ipynb) | Bedelsiz düzeltmesi, log getiri, TL vs $, oynaklık, beta, max drawdown | — | 3–4 akşam | M3 corporate-action adjustment |
| [02](02_monte_carlo.ipynb) | GBM, Itô, Cholesky, bootstrap, VaR/CVaR, VaR geriye dönük testi | 01 | 3–4 akşam | M3 forecasts + outcome tracker |
| [03](03_olay_calismasi.ipynb) | Olay çalışması: KAP haberleri sonrası anormal getiri (CAR) | 01, KAP geçmişi* | 2–3 akşam | M3 event-study engine |
| [04](04_xgboost.ipynb) | XGBoost, walk-forward, embargo, sızıntı, baseline, strateji testi | 01 (03 iyi olur) | 4–5 akşam | M3/M4 tahminler |

\* 03 için KAP geçmişi lazım (eve dönünce, saatler sürer):
`uv run papi kap backfill --from 2021-01-01 --to 2026-10-26 --tickers THYAO,TUPRS,CCOLA`

**Önerilen sıra:** Antalya'da (2–20 Kasım) 00 + 01; 3Ç26 bilançoları da o dönemde geliyor, 00'ın 3. görevi
için birebir. Dönünce 02 → KAP backfill'i başlat → 04 → 03.

**Veri:** fiyatlar 2015'ten beri (THYAO, TUPRS, CCOLA, XU100; TI1 2021'den). Yeni sembol eklemek için:
`uv run papi prices sync --tickers ASELS --from 2015-01-01`. USD/TRY Yahoo'dan çekilip `data/`'da önbelleklenir.

**CFA bağlantısı:** 00 ≈ Level I *Financial Statement Analysis* + *Equity Valuation*; 01–02 ≈ *Quantitative
Methods* + *Portfolio Management* (risk, VaR). Notebook'lardaki soruları CFA soruları gibi yaz-cevapla.
