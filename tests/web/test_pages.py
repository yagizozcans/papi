"""HTML pages on a seeded DB (other slices monkeypatched)."""

from __future__ import annotations

import re
import sys
import types
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select

from papi.core.ids import uuid7
from papi.core.schema import feedback, forecast
from papi.core.timeutil import now_utc
from papi.web import integrations, queries


def _headlines(html: str) -> list[str]:
    return re.findall(r'class="headline" href="/events/[^"]+">([^<]+)<', html)


# ------------------------------------------------------------------------------------------ feed


def test_feed_default_scope_is_holdings_watchlist_p3(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    heads = _headlines(r.text)
    assert "THYAO yeni uçak kiralama sözleşmesi" in heads  # watchlist + P3
    assert "ASELS temettü düzeltmesi" in heads  # holding
    assert "GARAN faaliyet raporu" not in heads  # P4, not mine
    assert 'badge hold">HOLDING' in r.text and 'badge watch">WATCH' in r.text
    assert "https://www.kap.org.tr/tr/Bildirim/1500002" in r.text
    assert "Geç görüldü" in r.text  # THYAO catch-up marker
    assert "Düzeltme" in r.text  # revision marker
    assert 'data-stream="/stream?scope=mine"' in r.text


def test_feed_all_kap_toggle(client: TestClient) -> None:
    heads = _headlines(client.get("/?scope=all").text)
    assert "GARAN faaliyet raporu" in heads
    assert len(heads) == 4


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("scope=all&ticker=thyao", {"THYAO yeni uçak kiralama sözleşmesi"}),
        ("scope=all&family=distribution", {"Nakit temettü", "ASELS temettü düzeltmesi"}),
        ("scope=all&materiality_gte=72", {"Nakit temettü"}),
        ("scope=all&priority=P1", {"Nakit temettü", "ASELS temettü düzeltmesi"}),
        (
            "scope=all&priority=P3",
            {"Nakit temettü", "ASELS temettü düzeltmesi", "THYAO yeni uçak kiralama sözleşmesi"},
        ),
        (
            "scope=all&hide_routine=1",
            {"Nakit temettü", "ASELS temettü düzeltmesi", "THYAO yeni uçak kiralama sözleşmesi"},
        ),
        ("scope=holdings", {"Nakit temettü", "ASELS temettü düzeltmesi"}),
        ("scope=watchlist", {"THYAO yeni uçak kiralama sözleşmesi"}),
        ("scope=all&code=reporting.annual_report", {"GARAN faaliyet raporu"}),
    ],
)
def test_feed_filters(client: TestClient, query: str, expected: set[str]) -> None:
    r = client.get(f"/?{query}")
    assert r.status_code == 200
    assert set(_headlines(r.text)) == expected


def test_feed_empty_offers_all_kap(client: TestClient) -> None:
    r = client.get("/?ticker=NOPE")
    assert "Bu filtrelerle olay yok" in r.text and 'href="/?scope=all"' in r.text


def test_group_bursts() -> None:
    base = now_utc()
    items = [
        {
            "code": "reporting.financials",
            "code_label": "Finansal rapor",
            "announced_at": base - timedelta(seconds=i),
        }
        for i in range(6)
    ] + [{"code": "business.x", "code_label": "x", "announced_at": base - timedelta(hours=1)}]
    groups = queries.group_bursts(items)
    assert groups[0]["burst"] and len(groups[0]["items"]) == 6
    assert not groups[1]["burst"]


def test_feed_renders_bursts(client: TestClient, world: dict[str, Any], db: Connection) -> None:
    from tests.web.sample import _disclosure, _event

    t = now_utc() - timedelta(minutes=5)
    for i in range(5):
        d = _disclosure(
            db,
            kap=world["kap"],
            ref=f"16000{i}",
            iss=world["asels_issuer"],
            published=t,
            seen=t,
            title="Finansal rapor",
        )
        _event(
            db,
            disc=d,
            iss=world["asels_issuer"],
            ins=world["asels"],
            ticker="ASELS",
            code="reporting.financials",
            at=t - timedelta(seconds=i),
            materiality=20,
            priority="P1",
            headline=f"Rapor {i}",
        )
    db.commit()
    r = client.get("/?scope=holdings")
    assert '<details class="burst">' in r.text and "5 × reporting.financials" in r.text


