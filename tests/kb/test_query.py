"""Turkish query processing: stopwords, stemming with consonant softening, synonyms, AND→OR fallback."""

from __future__ import annotations

from datetime import date

from papi.kb import api
from papi.kb import query as kbq


def test_stemming_handles_suffixes_and_consonant_softening():
    norm = __import__("papi.core.text", fromlist=["normalize_tr"]).normalize_tr
    assert kbq.stem(norm("işlemlerine")) == "islem"
    # üyeliği → "uyeli…" also prefix-matches the hard form üyelik
    assert norm("üyelik").startswith(kbq.stem(norm("üyeliği")))
    assert norm("kitap").startswith(kbq.stem(norm("kitabı")))
    assert kbq.stem("pay") == "pay"


def test_query_terms_drop_stopwords_and_add_synonyms():
    terms = kbq.query_terms("Kar Payı Dağıtım İşlemlerine İlişkin Bildirim")
    tokens = [t.token for t in terms]
    assert "iliskin" not in tokens and "bildirim" in tokens
    bildirim = next(t for t in terms if t.token == "bildirim")
    assert bildirim.alternatives == ("bildirim", "aciklam")  # stems
    assert kbq.and_match(terms).count(" AND ") == len(terms) - 1
    assert " OR " in kbq.or_match(terms)
    assert kbq.query_terms("ve ile ilişkin") == []


def test_kap_subject_falls_back_to_ranked_or(kb, db):
    got = [h.version.citation_id for h in api.search(db, "Pay Alım Satım Bildirimi", k=3)]
    assert any(c.startswith("tr:spk:II-15.1:m11") for c in got)
    ev = [
        h.version.citation_id
        for h in api.search_for_event(db, "ownership.insider_trade", "Pay Alım Satım Bildirimi", k=3)
    ]
    assert ev[0].startswith("tr:spk:II-15.1:m11")
    # a code without hints behaves like search(subject)
    assert [
        h.version.citation_id
        for h in api.search_for_event(db, "no.such.code", "Pay Alım Satım Bildirimi", k=3)
    ] == got


def test_relaxed_pass_never_returns_todays_text_for_another_dates_wording(kb, db):
    q = "tahsisli yurt dışında halka açık olmayan ortaklıklar uygulanmaz"
    old = [h.version.citation_id for h in api.search(db, q, date(2015, 6, 1))]
    now = [h.version.citation_id for h in api.search(db, q, date(2026, 10, 28))]
    assert old[0] == "tr:spk:II-15.1:m2:f4"
    assert "tr:spk:II-15.1:m2:f4" not in now and "tr:spk:II-15.1:m2" not in now
