"""Structure parser on real excerpts: numbering, edge cases, stable paths, flags."""

from __future__ import annotations

from papi.sources.legal.extract import Line
from papi.sources.legal.structure import bent_index, parse, parse_articles
from tests.kb.helpers import fixture_lines


def _doc(name: str, shape: str = "articles"):
    return parse(fixture_lines(name), shape)  # type: ignore[arg-type]


def test_ii151_articles_fikra_bent_altbent():
    doc = _doc("ii15_1_consolidated.txt")
    by = doc.by_path()
    assert doc.header.official_code == "II-15.1"
    assert doc.header.gazette is not None and doc.header.gazette.number == "28891"
    # MADDE head line split from the first fıkra; heading taken from the preceding line
    assert by["m1"].heading == "Amaç"
    assert by["m1/f1"].text.startswith("(1) Bu Tebliğin amacı")
    # Turkish bent letters incl. ç, ğ, ı, ö and alt bentler 1), 2), 3)
    for path in ("m4/f1/ç", "m4/f1/ğ", "m4/f1/ı", "m4/f1/i", "m4/f1/ö", "m4/f1/g/3", "m4/f1/k/4"):
        assert path in by, path
    # "(1) numaralı alt bentte" at a line start is a reference, not a new fıkra or alt bent
    assert "(1) numaralı alt bentte" in by["m4/f1/g/2"].text
    assert by["m4/f1/g/2"].text.startswith("2) İdari sorumluluğu bulunan kişiler veya")
    # a fıkra added by amendment keeps its number; the article text contains all children
    assert by["m12/f4"].own_text.startswith("(4) (Ek:RG-13/2/2018-30331) (Değişik:RG-17/11/2018-30598)")
    assert by["m12"].text.count("\n") >= 4
    # geçici maddeler get their own sequence
    assert {"gm1", "gm2", "gm3"} <= set(by)
    assert by["gm3"].heading.startswith("Nitelikli yatırımcıya satış suretiyle")
    assert by["m30"].heading == "Yürürlük"


def test_ii151_footnotes_and_annex_are_not_provision_text():
    doc = _doc("ii15_1_consolidated.txt")
    by = doc.by_path()
    assert by["m31/f1"].text == "(1) Bu Tebliğ hükümlerini Kurul yürütür."
    assert any("teselsül ettirilmiştir" in f for f in doc.footnotes)
    assert doc.appendix_lines[0] == "(EK)"
    assert all(not p.path.startswith("EK") for p in doc.provisions)


def test_ii151_leading_markers_scope():
    by = _doc("ii15_1_consolidated.txt").by_path()
    m2 = by["m2"].markers
    assert [m.raw for m in m2] == ["(Değişik:RG-10/2/2017-29975)"] and m2[0].leading
    m11 = by["m11"].markers[0]
    assert m11.kind == "degisik" and m11.what == "başlığı ile birlikte" and m11.leading
    assert [m.kind for m in by["m12/f4"].markers] == ["ek", "degisik"]


def test_law6362_kisim_bolum_context_and_special_numbering():
    doc = _doc("law6362_excerpt.txt")
    by = doc.by_path()
    assert by["m1"].context["kisim"] == "Birinci Kısım – Genel Hükümler"
    assert by["m1"].context["bolum"] == "Birinci Bölüm – Amaç, Kapsam ve Tanımlar"
    assert by["m106"].context["bolum"] == "İkinci Bölüm – Sermaye Piyasası Suçları"
    assert by["m106"].heading == "Bilgi suistimali"
    # MADDE 31/A → m31A; double-letter bent aa)
    assert "m31A/f1" in by and by["m31A"].label == "Madde 31/A"
    assert by["m31A"].markers[0].what == "madde" and by["m31A"].markers[0].kind == "ek"
    # MADDE 78's first paragraph carries no "(1)": implicit fıkra 1, flagged for review
    assert by["m78/f1"].flags == ["implicit_fikra_1"]
    assert by["m78/f1"].text.startswith("Kurul, merkezî takas")
    assert by["m78/f2"].text.startswith("(2) (Değişik fıkra:")
    # annex "(1) SAYILI LİSTE" ends the body
    assert doc.appendix_lines[0] == "(1) SAYILI LİSTE"
    assert "m151/f1" in by and "SAYILI" not in by["m151/f1"].text


