"""Thesis format: deterministic parsing, markers, trigger/kill lines, errors vs gaps."""

from __future__ import annotations

from datetime import date

import pytest

from papi.thesis import document as doc

HEAD = "---\nticker: thyao\ndate: 2026-11-06\ndirection: long\nhorizon: 6-12 ay\n---\n"


def test_user_headings_map_to_sections_and_markers_follow_template_order() -> None:
    t = doc.parse(
        HEAD
        + "# THYAO\n\n## Fikir:\nBirinci cümle. İkinci cümle!\n\n"
        + "## Dipnot\nYerli yolcu oranına bakılmalı.\n\n"
        + "## Karşı Taraf Neden Satıyor?\nDış yatırımcı çıkıyor.\n\n"
        + "## KONSENSUS\n<!-- rehber -->\nPiyasa kârın düşeceğini varsayıyor.\n",
        "x",
    )
    assert t.ticker == "THYAO"
    texts = {k: [(i.marker, i.text) for i in v] for k, v in t.sections.items() if v}
    # numbered in template order (Fikir, Konsensüs, …, Karşı Taraf, …, Dipnot), not document order
    assert texts == {
        "fikir": [(1, "Birinci cümle."), (2, "İkinci cümle!")],
        "konsensus": [(3, "Piyasa kârın düşeceğini varsayıyor.")],
        "karsi_taraf": [(4, "Dış yatırımcı çıkıyor.")],
        "dipnot": [(5, "Yerli yolcu oranına bakılmalı.")],
    }
    assert "'Farklı Görüş' bölümü boş veya yok" in t.gaps
    assert not any("Dipnot" in g for g in t.gaps)  # optional section


def test_trigger_and_kill_signal_lines() -> None:
    t = doc.parse(
        HEAD
        + "## Tetikleyiciler\n- 3. çeyrek bilançosu | 2026-11 | %40\n- TCMB indirimi | Aralık | 25%\n"
        + "- İran anlaşması | 2027\n- serbest metin olay\n\n"
        + "## Tezi Çürütecek Sinyaller\n- Doluluk düşer | %80 altı\n- Brent yükselir\n",
        "x",
    )
    assert [(tr.event, tr.window, tr.probability) for tr in t.triggers] == [
        ("3. çeyrek bilançosu", "2026-11", 0.40),
        ("TCMB indirimi", "Aralık", 0.25),
        ("İran anlaşması", "2027", None),
        ("serbest metin olay", None, None),
    ]
    assert [(k.signal, k.threshold) for k in t.kill_signals] == [
        ("Doluluk düşer", "%80 altı"),
        ("Brent yükselir", None),
    ]
    m = {tr.event: tr.marker for tr in t.triggers}
    assert f"T{m['İran anlaşması']}: tetikleyiciye kendi olasılık tahminin yazılmamış" in t.gaps
    assert f"T{m['serbest metin olay']}: tetikleyicinin zaman aralığı yok" in t.gaps
    assert any("sinyalin ölçütü / eşiği yok" in g for g in t.gaps)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("## Fikir\nx\n", "ön bilgi"),
        (HEAD.replace("thyao", "türk hava") + "## Fikir\nx\n", "ticker"),
        (HEAD.replace("long", "al") + "## Fikir\nx\n", "direction"),
        (HEAD + "## Fiyat Hedefi\nx\n", "bilinmeyen başlık"),
        (HEAD + "## Fikir\nx\n## fikir\ny\n", "iki kez"),
        (HEAD + "başlıksız metin\n## Fikir\nx\n", "başlık altında olmayan"),
        (HEAD + "## Tetikleyiciler\n- a | b | çok\n", None),  # a gap, not an error
    ],
)
def test_errors_stop_parsing_gaps_do_not(text: str, needle: str | None) -> None:
    if needle is None:
        assert any("okunamadı" in g for g in doc.parse(text, "x").gaps)
        return
    with pytest.raises(doc.ThesisError) as exc:
        doc.parse(text, "x")
    assert any(needle in e for e in exc.value.errors)


def test_new_template_is_readable_and_reports_every_required_section() -> None:
    t = doc.parse(doc.template("tuprs", date(2026, 1, 2)), "TUPRS-2026-01-02")
    assert t.ticker == "TUPRS" and t.direction == "long"
    assert not any(t.sections.values())
    assert len(t.gaps) == sum(1 for s in doc.SECTIONS if s.required)


def test_resolve_by_name_and_new_path_never_overwrites() -> None:
    first = doc.new_path("THYAO", date(2026, 11, 6))
    first.write_text(doc.template("THYAO", date(2026, 11, 6)), encoding="utf-8")
    assert doc.new_path("THYAO", date(2026, 11, 6)).name == "THYAO-2026-11-06-2.md"
    assert doc.resolve("THYAO-2026-11-06") == first
    with pytest.raises(FileNotFoundError, match="THYAO-2026-11-06"):
        doc.resolve("YOK")
