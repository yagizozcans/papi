from __future__ import annotations

import csv
from collections import Counter

import pytest

from papi.core.config import get_settings
from papi.triage import subject_map, taxonomy
from papi.triage.subject_map import MatchInput, SubjectMap, SubjectMapError

from .helpers import form_field, load_harvest


@pytest.fixture
def sm() -> SubjectMap:
    return subject_map.load()


def codes(sm: SubjectMap, subject: str, summary: str | None = None, **kw) -> tuple[str, ...]:
    return sm.match(MatchInput(subject=subject, summary=summary, **kw)).codes


def test_unconditional_subjects(sm):
    assert codes(sm, "Finansal Rapor") == ("report.financial.periodic",)
    assert codes(sm, "Yeni İş İlişkisi") == ("business.contract.award",)
    assert codes(sm, "Pay Alım Satım Bildirimi") == ("ownership.insider_trade",)
    assert codes(sm, "Sermaye Piyasası Kurulu Tedbir Kararı") == ("market.measure",)
    # Turkish normalisation: case / diacritics / trailing space
    assert codes(sm, "KATILIM FİNANSI İLKELERİ BİLGİ FORMU ") == ("report.company_info",)
    assert codes(sm, "Kurumsal Yönetim Bilgi Formu (Güncelleme) - Yönetim Kurulu-2") == (
        "report.corporate_governance",
    )


def test_oda_general_keywords(sm):
    oda = "Özel Durum Açıklaması (Genel)"
    assert codes(sm, oda, "Grev Kararı Alınması Hk.") == ("business.labour",)
    assert codes(sm, oda, "Rekabet Kurulu tarafından açılan soruşturma hk") == ("legal.investigation",)
    assert codes(sm, oda, "Kâr Payı Avansı 2026 1. Çeyrek") == ("distribution.dividend.advance",)
    assert codes(
        sm, oda, "BİAŞ'da oluşan ağırlıklı ortalama fiyatın pay başına net aktif değerin 2 katını aşması"
    ) == ("market.nav_premium",)
    assert codes(sm, oda, "Bağımsız Yönetim Kurulu Üye Adaylarının SPK Uygunluk Onayı") == (
        "governance.board.change",
    )
    assert codes(sm, oda, "Kredi Sözleşmesi İmzalanması") == ("finance.loan",)
    assert codes(sm, oda, "Bayilik Sözleşmesi İmzalanması hk") == ("business.contract.award",)
    assert codes(sm, oda, "İhtiyati tedbir kararı hakkında") == ("legal.lawsuit",)
    assert codes(sm, oda, "Özel Durum Açıklaması") == ("other.unclassified",)


def test_body_text_fallback(sm):
    oda = "Özel Durum Açıklaması (Genel)"
    r = sm.match(
        MatchInput(
            subject=oda,
            summary="Özel Durum Açıklaması",
            body_text="Şirketimiz ile X A.Ş. arasında sözleşme imzalanmıştır.",
        )
    )
    assert r.codes == ("business.contract.award",)
    assert r.matched_on == "body"


def test_class_gated_rules_and_fallback(sm):
    assert codes(sm, "Borsa İstanbul A.Ş. Duyurusu", "XYZ paylarında brüt takas uygulaması") == (
        "market.measure",
    )
    assert codes(sm, "Borsa İstanbul A.Ş. Duyurusu", "Seans saatleri") == ("market.notice",)
    r = sm.match(MatchInput(subject="Konkordato Talebine İlişkin Bildirim (yeni konu)", summary=None))
    assert r.mapped_by == "fallback"
    assert r.codes == ("legal.concordat",)
    assert codes(sm, "Hiç Görülmemiş Konu", "x") == ("other.unclassified",)


def test_multi_event_rules(sm):
    kp = "Kar Payı Dağıtım İşlemlerine İlişkin Bildirim"
    assert codes(sm, kp, "Kar payı dağıtımı ve pay geri alım programı") == (
        "buyback.program.start",
        "distribution.dividend.cash",
    )
    fields = [form_field("Pay Biçiminde Ödeme", "Ödenecek")]
    assert codes(sm, kp, "Kar Dağıtım Önerisi", form_fields=fields) == (
        "distribution.dividend.stock",
        "distribution.dividend.cash",
    )
    fields = [form_field("Pay Biçiminde Ödeme", "Ödenmeyecek")]
    assert codes(sm, kp, "Kar Dağıtım Önerisi", form_fields=fields) == ("distribution.dividend.cash",)
    sa = "Sermaye Artırımı - Azaltımı İşlemlerine İlişkin Bildirim"
    assert codes(sm, sa, "Bedelli ve bedelsiz sermaye artırımı") == (
        "capital.increase.bonus",
        "capital.increase.rights",
    )
    # table-based signal: a non-zero rights table
    tables = [
        {
            "caption": "Bedelli Sermaye Artırımı (Rüçhan hakkı kullandırılarak)",
            "header": ["Pay Grup Bilgileri", "Rüçhan Hakkı Kullanım Tutarı (TL)"],
            "rows": [["ABC, TRAABC", "1.000.000"]],
        }
    ]
    assert codes(sm, sa, "Sermaye Artırımı Tescili", tables=tables) == ("capital.increase.rights",)
    assert codes(sm, sa, "Sermaye artırımı tescili") == ("capital.change",)