def test_law6362_double_letter_bent():
    lines = fixture_lines("law6362_excerpt.txt")
    doc = parse_articles(lines)
    # the excerpt jumps from bent b) to aa): accepted as a bent of fıkra 1 only via the sequence rule
    # (aa follows z); here it stays continuation text of b) — positions are never invented
    assert "m3/f1/aa" not in doc.by_path()
    assert bent_index("aa") == bent_index("z") + 1
    assert bent_index("çç") == bent_index("cc") + 1
    assert bent_index("ab") is None


def test_kap_yonerge_lowercase_madde_and_gecici():
    by = _doc("kap_bildirim_yonergesi_excerpt.txt").by_path()
    assert by["m1"].heading == "Amaç ve Kapsam"
    assert by["m1/f1"].text.startswith("(1) Bu Yönergenin amacı")
    assert by["gm1"].label == "Geçici Madde 1"
    assert by["gm1"].heading == "KAP Kullanıcı Sertifikası’na İlişkin Geçiş Hükmü"
    assert by["m30/f1"].text == "(1) Bu Yönerge yayınlandığı tarihte yürürlüğe girer."


def test_bist_yonerge_toc_dropped_and_revision_table_is_appendix():
    doc = _doc("bist_pay_piyasasi_yonergesi_excerpt.txt")
    by = doc.by_path()
    # (the excerpt joins two cuts, so the cut-off "Dayanak" heading line precedes this one)
    assert by["m30"].heading.endswith(
        "Bir sermaye piyasası aracına ilişkin işlemlerin geçici olarak durdurulması"
    )
    assert {"m30/f1/a", "m30/f1/ç"} <= set(by)
    assert doc.appendix_lines[0] == "PAY PİYASASI YÖNERGESİ REVİZYONLARI"
    # revision tables quote "MADDE 4 (1)" etc. — never parsed as provisions
    assert [p.path for p in doc.provisions if p.depth == 0] == ["m1", "m2", "m30", "m48", "m49"]


def test_rehber_sections_with_toc_and_wrapped_heading():
    doc = _doc("ozel_durumlar_rehberi_excerpt.txt", "sections")
    by = doc.by_path()
    assert [p.path for p in doc.provisions] == ["I", "II", "II.1", "II.2", "II.2.1", "II.2.2", "IV", "V"]
    assert by["II.2.1"].heading == "Bilginin Niteliklerinin Belirli Olması"
    assert by["V"].heading == "ÖZEL DURUMLARIN İHRAÇÇININ İNTERNET SİTESİNDE YAYINLANMASI"
    assert by["II"].text.startswith("II. İÇSEL BİLGİLER")
    assert "II.2.1" in [a for p in doc.provisions if p.path == "II.2.1" for a in [p.path]]
    assert by["II.2.1"].ancestors == ["II.2", "II"]
    assert any(f.startswith("toc_headings_not_found:") for f in doc.flags)  # excerpt omits sections


def test_guideline_letter_parts_and_items():
    kar = _doc("kar_payi_rehberi_excerpt.txt", "sections")
    assert [p.path for p in kar.provisions][:3] == ["A", "A.1", "A.2"]
    oni = _doc("onemli_nitelikteki_islemler_rehberi_excerpt.txt", "sections")
    assert [p.path for p in oni.provisions] == ["giris", "1", "2"]
    assert "numbered_items_as_sections" in oni.flags
    assert oni.by_path()["2"].text.startswith("2) Tebliğ’in 6’ncı maddesinin")


def test_sequence_rules_flag_gaps_and_reject_out_of_order():
    lines = [
        Line("Amaç"),
        Line("MADDE 1 – (1) Birinci fıkra."),
        Line("(3) Üçüncü fıkra; ikinci fıkra metinden çıkarılmış."),
        Line("b) bendi gibi bir atıf satırı, bent değil."),
        Line("Kapsam"),
        Line("MADDE 2 – (1) Kapsam fıkrası:"),
        Line("a) Birinci bent,"),
        Line("c) Üçüncü bent."),
    ]
    doc = parse_articles(lines)
    by = doc.by_path()
    assert by["m1/f3"].flags == ["fikra_gap:1->3"]
    assert "b) bendi gibi" in by["m1/f3"].text  # reference words: not a bent
    assert by["m2/f1/c"].flags == ["bent_gap:a->c"]
    assert by["m2"].heading == "Kapsam"


def test_citation_paths_are_stable_across_parses():
    a = [p.path for p in _doc("ii15_1_consolidated.txt").provisions]
    b = [p.path for p in _doc("ii15_1_consolidated.txt").provisions]
    assert a == b and len(a) == len(set(a))
