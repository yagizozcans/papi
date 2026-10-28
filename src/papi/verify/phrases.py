"""Curated investment-advice phrases (TR/EN) that must not appear in analyst output (PRD 10, 04 §4.1).

Matching runs on Turkish-lowercased, diacritic-folded text (``satın`` → ``satin``) with word
boundaries, so factual wording stays allowed: "pay satışı", "satın alma", "satın alınan", "hisse
geri alımı", "share buyback", "agreed to buy a stake", "yatırım tavsiyesi değildir". Advisory
wording is flagged: imperatives ("alın", "satın", "satın alın", "tutun"), recommendations
("al tavsiyesi", "strong buy", "you should sell"), price targets and return promises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from papi.core.text import fold_diacritics, tr_lower


@dataclass(frozen=True)
class Phrase:
    id: str
    pattern: re.Pattern[str]
    label: str
    folded: bool = True  # match on diacritic-folded text (else Turkish lowercase only)


def _p(pid: str, regex: str, label: str, *, folded: bool = True) -> Phrase:
    return Phrase(pid, re.compile(regex), label, folded)


# Patterns are written against Turkish-lowercased text; ``folded`` ones additionally against the
# diacritic-folded form (ç→c, ğ→g, ı→i, ö→o, ş→s, ü→u). Imperatives match unfolded so that e.g.
# "tütün" (tobacco) is not read as "tutun" (hold).
PHRASES: tuple[Phrase, ...] = (
    # --- Turkish: imperatives addressed to the reader
    _p("tr.imperative.alin", r"\balın(?:ız)?\b", "alın", folded=False),
    _p("tr.imperative.satin", r"\bsatın(?:ız)?\b(?!\s+al)", "satın (emir)", folded=False),
    _p("tr.imperative.satin_alin", r"\bsatın\s+al(?:ın|ınız)\b", "satın alın", folded=False),
    _p("tr.imperative.tutun", r"\b(?:tutun|tutunuz)\b", "tutun", folded=False),
    _p("tr.imperative.ekleyin", r"\b(?:ekleyin|toplayin|biriktirin)\b", "ekleyin/toplayın"),
    _p("tr.imperative.kar_al", r"\b(?:kar|kari)\s+(?:alin|realize\s+edin)\b", "kâr alın"),
    _p("tr.imperative.zarar_kes", r"\b(?:zarar\s+kesin|stop\s+olun|stop\s+loss\s+koyun)\b", "zarar kesin"),
    _p(
        "tr.imperative.pozisyon",
        r"\bpozisyon(?:unuzu|u|lari)?\s+(?:acin|kapatin|artirin|azaltin|buyutun|kuculun)\b",
        "pozisyon açın/kapatın",
    ),
    _p("tr.modal.second_person", r"\b(?:al|sat|tut)malisin(?:iz)?\b", "almalısınız/satmalısınız"),
    _p("tr.modal.satin_almali", r"\bsatin\s+almalisin(?:iz)?\b", "satın almalısınız"),
    # --- Turkish: recommendations, targets, promises
    _p(
        "tr.reco.tavsiye",
        r"\b(?:al|sat|tut|endeks\s+ustu|endeks\s+alti)\s+tavsiye(?:si|miz)?\b",
        "al/sat/tut tavsiyesi",
    ),
    _p("tr.reco.sinyal", r"\b(?:al|sat)\s+sinyal(?:i)?\b", "al/sat sinyali"),
    _p("tr.reco.firsat", r"\balim\s+firsati\b", "alım fırsatı"),
    _p(
        "tr.reco.tavsiye_ederiz",
        r"\b(?:almanizi|satmanizi|tutmanizi)\s+(?:tavsiye|oneri)",
        "almanızı tavsiye",
    ),
    _p("tr.target.hedef_fiyat", r"\bhedef\s+fiyat", "hedef fiyat"),
    _p("tr.target.fiyat_hedefi", r"\bfiyat\s+hedef", "fiyat hedefi"),
    _p(
        "tr.promise.kesin",
        r"\bkesin(?:likle)?\s+(?:yukselir|yukselecek|yukselecektir|artar|artacak|kazandirir"
        r"|kazandiracak|duser|dusecek)\b",
        "kesin yükselir/düşer",
    ),
    _p("tr.promise.garanti", r"\bgaranti(?:li)?\s+(?:getiri|kazanc|kar)\b", "garanti getiri"),
    _p("tr.promise.kacirmayin", r"\bkacirmayin\b", "kaçırmayın"),
    # --- English
    _p("en.reco.strong", r"\bstrong\s+(?:buy|sell)\b", "strong buy/sell"),
    _p(
        "en.reco.rating",
        r"\b(?:buy|sell|hold|accumulate|overweight|underweight)\s+(?:rating|recommendation|signal|call)\b",
        "buy/sell rating",
    ),
    _p(
        "en.reco.rated",
        r"\b(?:rate|rated|rating|upgrade[sd]?\s+to|downgrade[sd]?\s+to)\s+(?:a\s+)?(?:buy|sell|hold)\b",
        "rated buy/sell",
    ),
    _p(
        "en.reco.modal",
        r"\b(?:you|investors|we)\s+(?:should|must|could|might\s+want\s+to)\s+(?:consider\s+)?"
        r"(?:buy|sell|hold|buying|selling|accumulate|exit)\b",
        "you should buy/sell",
    ),
    _p(
        "en.reco.recommend",
        r"\b(?:recommend|suggest|advise)(?:s|ed)?\s+(?:to\s+)?(?:buy|sell|hold|buying|selling|holding)\b",
        "recommend buying/selling",
    ),
    _p("en.reco.time_to", r"\btime\s+to\s+(?:buy|sell)\b", "time to buy/sell"),
    _p("en.reco.imperative", r"(?:^|[.!?]\s+)(?:buy|sell)\s+(?:the|this|these|now)\b", "Buy/Sell (emir)"),
    _p("en.reco.opportunity", r"\bbuying\s+opportunity\b", "buying opportunity"),
    _p("en.target.target_price", r"\btarget\s+price|\bprice\s+target", "target price"),
    _p(
        "en.promise.guaranteed",
        r"\bguaranteed\s+(?:return|gain|profit)s?\b|\bwill\s+(?:certainly|definitely|surely)\s+"
        r"(?:rise|go\s+up|fall)\b",
        "guaranteed return",
    ),
)


@dataclass(frozen=True)
class AdviceHit:
    phrase_id: str
    label: str
    match: str


def find_advice(text: str | None) -> list[AdviceHit]:
    """Advice phrases found in ``text`` (empty list when clean)."""
    if not text:
        return []
    lower = tr_lower(text)
    folded = fold_diacritics(lower)
    hits: list[AdviceHit] = []
    for ph in PHRASES:
        m = ph.pattern.search(folded if ph.folded else lower)
        if m:
            hits.append(AdviceHit(ph.id, ph.label, m.group(0).strip()))
    return hits