def test_buyback_and_gm_variants(sm):
    gai = "Payların Geri Alınmasına İlişkin Bildirim"
    assert codes(sm, gai, "27.10.2026 Tarihli Pay Geri Alım İşlemleri") == ("buyback.execution",)
    assert codes(sm, gai, "Pay Geri Alım Programının Revize Edilmesi") == ("buyback.program.start",)
    assert codes(sm, gai, "Geri alım programının sona ermesi") == ("buyback.program.end",)
    gk = "Genel Kurul İşlemlerine İlişkin Bildirim"
    assert codes(sm, gk, "2025 Yılı Olağan Genel Kurul Toplantı Daveti") == ("governance.gm.notice",)
    assert codes(sm, gk, "Olağan Genel Kurul Kararlarının Tescili") == ("governance.gm.result",)
    assert codes(sm, gk, "Kar payı dağıtımına ilişkin genel kurul kararı") == (
        "distribution.dividend.cash",
        "governance.gm.result",
    )


def test_debt_rules(sm):
    faiz = "Pay Dışında Sermaye Piyasası Aracı İşlemlerine İlişkin Bildirim (Faiz İçeren)"
    assert codes(sm, faiz, "Kupon Ödemesi") == ("finance.debt.payment",)
    assert codes(sm, faiz, "Nitelikli Yatırımcıya Borçlanma Aracı İhracı") == ("finance.debt.issuance",)


def test_harvest_coverage_at_least_90_percent(sm):
    items = list(load_harvest())
    assert len(items) > 5000
    mapped = 0
    for it in items:
        r = sm.match(
            MatchInput(
                subject=it["subject"],
                source_class=it["disclosureClass"],
                source_type=it["disclosureType"],
                summary=it["summary"],
            )
        )
        mapped += any(not c.startswith("other.") for c in r.codes)
    share = mapped / len(items)
    assert share >= 0.90, share


def test_frequency_csv_subjects_all_known(sm):
    path = get_settings().pack_dir / "kap_subject_frequencies.csv"
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    total = sum(int(r["count"]) for r in rows)
    volume = Counter()
    for r in rows:
        volume[sm.is_mapped(r["subject"])] += int(r["count"])
    assert volume[True] / total >= 0.99  # every subject covering ≥ 99 % of volume has a map entry


def test_top20_codes_reachable_from_the_map(sm):
    reachable = {c for e in sm.subjects.values() for c in e.default}
    reachable |= {c for e in sm.subjects.values() for r in e.rules for c in r.codes}
    reachable |= {c for rs in sm.rule_sets.values() for r in rs for c in r.codes}
    top20 = [
        "business.contract.award", "capital.increase.bonus", "capital.increase.rights",
        "distribution.dividend.cash", "buyback.program.start", "buyback.execution", "ownership.insider_trade",
        "ownership.threshold_crossing", "mna.acquisition", "mna.disposal", "report.financial.periodic",
        "report.financial.guidance", "governance.gm.notice", "governance.gm.result",
        "governance.board.change",
        "legal.lawsuit", "legal.sanction", "market.measure", "finance.rating.change", "finance.debt.issuance",
        "business.tender_result", "business.capex", "ownership.tender_offer",
    ]  # fmt: skip
    assert [c for c in top20 if c not in reachable] == []


def test_invalid_map_rejected():
    tax = taxonomy.load()
    with pytest.raises(SubjectMapError, match="unknown event code"):
        SubjectMap({"subjects": {"X": ["no.such.code"]}}, tax)
    with pytest.raises(SubjectMapError, match="unknown rule_set"):
        SubjectMap({"subjects": {"X": {"rules": [{"use": "missing"}]}}}, tax)
    with pytest.raises(SubjectMapError, match="duplicate subject"):
        SubjectMap({"subjects": {"Yeni İş": ["business.contract.award"], "YENİ İŞ": ["business.capex"]}}, tax)
    with pytest.raises(SubjectMapError, match="unknown keys"):
        SubjectMap({"subjects": {"X": {"rules": [{"codes": ["business.capex"], "anyy": ["a"]}]}}}, tax)
