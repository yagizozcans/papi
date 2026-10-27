from __future__ import annotations

import io
import zipfile

import pytest

from papi.core.text import normalize_tr
from papi.sources.kap.client import unwrap_java_bytes
from papi.sources.kap.parsing.doc import ParsedDocument
from papi.sources.kap.parsing.files import decode_text, fix_turkish, guess_language, parse_file, sniff
from papi.sources.kap.parsing.html import languages, parse_body

from .conftest import FIXTURES, detail_json

ALL = [
    1687728, 1687750, 1688011, 1688065, 1688079, 1688153, 1688235, 1688254, 1688272, 1688292, 1688336,
    1688363, 1688368, 1688379, 1688480, 1688504, 1688533, 1688545, 1688549, 1688568, 1688576, 1688581,
    1688591, 1688597, 1688605, 1688613, 1688615, 1688616,
]  # fmt: skip


def body(index: int, lang: str = "tr") -> ParsedDocument:
    detail = detail_json(index)
    assert detail is not None
    return parse_body(detail[0]["disclosureBody"], language=lang)


def assert_c1(doc: ParsedDocument) -> None:
    """Contract C1: spans 1..N, offsets into text, structure entries reference existing spans."""
    assert [s.ordinal for s in doc.spans] == list(range(1, len(doc.spans) + 1))
    for s in doc.spans:
        assert doc.text[s.char_start : s.char_end] == s.text
        assert s.field_path.split(":")[0] in ("form", "table", "para", "pdf")
    ordinals = {s.ordinal for s in doc.spans}
    st = doc.structure
    assert set(st) >= {"template_id", "form_fields", "tables", "paragraphs"}
    for f in st["form_fields"]:
        assert f["label_norm"] == normalize_tr(f["label"])
        assert f["span_ordinal"] is None or f["span_ordinal"] in ordinals
    for t in st["tables"]:
        assert len(t["span_ordinals"]) == len(t["rows"])
        for row, ords in zip(t["rows"], t["span_ordinals"], strict=True):
            assert len(row) == len(ords)
            for cell, o in zip(row, ords, strict=True):
                if "page" in t:  # PDF tables point at the text line holding the row
                    assert o is None or cell in doc.spans[o - 1].text
                    continue
                assert (o is None) == (cell == "")
                if o is not None:
                    assert doc.spans[o - 1].text == cell
    for p in st["paragraphs"]:
        assert doc.spans[p["span_ordinal"] - 1].text == p["text"]


def field(doc: ParsedDocument, label_norm_prefix: str) -> dict:
    return next(f for f in doc.structure["form_fields"] if f["label_norm"].startswith(label_norm_prefix))


@pytest.mark.parametrize("index", ALL)
def test_every_fixture_parses_to_c1(index: int) -> None:
    doc = body(index)
    assert_c1(doc)
    assert doc.template_id and doc.template_id.startswith("tbl_")
    assert doc.spans and doc.text


def test_oda_general_material_event() -> None:
    doc = body(1688616)
    assert doc.template_id == "tbl_oda-10000_Material-Event-Disclosure-General"
    corr = next(f for f in doc.structure["form_fields"] if f["field_id"] == "oda_CorrectionAnnouncementFlag")
    assert corr["label"] == "Yapılan Açıklama Düzeltme mi?" and corr["value"] == "Hayır (No)"
    expl = next(f for f in doc.structure["form_fields"] if f["field_id"] == "oda_ExplanationTextBlock")
    assert expl["label"] == "Açıklamalar" and len(expl["span_ordinals"]) == 7
    assert doc.structure["paragraphs"][0]["text"].startswith("Sermaye Piyasası Kurulu'nun 27.10.2026")
    assert doc.spans[expl["span_ordinal"] - 1].field_path == "para:0"
    assert "Related Companies" not in doc.text  # English labels excluded from the TR document


def test_english_version_is_a_separate_document() -> None:
    detail = detail_json(1688616)[0]
    assert languages(detail["disclosureBody"]) == ["tr", "en"]
    en = body(1688616, "en")
    assert_c1(en)
    assert en.language == "en"
    assert field(en, "correction notification flag")["field_id"] == "oda_CorrectionAnnouncementFlag"
    assert en.structure["paragraphs"][0]["text"].startswith("In the announcement published in the bulletin")
    assert "Açıklamalar" not in en.text
    assert languages(detail_json(1688504)[0]["disclosureBody"]) == ["tr"]