# ------------------------------------------------------------------------------------------ event detail


def test_event_detail_content(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get(f"/events/{world['e_div']}")
    assert r.status_code == 200
    html = r.text
    assert "ASELS" in html and "distribution.dividend.cash" in html
    assert "https://www.kap.org.tr/tr/Bildirim/1500001" in html
    assert "Gecikme: 40 sn" in html
    # revision chain shows the correction
    assert "Revizyon geçmişi" in html and "KAP 1500004" in html
    # summary with [S n] links, key facts linked to spans, spans as anchors in the viewer
    assert 'data-anchor="d0s1">[S1]</a>' in html
    assert 'class="fact-val" href="#d0s1"' in html  # payload_spans provenance
    assert 'class="fact-val" href="#d0s2"' in html  # form_fields value match
    assert '<span class="span" id="d0s1"' in html and '<span class="span" id="d0s2"' in html
    assert 'id="d0s3"' in html  # span without offsets listed separately
    assert f"/raw/{world['sha']}" in html
    # For me: holding with position, explicit reason why no impact yet
    assert "Benim için" in html and "6.900,00" in html and "henüz senaryo" in html
    # analysis claims with fact styling + unsupported marker
    assert "tag fact" in html and "doğrulanamadı" in html
    for label in ("Yanlış bilgi", "Yanlış tür", "Önemsiz", "Faydalı"):
        assert label in html


def test_event_detail_for_me_reason_when_not_held(client: TestClient, world: dict[str, Any]) -> None:
    html = client.get(f"/events/{world['e_thy']}").text
    assert "portföyünüzdeki bir hisseyi ilgilendirmiyor" in html
    assert "Belge metni henüz alınmadı" in html


def test_event_detail_for_me_impact(client: TestClient, world: dict[str, Any], db: Connection) -> None:
    db.execute(
        forecast.insert().values(
            id=uuid7(),
            event_id=world["e_div"],
            instrument_id=world["asels"],
            model="baseline",
            model_version="1",
            created_at=now_utc(),
            horizon="T+5",
            target="abnormal_return",
            distribution={"q05": -0.02, "q50": 0.01, "q95": 0.04},
            position_impact={"cash_dividend_try": 250, "value_change_try_q50": 69},
            frozen_hash="x" * 64,
        )
    )
    db.commit()
    html = client.get(f"/events/{world['e_div']}").text
    assert "cash_dividend_try" in html and "T+5" in html
    assert "henüz senaryo" not in html


def test_event_detail_without_portfolio_slice(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrations, "positions", lambda conn: None)
    html = client.get(f"/events/{world['e_div']}").text
    assert "Portföy modülü yüklü değil" in html


def test_event_not_found(client: TestClient) -> None:
    r = client.get("/events/nope")
    assert r.status_code == 404 and "Olay bulunamadı" in r.text


def test_feed_item_fragment(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get(f"/fragments/events/{world['e_thy']}")
    assert r.status_code == 200 and f'id="ev-{world["e_thy"]}"' in r.text
    assert client.get("/fragments/events/nope").status_code == 404


def test_feedback_post_writes_row(client: TestClient, world: dict[str, Any], db: Connection) -> None:
    r = client.post(
        f"/events/{world['e_div']}/feedback",
        data={"kind": "wrong_fact", "comment": "tutar yanlış"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Kaydedildi" in r.text
    r2 = client.post(f"/events/{world['e_div']}/feedback", data={"kind": "helpful"}, follow_redirects=False)
    assert r2.status_code == 303
    db.rollback()
    rows = db.execute(select(feedback).where(feedback.c.target_id == world["e_div"])).all()
    assert sorted((f.kind, f.comment) for f in rows) == [("helpful", None), ("wrong_fact", "tutar yanlış")]
    assert all(f.target_type == "event" for f in rows)
    assert client.post(f"/events/{world['e_div']}/feedback", data={"kind": "bogus"}).status_code == 400
    # shows up on the ops review list and as a count on the detail page
    assert "tutar yanlış" in client.get("/ops").text
    assert "Yanlış bilgi (1)" in client.get(f"/events/{world['e_div']}").text


def test_raw_download(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get(f"/raw/{world['sha']}")
    assert r.status_code == 200 and "kâr payı" in r.text
    assert "sandbox" in r.headers["content-security-policy"]
    assert client.get("/raw/" + "0" * 64).status_code == 404


# ------------------------------------------------------------------------------------------ portfolio


def test_portfolio_page(client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        integrations,
        "pending_actions",
        lambda conn, status="expected": [
            {
                "id": "pa1",
                "ticker": "ASELS",
                "instrument_id": world["asels"],
                "kind": "bonus",
                "status": "expected",
                "proposal": {"text_tr": "%100 bedelsiz: +100 adet"},
                "ex_date": None,
                "created_at": now_utc(),
            }
        ],
    )
    r = client.get("/portfolio")
    assert r.status_code == 200
    html = r.text
    assert "6.900,00" in html and "650,00" in html and "%10,4" in html  # value, P&L TL, P&L %
    assert "%100 bedelsiz: +100 adet" in html and "/portfolio/pending/pa1/apply" in html
    assert "Portföy olayları" in html and "ASELS temettü düzeltmesi" in html
    assert "62,50" in html and "buy" in html  # recent txn


def _fake_service(monkeypatch: pytest.MonkeyPatch, calls: list[tuple]) -> None:
    mod = types.ModuleType("papi.portfolio.service")

    def apply_pending_action(conn: Connection, pid: str, *, quantity_override: Decimal | None = None) -> str:
        calls.append(("apply", pid, quantity_override))
        return "txn-123456789"

    def dismiss_pending_action(conn: Connection, pid: str) -> None:
        calls.append(("dismiss", pid))

    mod.apply_pending_action = apply_pending_action  # type: ignore[attr-defined]
    mod.dismiss_pending_action = dismiss_pending_action  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "papi.portfolio.service", mod)


def test_pending_apply_and_dismiss_call_service(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    _fake_service(monkeypatch, calls)
    hx = {"HX-Request": "true"}
    r = client.post("/portfolio/pending/pa1/apply", data={"quantity_override": "1.250"}, headers=hx)
    assert r.status_code == 200 and "Uygulandı" in r.text
    r = client.post("/portfolio/pending/pa2/apply", data={}, headers=hx)
    assert r.status_code == 200
    r = client.post("/portfolio/pending/pa3/dismiss", headers=hx)
    assert r.status_code == 200 and "Reddedildi" in r.text
    assert calls == [("apply", "pa1", Decimal("1250")), ("apply", "pa2", None), ("dismiss", "pa3")]
    assert client.post("/portfolio/pending/pa1/apply", data={"quantity_override": "abc"}).status_code == 400


def test_pending_apply_without_portfolio_slice(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "papi.portfolio.service", None)  # import raises ModuleNotFoundError
    r = client.post("/portfolio/pending/pa1/apply", headers={"HX-Request": "true"})
    assert r.status_code == 503 and "Portföy modülü yok" in r.text


# ------------------------------------------------------------------------------------------ company


def test_company_page(client: TestClient) -> None:
    r = client.get("/company/asels")
    assert r.status_code == 200
    html = r.text
    assert "Aselsan Elektronik" in html and 'badge hold">HOLDING' in html
    assert "KAP_MEMBER_OID" in html and "oid-asels" in html
    assert 'class="chart-data">{"x": [' in html and '"buy": [' in html and "62.5" in html
    assert "Kâr Payı Dağıtım İşlemlerine İlişkin Bildirim" in html
    assert "Son kapanış: <b>69,00</b>" in html
    assert client.get("/company/NOPE").status_code == 404


# ------------------------------------------------------------------------------------------ ops / usage


def test_ops_page(client: TestClient) -> None:
    r = client.get("/ops")
    assert r.status_code == 200
    html = r.text
    assert "ValueError: boom" in html and "event:triage.classify" in html
    assert "failed:parse" in html and "parse: unexpected table" in html
    assert "tbl_new_form" in html and "1500006" in html
    assert "2 açık indeks" in html and "1499990" in html
    assert "metric kap.latency_s" in html and "n=4" in html
    assert "İşaretli analizler" in html and "distribution.dividend.cash" in html
    assert "Brüt temettü 2,50 TL" in html  # unsupported claim on the review list
    assert "<th>dead</th>" in html


def test_usage_page_matches_ledger(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    html = client.get("/ops/usage").text
    assert "LLM modülü yüklü değil" in html
    assert '<td class="r">3</td><td class="r">0,1500 <small>(analiz 0,1500 · sohbet 0,0000)' in html  # 5 h
    assert '<td class="r">4</td><td class="r">1,1500</td>' in html  # week
    assert "limit_hit=1" in html and 'data-kind="usage"' in html
    monkeypatch.setattr(
        integrations,
        "governor_status",
        lambda conn: {
            "paused": True,
            "paused_until": None,
            "used_5h": 0.15,
            "used_week": 1.15,
            "cap_5h": 2.0,
            "cap_week": 20.0,
            "cap_chat_5h": 1.0,
            "by_task": {},
            "queue": {"llm_deferred": {"P1": 2}, "llm_deferred_total": 2},
        },
    )
    html = client.get("/ops/usage").text
    assert "DURAKLATILDI" in html and "0,1500 / 2,00 USD" in html and "1,1500 / 20,00 USD" in html
    assert "P1=2" in html


def test_usage_page_with_real_governor_matches_ledger(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db: Connection
) -> None:
    governor = pytest.importorskip("papi.llm.governor")
    monkeypatch.setattr(integrations, "governor_status", lambda conn: dict(governor.status(conn)))
    html = client.get("/ops/usage").text
    st = governor.status(db)
    # The governor's rolling sums are the ledger sums the page shows in its own table.
    assert abs(st["used_5h"] - 0.15) < 1e-9 and abs(st["used_week"] - 1.15) < 1e-9
    assert "0,1500 / " in html and "1,1500 / " in html


def test_usage_pause_resume(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    mod = types.ModuleType("papi.llm.governor")
    mod.pause = lambda conn, until=None, reason="manual": calls.append(f"pause:{reason}")  # type: ignore[attr-defined]
    mod.resume = lambda conn: calls.append("resume")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "papi.llm.governor", mod)
    hx = {"HX-Request": "true"}
    assert "duraklatıldı" in client.post("/ops/usage/pause", headers=hx).text
    assert "sürdürülüyor" in client.post("/ops/usage/resume", headers=hx).text
    assert calls == ["pause:manual", "resume"]
    assert client.post("/ops/usage/explode", headers=hx).status_code == 404


def test_static_vendor_assets_served(client: TestClient) -> None:
    for path in (
        "/static/vendor/htmx.min.js",
        "/static/vendor/uPlot.iife.min.js",
        "/static/app.js",
        "/static/app.css",
    ):
        assert client.get(path).status_code == 200
    html = client.get("/").text
    assert not re.search(r'(src|href)="https?://(?!www\.kap\.org\.tr)', html)  # no external assets


def test_feed_load_more_link(client: TestClient) -> None:
    html = client.get("/?scope=all&limit=2").text
    assert len(_headlines(html)) == 2
    assert 'href="/?scope=all&amp;limit=102">Daha fazla göster' in html
