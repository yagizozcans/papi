from __future__ import annotations

from decimal import Decimal

import pytest

from papi.verify.numbers import Pool, extract, untraced
from papi.verify.phrases import find_advice

SOURCE = (
    "Sipariş bedelleri toplamı KDV hariç 1.325.000 (BirMilyonÜçYüzYirmiBeşBin) EUR 'dır. Bugün (27.10.2026) "
    "brüt 3,0700000 TL, net 2,6095000 TL, oran %307. Toplam 4,2 milyar TL. XU100 endeksi, II-15.1 Tebliğ. "
    "Saat 17:30."
)


@pytest.fixture
def pool() -> Pool:
    return Pool().add_text(SOURCE)


@pytest.mark.parametrize(
    "text",
    [
        "1.325.000 EUR",
        "EUR 1,325,000",  # English grouping
        "1,3 milyon EUR",  # rounded with scale word
        "1.3 million EUR",
        "27 Ekim 2026",
        "27/10/2026",
        "2026-10-27",
        "brüt 3,07 TL",  # rounding at the writer's precision
        "net 2,61 TL",
        "%307",
        "4,2 milyar TL",
        "4.200.000.000 TL",
        "XU100 ve II-15.1",  # identifiers are not numbers
        "saat 18:45",  # clock times are ignored
        "2026 yılında",  # year part of a source date
        "4. çeyrek sonuçları",  # ordinals are not amounts
        "3'üncü yıl",
    ],
)
def test_traced(pool: Pool, text: str) -> None:
    assert untraced(text, pool) == []


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        ("1.500.000 EUR", "1.500.000"),
        ("1,5 milyon EUR", "1,5"),
        ("28.10.2026", "2026-10-28"),
        ("brüt 3,5 TL", "3,5"),
        ("%15 temettü verimi", "15"),
        ("5 milyar TL", "5"),
        ("Tutar 2.000.000. Bu nedenle", "2.000.000"),  # sentence-final number is still a number
    ],
)
def test_untraced(pool: Pool, text: str, raw: str) -> None:
    assert [u.raw for u in untraced(text, pool)] == [raw]


def test_extract_readings_and_markers() -> None:
    ex = extract("[S12] 1.234 adet ve [KB:tr:spk:II-15.1:m5] 62.500 TL; 1,5 milyon")
    values = [[n.value for n in r] for r in ex.numbers]
    assert values[0] == [Decimal("1234"), Decimal("1.234")]  # Turkish thousands / English decimal
    assert values[1] == [Decimal("62500"), Decimal("62.500")]
    assert values[2][0] == Decimal("1500000") and values[2][1] == Decimal("1.5")
    assert len(ex.numbers) == 3  # marker digits ignored


def test_pool_exact_for_payload_values() -> None:
    p = Pool().add_text("Sipariş bedeli 1.325.000,00 EUR")
    assert p.has_exact(Decimal("1325000"))
    assert not p.has_exact(Decimal("1325001"))


# --------------------------------------------------------------------------- advice phrases


@pytest.mark.parametrize(
    "text",
    [
        "Pay satışı tamamlandı.",
        "Şirket iştirak hisselerinin satın alınmasına karar verdi.",
        "Satın alma bedeli 10 milyon TL.",
        "satın alınan paylar",
        "Geri alınan paylar ve hisse geri alımı",
        "Yatırım tavsiyesi değildir.",
        "Alınan kararlar genel kurula sunulacak.",
        "Tütün üreticisi şirket",
        "Satış gelirleri arttı; alım satım bildirimi.",
        "The company agreed to buy a 51% stake in X.",
        "Share buyback programme and buy-back execution.",
        "Sell-side analysts expect results.",
        "Selling shareholders sold 1m shares.",
        "Hedefi 2027 yılında kapasiteyi artırmak.",
    ],
)
def test_no_false_positives(text: str) -> None:
    assert find_advice(text) == []


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Hemen alın!", "alın"),
        ("Bu hisseyi satın.", "satın (emir)"),
        ("Hisseleri satın alın.", "satın alın"),
        ("Pozisyonu tutun.", "tutun"),
        ("Hisse için AL tavsiyesi veriyoruz.", "al/sat/tut tavsiyesi"),
        ("Hedef fiyatımız 55 TL.", "hedef fiyat"),
        ("Hisse kesin yükselir.", "kesin yükselir/düşer"),
        ("Almalısınız.", "almalısınız/satmalısınız"),
        ("Strong buy.", "strong buy/sell"),
        ("We recommend buying the shares.", "recommend buying/selling"),
        ("Investors should sell now.", "you should buy/sell"),
        ("Our target price is 55.", "target price"),
        ("Now is the time to buy.", "time to buy/sell"),
        ("Buy the stock.", "Buy/Sell (emir)"),
    ],
)
def test_advice_detected(text: str, label: str) -> None:
    assert label in [h.label for h in find_advice(text)]
