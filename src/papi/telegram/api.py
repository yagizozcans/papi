"""Minimal synchronous Telegram Bot API client (httpx) + HTML helpers.

Only the methods papi needs: ``getMe``, ``getUpdates`` (long poll), ``sendMessage`` (HTML parse mode),
``editMessageText``, ``editMessageReplyMarkup``, ``answerCallbackQuery``, ``sendChatAction``.
The token comes from ``papi.core.config.get_telegram_token()`` (env ``PAPI_TELEGRAM_TOKEN`` or the
macOS keychain); it is never logged.
"""

from __future__ import annotations

import html
import time
from typing import Any

import httpx

from papi.core.config import get_settings, get_telegram_token

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096
_CHUNK_LEN = 4000

__all__ = [
    "API_BASE",
    "MAX_MESSAGE_LEN",
    "TelegramClient",
    "TelegramError",
    "button",
    "chunk_html",
    "esc",
    "get_client",
    "keyboard",
]


class TelegramError(Exception):
    """A Bot API call returned ``ok: false`` (or a non-JSON error response)."""

    def __init__(
        self, method: str, description: str, error_code: int | None = None, retry_after: int | None = None
    ):
        super().__init__(f"telegram {method}: {error_code or '?'} {description}")
        self.method = method
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def not_modified(self) -> bool:
        return "message is not modified" in self.description


def esc(value: Any) -> str:
    """Escape text for Telegram HTML parse mode: ``&``, ``<``, ``>`` and ``"`` (safe in attributes)."""
    return html.escape("" if value is None else str(value), quote=False).replace('"', "&quot;")


def button(text: str, data: str) -> dict[str, str]:
    """Inline keyboard callback button (``callback_data`` must be ≤ 64 bytes)."""
    if len(data.encode()) > 64:
        raise ValueError(f"callback_data too long: {data!r}")
    return {"text": text, "callback_data": data}


def keyboard(*rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [list(r) for r in rows if r]}


def chunk_html(text: str, limit: int = _CHUNK_LEN) -> list[str]:
    """Split a message on line boundaries so each part fits Telegram's 4096-character limit.

    papi's messages keep HTML tags within one line, so line splits never cut a tag. A single line
    longer than ``limit`` is hard-split (only plain text lines are that long).
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        timeout: float = 15.0,
        user_agent: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._timeout = timeout
        headers = {"User-Agent": user_agent} if user_agent else {}
        self._http = http or httpx.Client(timeout=timeout, headers=headers)

    def __repr__(self) -> str:  # never leak the token
        return "TelegramClient(<token>)"

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        body = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._http.post(f"{self._base}/{method}", json=body, timeout=timeout or self._timeout)
        except httpx.HTTPError as exc:
            # Re-raise without the URL (it contains the token).
            raise httpx.TransportError(f"telegram {method}: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(method, f"HTTP {resp.status_code} (non-JSON)", resp.status_code) from None
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(method, str(data.get("description", "")), data.get("error_code"), retry)
        return data.get("result")

    # ---- methods -------------------------------------------------------------------------------
    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(
        self, offset: int | None = None, *, timeout: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "limit": limit,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            timeout=timeout + 15.0,
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": reply_markup,
        }
        if reply_to_message_id is not None:
            params["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return self.call("sendMessage", params)

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> Any:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": reply_markup,
        }
        return self._tolerate_not_modified("editMessageText", params)

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict[str, Any] | None = None
    ) -> Any:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        return self._tolerate_not_modified("editMessageReplyMarkup", params)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> Any:
        return self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def send_chat_action(self, chat_id: int, action: str = "typing") -> Any:
        return self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def _tolerate_not_modified(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return self.call(method, params)
        except TelegramError as exc:
            if exc.not_modified:
                return None
            raise


_TOKEN_TTL_S = 60.0
_client: TelegramClient | None = None
_token: str | None = None
_checked_at = float("-inf")


def get_client() -> TelegramClient | None:
    """Process-wide client for the configured token, or ``None`` when no token is set.

    The token (env / keychain) is re-read at most once a minute, so ``papi telegram setup`` in another
    process takes effect without a restart and the keychain is not hit on every poll.
    """
    global _client, _token, _checked_at
    now = time.monotonic()
    if now - _checked_at >= _TOKEN_TTL_S:
        _checked_at = now
        token = get_telegram_token()
        if token != _token:
            if _client is not None:
                _client.close()
            _token = token
            _client = (
                TelegramClient(token, user_agent=get_settings().kap.resolved_user_agent) if token else None
            )
    return _client


def reset_client() -> None:
    """Forget the cached client/token (tests, or right after changing the token)."""
    global _client, _token, _checked_at
    if _client is not None:
        _client.close()
    _client, _token, _checked_at = None, None, float("-inf")
