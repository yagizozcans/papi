"""Shared test helpers for the Telegram and notify slices: a respx-backed fake Bot API and DB seeding."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import respx
from sqlalchemy import Connection

from papi.core import schema as s
from papi.core.config import Settings
from papi.core.ids import uuid7
from papi.core.rawstore import source_id_for
from papi.core.timeutil import now_utc

TOKEN = "123456:TEST-token"
USER_ID = 777
CHAT_ID = 777


class FakeTelegram:
    """Records every Bot API call; ``sendMessage`` returns increasing message ids."""

    def __init__(self, router: respx.MockRouter, token: str = TOKEN) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.next_message_id = 5000
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}
        self.route = router.post(url__regex=rf"^https://api\.telegram\.org/bot{token}/(\w+)$").mock(
            side_effect=self._handle
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method in self.fail:
            status, payload = self.fail.pop(method)
            return httpx.Response(status, json=payload)
        if method == "sendMessage":
            self.next_message_id += 1
            result: Any = {
                "message_id": self.next_message_id,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": body["chat_id"], "type": "private"},
                "text": body["text"],
            }
        elif method == "getUpdates":
            offset = body.get("offset")
            result = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        elif method == "getMe":
            result = {"id": 42, "is_bot": True, "username": "papi_test_bot", "first_name": "papi"}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def bodies(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]

    def texts(self) -> list[str]:
        return [b["text"] for b in self.bodies("sendMessage")]

    def last_text(self) -> str:
        return self.texts()[-1]

    def last_keyboard(self) -> list[list[dict[str, str]]]:
        for b in reversed(self.bodies("sendMessage")):
            if b.get("reply_markup"):
                return b["reply_markup"]["inline_keyboard"]
        raise AssertionError("no keyboard sent")

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def configure(settings: Settings, monkeypatch: Any, *, macos: bool = False) -> None:
    monkeypatch.setenv("PAPI_TELEGRAM_TOKEN", TOKEN)
    settings.telegram.enabled = True
    settings.telegram.allowed_user_id = USER_ID
    settings.telegram.chat_id = CHAT_ID
    settings.alerts.macos_notifications = macos
    settings.alerts.quiet_hours_enabled = False


def add_instrument(
    conn: Connection, ticker: str, name: str, *, sector: str | None = None, itype: str = "equity"
) -> str:
    now = now_utc()
    isid, iid = uuid7(), uuid7()
    conn.execute(
        s.issuer.insert().values(
            id=isid, legal_name=name, short_name=name, sector_code=sector, kind="company", status="active",
            created_at=now,
        )
    )  # fmt: skip
    conn.execute(
        s.instrument.insert().values(
            id=iid, issuer_id=isid, type=itype, currency="TRY", status="active", created_at=now
        )
    )
    conn.execute(
        s.identifier.insert().values(
            id=uuid7(), entity_type="instrument", entity_id=iid, scheme="TICKER", value=ticker,
            valid_from=s.OPEN_START, recorded_at=now,
        )
    )  # fmt: skip
    return iid


def issuer_of(conn: Connection, iid: str) -> str:
    return conn.execute(s.instrument.select().where(s.instrument.c.id == iid)).mappings().one()["issuer_id"]


def add_close(conn: Connection, iid: str, d: date, close: float) -> None:
    conn.execute(
        s.price_bar.insert().values(
            instrument_id=iid, interval="1d", ts=d, source_id=source_id_for(conn, "isyatirim_prices"),
            close=close, recorded_at=now_utc(),
        )
    )  # fmt: skip


def add_disclosure(
    conn: Connection,
    ref: str,
    *,
    subject: str = "Özel Durum Açıklaması (Genel)",
    title: str | None = "Yeni iş ilişkisi",
    summary: str | None = None,
    published_at: datetime | None = None,
    first_seen_at: datetime | None = None,
    revision_of: str | None = None,
    revision_kind: str = "original",
    is_late: bool = False,
) -> str:
    did = uuid7()
    published_at = published_at or now_utc()
    first_seen_at = first_seen_at or published_at
    conn.execute(
        s.disclosure.insert().values(
            id=did, source_id=source_id_for(conn, "kap"), source_ref=ref, revision_of=revision_of,
            revision_kind=revision_kind, published_at=published_at, first_seen_at=first_seen_at,
            source_subject=subject, title=title, summary=summary, is_late=is_late, ingest_status="normalized",
            created_at=now_utc(),
        )
    )  # fmt: skip
    return did


def add_event(
    conn: Connection,
    disclosure_id: str,
    instrument_ids: list[str],
    *,
    code: str = "business.contract.award",
    priority: str = "P2",
    materiality: int | None = 72,
    band: str | None = "high",
    headline: str = "Yeni sözleşme",
    payload: dict[str, Any] | None = None,
    announced_at: datetime | None = None,
    tickers: list[str] | None = None,
) -> str:
    eid = uuid7()
    extra: dict[str, Any] = {"headline_tr": headline, "rules": {}}
    if tickers is not None:
        extra["tickers"] = tickers
    conn.execute(
        s.event.insert().values(
            id=eid, disclosure_ids=[disclosure_id], primary_disclosure_id=disclosure_id, code=code,
            taxonomy_version="test", instrument_ids=instrument_ids, announced_at=announced_at or now_utc(),
            payload=payload, materiality=materiality, materiality_band=band, priority=priority,
            status="provisional", extra=extra, created_at=now_utc(),
        )
    )  # fmt: skip
    return eid


def buy(conn: Connection, iid: str, qty: int, price: str, *, source: str = "manual") -> str:
    from papi.portfolio.service import ProposedTxn, record_transaction

    return record_transaction(
        conn,
        ProposedTxn(
            instrument_id=iid, type="buy", quantity=Decimal(qty), price=Decimal(price),
            trade_date=date(2026, 10, 1), source=source,
        ),
    )  # fmt: skip


def message_update(
    update_id: int,
    text: str,
    *,
    user_id: int = USER_ID,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
    message_id: int | None = None,
    date_: datetime | None = None,
    reply_to: int | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": message_id or update_id,
        "from": {"id": user_id, "is_bot": False, "first_name": "Yağız", "username": "yagiz"},
        "chat": {"id": chat_id, "type": chat_type},
        "date": int((date_ or datetime.now(UTC)).timestamp()),
        "text": text,
    }
    if reply_to is not None:
        msg["reply_to_message"] = {
            "message_id": reply_to,
            "chat": {"id": chat_id, "type": chat_type},
            "date": 0,
        }
    return {"update_id": update_id, "message": msg}


def callback_update(update_id: int, data: str, message_id: int, *, user_id: int = USER_ID) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cq{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Yağız"},
            "message": {"message_id": message_id, "chat": {"id": CHAT_ID, "type": "private"}, "date": 0},
            "data": data,
        },
    }
