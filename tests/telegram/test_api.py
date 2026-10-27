from __future__ import annotations

import httpx
import pytest
import respx

from papi.telegram.api import TelegramClient, TelegramError, button, chunk_html, esc, get_client, keyboard
from tests.telegram.helpers import TOKEN, FakeTelegram

BASE = f"https://api.telegram.org/bot{TOKEN}"


def test_esc_html() -> None:
    assert esc('<b>A&B</b> "x"') == "&lt;b&gt;A&amp;B&lt;/b&gt; &quot;x&quot;"
    assert esc(None) == ""


def test_chunk_html_splits_on_lines() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = chunk_html(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text
    assert chunk_html("short") == ["short"]
    long_line = "y" * 2500
    assert chunk_html(long_line, 1000) == ["y" * 1000, "y" * 1000, "y" * 500]


def test_button_data_limit() -> None:
    assert button("ok", "t:ok:" + "a" * 36) == {"text": "ok", "callback_data": "t:ok:" + "a" * 36}
    with pytest.raises(ValueError):
        button("x", "y" * 65)
    assert keyboard([button("a", "1")], []) == {"inline_keyboard": [[{"text": "a", "callback_data": "1"}]]}


def test_send_message_html_and_reply_parameters() -> None:
    with respx.mock as router:
        fake = FakeTelegram(router)
        client = TelegramClient(TOKEN)
        msg = client.send_message(
            1, "<b>hi</b>", reply_to_message_id=9, reply_markup=keyboard([button("a", "b")])
        )
    body = fake.bodies()[0]
    assert msg["message_id"] == 5001
    assert body["parse_mode"] == "HTML"
    assert body["reply_parameters"] == {"message_id": 9, "allow_sending_without_reply": True}
    assert body["link_preview_options"] == {"is_disabled": True}
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "b"


def test_all_methods_hit_the_api() -> None:
    with respx.mock as router:
        fake = FakeTelegram(router)
        client = TelegramClient(TOKEN)
        assert client.get_me()["username"] == "papi_test_bot"
        fake.updates = [{"update_id": 1}, {"update_id": 2}]
        assert [u["update_id"] for u in client.get_updates(2, timeout=0)] == [2]
        client.edit_message_text(1, 2, "x", reply_markup=keyboard([button("a", "b")]))
        client.edit_message_reply_markup(1, 2)
        client.answer_callback_query("cq1", "tamam")
        client.send_chat_action(1)
    assert fake.methods() == [
        "getMe",
        "getUpdates",
        "editMessageText",
        "editMessageReplyMarkup",
        "answerCallbackQuery",
        "sendChatAction",
    ]
    assert fake.bodies("editMessageReplyMarkup")[0]["reply_markup"] == {"inline_keyboard": []}
    assert fake.bodies("getUpdates")[0]["allowed_updates"] == ["message", "edited_message", "callback_query"]


def test_api_error_and_flood_control() -> None:
    with respx.mock as router:
        router.post(f"{BASE}/sendMessage").mock(
            return_value=httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 7},
                },
            )
        )
        with pytest.raises(TelegramError) as info:
            TelegramClient(TOKEN).send_message(1, "x")
    assert info.value.retry_after == 7 and info.value.error_code == 429


def test_not_modified_is_tolerated() -> None:
    with respx.mock as router:
        router.post(f"{BASE}/editMessageText").mock(
            return_value=httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"},
            )
        )
        assert TelegramClient(TOKEN).edit_message_text(1, 2, "same") is None


def test_transport_error_does_not_leak_token() -> None:
    with respx.mock as router:
        router.post(f"{BASE}/getMe").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(httpx.TransportError) as info:
            TelegramClient(TOKEN).get_me()
    assert TOKEN not in str(info.value)
    assert TOKEN not in repr(TelegramClient(TOKEN))


def test_get_client_reads_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_client() is None
    from papi.telegram.api import reset_client

    monkeypatch.setenv("PAPI_TELEGRAM_TOKEN", TOKEN)
    reset_client()
    client = get_client()
    assert client is not None and get_client() is client
