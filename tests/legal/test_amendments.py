"""Amendment lists, inline markers and entry-into-force dating on real texts."""

from __future__ import annotations

from datetime import date

from papi.sources.legal.amendments import (
    GazetteRef,
    base_effective,
    find_markers,
    parse_revision_appendix,
    resolve_marker,
)
from papi.sources.legal.structure import parse_articles
from tests.kb.helpers import fixture_lines


def test_teblig_amendment_list_and_rg_markers():
    doc = parse_articles(fixture_lines("ii15_1_consolidated.txt"))
    listed = doc.amendment_list
    assert [(a.ordinal, a.teblig_code, a.gazette) for a in listed] == [
        (1, "II-15.1.a", GazetteRef(date(2017, 2, 10), "29975")),
        (2, "II-15.1.b", GazetteRef(date(2018, 2, 13), "30331")),
        (3, "II-15.1.c", GazetteRef(date(2018, 11, 17), "30598")),
    ]
    mk = find_markers("(4) (Ek:RG-13/2/2018-30331) (Değişik:RG-17/11/2018-30598) Bir gerçek")
    assert [resolve_marker(m, listed) for m in mk] == [
        (date(2018, 2, 13), "II-15.1.b – RG 13/02/2018-30331"),
        (date(2018, 11, 17), "II-15.1.c – RG 17/11/2018-30598"),
    ]


def test_law_markers_resolve_through_the_list():
    doc = parse_articles(fixture_lines("law6362_excerpt.txt"))
    listed = doc.amendment_list
    assert [a.ordinal for a in listed] == list(range(1, 15))
    by_no = {a.ordinal: a for a in listed}
    assert by_no[5].law_no == "6637" and by_no[5].gazette == GazetteRef(date(2015, 4, 7), "29319")
    assert by_no[8].khk_no == "684"
    assert by_no[4].court_decision and by_no[4].decision_no == "2013/133"
    assert by_no[14].law_no == "7222" and by_no[14].gazette == GazetteRef(date(2020, 2, 25), "31050")

    def res(text: str):
        return resolve_marker(find_markers(text)[0], listed)

    # adoption date in the marker, publication date from the list
    assert res("(2) (Değişik fıkra: 27/3/2015 tarihli ve 6637 sayılı Kanunun 11 inci maddesi ile) X")[
        0
    ] == date(2015, 4, 7)
    assert res("(Değişik bent: 28.11.2017 tarihli ve 7061 sayılı Kanunun 107 nci maddesi ile) X")[0] == date(
        2017, 12, 5
    )
    assert res("(Ek: 28/11/2017-7061/109 md.) (Değişik cümle:27/12/2018-7159/8 md.) X")[0] == date(
        2017, 12, 5
    )
    # 7159 is not named in the list: matched by the gazette date of entry 12
    assert resolve_marker(find_markers("(Değişik cümle:27/12/2018-7159/8 md.)")[0], listed)[0] == date(
        2018, 12, 27
    )
    assert res("ı) (Değişik bent: 703 sayılı KHK’nın 165/a maddesi ile) İlgili Bakan")[0] == date(2018, 7, 9)
    # Constitutional Court annulment: matched by decision number K.2013/133
    assert res(
        "(İptal ikinci cümle: Anayasa Mahkemesi’nin 14/11/2013 tarihli ve E.: 2013/24, K.: 2013/133 "
        "sayılı Kararı ile.)"
    )[0] == date(2014, 7, 22)
    # an act missing from the list stays undated
    d, raw = res("aa) (Ek bent: 26.6.2024 tarihli ve 7518 sayılı Kanunun 1 inci maddesi ile) Cüzdan")
    assert d is None and raw.startswith("(Ek bent:")


def test_base_effective_forms():
    g = GazetteRef(date(2014, 1, 23), "28891")
    assert base_effective("(1) Bu Tebliğ yayımı tarihinden bir ay sonra yürürlüğe girer.", g)[0] == date(
        2014, 2, 23
    )
    assert base_effective("(1) Bu Kanun yayımı tarihinde yürürlüğe girer.", GazetteRef(date(2012, 12, 30)))[
        0
    ] == date(2012, 12, 30)
    assert base_effective("MADDE 48 – (1) Bu Yönerge, 30/11/2015 tarihinde yürürlüğe girer.", None)[
        0
    ] == date(2015, 11, 30)
    d, excluded, _ = base_effective(
        "(1) Bu Tebliğin 29 uncu maddesinin üçüncü fıkrası 1/1/2014 tarihinde, diğer maddeleri ise yayım "
        "tarihinden bir ay sonra yürürlüğe girer.",
        GazetteRef(date(2013, 6, 22)),
    )
    assert d == date(2013, 7, 22) and excluded == {"29"}
    assert base_effective("(1) Bu Yönerge yayınlandığı tarihte yürürlüğe girer.", None)[0] is None
    assert base_effective(None, g) == (None, set(), "no Yürürlük article")


def test_bist_revision_appendix():
    doc = parse_articles(fixture_lines("bist_pay_piyasasi_yonergesi_excerpt.txt"))
    revs = parse_revision_appendix(doc.appendix_lines)
    assert [(r.date, sorted(r.articles)) for r in revs] == [
        (date(2016, 10, 11), ["m4", "m9"]),
        (date(2019, 4, 3), ["m30"]),
        (date(2019, 9, 27), []),  # the excerpt ends right after this block's date line
    ]