def test_dkb_share_transaction_relay() -> None:
    doc = body(1688272)
    assert doc.template_id == "tbl_GENEL_DUYURU_GONDERIMI"
    assert field(doc, "ilgili sirketler")["value"] == "MHRGY"
    assert field(doc, "yapilan aciklama duzeltme mi")["value"] == "Hayır"
    assert "Corpus Sigorta" in field(doc, "aciklamalar")["value"]


def test_oda_share_transaction_dimensional_table() -> None:
    doc = body(1688533)
    assert doc.template_id == "tbl_oda-10400_Shares-Transaction-Notification"
    assert field(doc, "ilgili sirketler")["value"] == "[METRO]"
    (t,) = doc.structure["tables"]
    assert t["caption"] == "Pay Alım Satım Bilgileri"
    assert t["header"][0] == "İşlem Tarihi" and len(t["header"]) == 10
    assert t["rows"] == [
        ["27/10/2026", "0", "750.000", "-750.000", "6.193.592,20", "5.443.592,20"]
        + ["% 1,15"] * 2
        + ["% 1,01"] * 2
    ]
    assert doc.spans[t["span_ordinals"][0][2] - 1].field_path == "table:0:0:2"


def test_buyback_report_template() -> None:
    doc = body(1688504)
    assert doc.template_id == "tbl_GAI_Report_ID"
    assert field(doc, "yapilan aciklama guncelleme mi")["value"] == "Evet"
    assert field(doc, "geri alima konu pay miktari")["value"] == "42.000.000"
    (t,) = doc.structure["tables"]
    assert t["caption"] == "Geri Alım İşlemlerinin Detayları"
    assert t["rows"][-1][:3] == ["SAHOL, TRASAHOL91Q5", "27.10.2026", "286.000"]


def test_dividend_template() -> None:
    doc = body(1688363)
    assert doc.template_id == "tbl_KP_Report_ID"
    assert field(doc, "nakit kar payi odeme sekli")["value"] == "Peşin"
    cash = next(
        t for t in doc.structure["tables"] if t["caption"] == "Nakit Kar Payı Ödeme Tutar ve Oranları"
    )
    assert cash["rows"][0][:3] == ["A Grubu, FROTO, TRAOTOSN91H6", "Peşin", "3,0700000"]
    dates = next(t for t in doc.structure["tables"] if t["caption"] == "Kar Payı Ödeme Tarihleri")
    assert dates["rows"][0] == ["Peşin", "26.11.2026", "", "30.11.2026", "27.11.2026"]


def test_capital_increase_template() -> None:
    doc = body(1687750)
    assert doc.template_id == "tbl_SA_Report_ID"
    assert field(doc, "ozet bilgi")["value"] == "Bedelli Sermaye Artırımı Hakkında."
    assert doc.structure["tables"]


def test_general_assembly_template_two_column_rows() -> None:
    doc = body(1688368)
    assert doc.template_id == "tbl_GK_Report_ID"
    assert field(doc, "genel kurul tipi")["value"] == "Olağanüstü Genel Kurul"
    assert field(doc, "genel kurul tarihi")["value"] == "24.11.2026"
    assert any(p["text"].startswith("1 - Açılış") for p in doc.structure["paragraphs"])


def test_new_business_relation() -> None:
    doc = body(1688011)
    assert doc.template_id == "tbl_oda-12000_New-Business-Relation"
    assert "Müşteri" in field(doc, "yeni is iliskisine baslanan")["value"]
    assert "1.325.000" in field(doc, "aciklamalar")["value"]


def test_financial_report_tables() -> None:
    doc = body(1688549)
    assert doc.template_id == "tbl_general_role_210015"
    assert doc.structure["template_ids"][:2] == ["tbl_general_role_210015", "tbl_general_role_310000"]
    bs = doc.structure["tables"][0]
    assert bs["header"] == ["Kalem", "Dipnot Referansı", "Cari Dönem 30.09.2026", "Önceki Dönem 31.12.2025"]
    cash = bs["rows"].index(["Nakit ve Nakit Benzerleri", "4", "3.648.035.372", "6.515.999.943"])
    assert bs["row_field_ids"][cash] == "ifrs-full_CashAndCashEquivalents"
    pl = next(t for t in doc.structure["tables"] if t["caption"] == "Kar veya Zarar Tablosu")
    rev = next(r for r in pl["rows"] if r[0] == "Hasılat")
    assert rev[2] == "28.452.112.861"


def test_embedded_table_in_text_block() -> None:
    doc = body(1688591)
    t = doc.structure["tables"][0]
    assert t["header"] == ["Ad-Soyad/Ünvan", "MKK Sicil No"]
    assert t["rows"][0] == ["Abdülkadir ÖZKAN", "22952591"]


def test_generic_fallback_for_unknown_markup() -> None:
    html = (
        "<div><p>Serbest metin</p><table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table></div>"
    )
    doc = parse_body([html])
    assert_c1(doc)
    assert doc.template_id is None and doc.structure["families"] == ["generic"]
    assert doc.structure["tables"][0]["header"] == ["A", "B"]
    assert doc.structure["paragraphs"][0]["text"] == "Serbest metin"


# ------------------------------------------------------------------------------------------ files


def test_pdf_attachment_page_spans() -> None:
    pdf = unwrap_java_bytes((FIXTURES / "file_4028328ca16c7de401a17de5e8772c7f.bin").read_bytes())
    doc = parse_file(pdf, file_name="MHRGY_56659.pdf", ext="pdf")
    assert_c1(doc)
    assert doc.mime == "application/pdf" and doc.language == "tr" and doc.extra["pages"] == 1
    first = doc.spans[0]
    assert first.page == 1 and first.field_path == "pdf:1:1" and len(first.bbox) == 4
    assert first.text == "SÜREKLİ BİLGİLERE İLİŞKİN ÖZEL DURUM AÇIKLAMASI"
    assert any("CORPUS SİGORTA" in s.text for s in doc.spans)
    t = doc.structure["tables"][0]
    assert t["rows"][0][:2] == ["27/10/2026", "7.000.000"]
    assert t["span_ordinals"][0][0] is not None


def test_english_pdf_language() -> None:
    pdf = unwrap_java_bytes((FIXTURES / "file_4028328da16c7fe501a17db833540bc8.bin").read_bytes())
    doc = parse_file(pdf, file_name="Dividend Distribution Proposal.pdf")
    assert doc.language == "en"
    assert doc.spans[0].text == "DIVIDEND DISTRIBUTION PROPOSAL"


def test_kap_pdf_plain() -> None:
    doc = parse_file((FIXTURES / "bildirimpdf_1688616.pdf").read_bytes(), kind="kap_pdf")
    assert doc.kind == "kap_pdf" and doc.extra["pages"] == 2
    assert any("PASİFİK EURASİA" in s.text for s in doc.spans)


def test_xlsx_attachment() -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Özet"
    ws.append(["Kalem", "Tutar (TL)"])
    ws.append(["Hasılat", 1250000.5])
    ws.append(["Net Kâr", 300000])
    buf = io.BytesIO()
    wb.save(buf)
    doc = parse_file(buf.getvalue(), file_name="tablo.xlsx")
    assert_c1(doc)
    assert sniff(buf.getvalue()) == "xlsx"
    (t,) = doc.structure["tables"]
    assert t["caption"] == "Özet" and t["header"] == ["Kalem", "Tutar (TL)"]
    assert t["rows"] == [["Hasılat", "1250000.5"], ["Net Kâr", "300000"]]


def test_docx_attachment() -> None:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Yönetim Kurulu Kararı</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Ad</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Görev</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Ayşe</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Başkan</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    doc = parse_file(buf.getvalue(), file_name="karar.docx")
    assert_c1(doc)
    assert doc.structure["paragraphs"][0]["text"] == "Yönetim Kurulu Kararı"
    assert doc.structure["tables"][0]["rows"] == [["Ayşe", "Başkan"]]


def test_turkish_encodings() -> None:
    text = "Şirketimiz İstanbul'da kâr payı dağıtımı yapacaktır. Ğ ı"
    assert decode_text(text.encode("cp1254")) == text
    assert decode_text(text.encode("iso-8859-9")) == text
    assert decode_text(text.encode("utf-8")) == text
    assert fix_turkish("ÞÝRKETÝMÝZ ÝSTANBUL'DA KAR DAÐITACAKTIR") == "ŞİRKETİMİZ İSTANBUL'DA KAR DAĞITACAKTIR"
    assert fix_turkish("Şirket Ýstanbul") == "Şirket Ýstanbul"  # real Turkish letters present: untouched
    doc = parse_file(text.encode("cp1254"), file_name="duyuru.txt", ext="txt")
    assert doc.text == text and doc.language == "tr"
    assert guess_language("The Company announces that the board of directors resolved") == "en"


def test_unsupported_formats_are_recorded_not_dropped() -> None:
    doc = parse_file(b"\xd0\xcf\x11\xe0" + b"\x00" * 64, file_name="eski.doc", ext="doc")
    assert doc.extra["unsupported"] is True and doc.text == ""
    broken = parse_file(b"%PDF-1.4 not really a pdf", file_name="bozuk.pdf")
    assert broken.extra.get("parse_error") or broken.extra.get("ocr_needed")
