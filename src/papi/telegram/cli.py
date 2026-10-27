"""``papi telegram`` — bot setup, test messages, manual polling and alert rules."""

from __future__ import annotations

from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from papi.core import db
from papi.core.bootstrap import init_home
from papi.core.config import KEYRING_TELEGRAM, get_settings, reset_settings, set_secret
from papi.core.state import set_state
from papi.core.timeutil import now_ist
from papi.notify.rules import RuleSyntaxError
from papi.notify.store import add_rule, list_rules, seed_default_rules, set_enabled
from papi.telegram import outbox
from papi.telegram.api import TelegramClient, TelegramError, esc, reset_client
from papi.telegram.bot import process_inbox
from papi.telegram.poller import OFFSET_KEY, poll_once
from papi.telegram.setup import wait_for_first_message, write_config_values

app = typer.Typer(help="Telegram botu: kurulum, test, kurallar.", no_args_is_help=True)
rules_app = typer.Typer(help="Uyarı kuralları (08 §3.1 filtre dili).", no_args_is_help=True)
app.add_typer(rules_app, name="rules")

console = Console(soft_wrap=True, markup=False, highlight=False)

WELCOME = (
    "✅ <b>papi bağlandı.</b>\n"
    "KAP uyarıları buraya gelecek. İşlemlerini yazabilirsin, "
    "ör. <code>THYAO 100 lot 285,40'tan aldım</code>.\n"
    "Komutlar: /yardim"
)


def _fail(message: str) -> None:
    console.print(message)
    raise typer.Exit(1)


@app.command()
def setup(
    token: str | None = typer.Option(
        None, "--token", help="Bot token (verilmezse sorulur, ekrana yazılmaz)."
    ),
    wait: int = typer.Option(300, "--wait", help="İlk mesajın beklenme süresi (saniye)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Kullanıcı onayını sorma."),
) -> None:
    """Token'ı Keychain'e kaydet, ilk mesajdan kullanıcı/sohbet kimliğini al, config'e yaz, kuralları kur."""
    init_home()
    token = (token or typer.prompt("Telegram bot token'ı (BotFather → /newbot)", hide_input=True)).strip()
    client = TelegramClient(token, user_agent=get_settings().kap.resolved_user_agent)
    try:
        me = client.get_me()
    except TelegramError as exc:
        _fail(f"Token doğrulanamadı: {exc.description}")
        return
    username = me.get("username") or me.get("first_name") or "bot"
    console.print(f"Bot: @{username} (id {me.get('id')})")
    try:
        set_secret(KEYRING_TELEGRAM, token)
        console.print("Token macOS Keychain'e kaydedildi (servis 'papi', anahtar 'telegram_token').")
    except Exception as exc:  # keyring backends raise various errors
        console.print(
            f"Uyarı: token Keychain'e yazılamadı ({exc}). PAPI_TELEGRAM_TOKEN ortam değişkenini kullan."
        )

    console.print(
        f"Şimdi Telegram'da @{username} ile özel sohbet aç ve bir mesaj gönder (ör. /start). "
        f"{wait} sn bekleniyor…"
    )
    msg, offset = wait_for_first_message(client, wait_s=wait)
    if msg is None:
        _fail("Mesaj gelmedi. Komutu tekrar çalıştır.")
        return
    user, chat = msg["from"], msg["chat"]
    who = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    handle = f"@{user['username']}" if user.get("username") else "kullanıcı adı yok"
    console.print(f"Mesaj geldi: {who} ({handle}), kullanıcı id {user['id']}, sohbet id {chat['id']}")
    if not yes and not typer.confirm("Bu sen misin? Yalnız bu kullanıcıya yanıt verilecek.", default=True):
        _fail("İptal edildi; config değiştirilmedi.")
        return

    write_config_values(
        get_settings().config_path,
        "telegram",
        {"enabled": True, "allowed_user_id": int(user["id"]), "chat_id": int(chat["id"])},
    )
    reset_settings()
    reset_client()
    console.print(
        f"Config güncellendi: {get_settings().config_path} [telegram] enabled/allowed_user_id/chat_id"
    )
    with db.transaction() as conn:
        if offset is not None:
            set_state(conn, OFFSET_KEY, offset)  # the setup message is not processed as a chat message
        seeded = seed_default_rules(conn)
        sent = outbox.send(conn, WELCOME, client=client)
    console.print(
        f"Varsayılan uyarı kuralları: {seeded} eklendi. Test mesajı gönderildi (#{sent.message_id})."
    )


@app.command()
def test() -> None:
    """Sohbete bir test mesajı gönder."""
    try:
        with db.transaction() as conn:
            sent = outbox.send(conn, f"🔔 papi test mesajı · {now_ist().strftime('%d.%m.%Y %H:%M')}")
    except (outbox.TelegramUnavailable, TelegramError) as exc:
        _fail(f"Gönderilemedi: {exc}")
        return
    console.print(f"Gönderildi (mesaj #{sent.message_id}).")


@app.command("send")
def send_text(
    text: str = typer.Argument(..., help="Mesaj metni."),
    html: bool = typer.Option(False, "--html", help="Metin Telegram HTML'i olarak gönderilsin."),
) -> None:
    """Sohbete serbest bir mesaj gönder."""
    try:
        with db.transaction() as conn:
            sent = outbox.send(conn, text if html else esc(text))
    except (outbox.TelegramUnavailable, TelegramError) as exc:
        _fail(f"Gönderilemedi: {exc}")
        return
    console.print(f"Gönderildi (mesaj #{sent.message_id}).")


@app.command("poll-once")
def poll_once_cmd(
    timeout: int = typer.Option(0, "--timeout", help="Long-poll süresi (saniye)."),
    process: bool = typer.Option(True, "--process/--no-process", help="Gelen mesajları hemen işle."),
) -> None:
    """Bir getUpdates turu çalıştır (worker olmadan elle test için)."""
    try:
        result = poll_once(timeout=timeout)
    except (outbox.TelegramUnavailable, TelegramError) as exc:
        _fail(f"Alınamadı: {exc}")
        return
    console.print(
        f"güncelleme {result.received} · mesaj {result.messages} · düğme {result.callbacks} · "
        f"yoksayılan {result.ignored}"
    )
    if process and result.messages:
        with db.transaction() as conn:
            n = process_inbox(conn)
        console.print(f"işlenen mesaj: {n}")


# ---------------------------------------------------------------------------------------- rules


@rules_app.command("list")
def rules_list() -> None:
    """Kuralları listele."""
    with db.connect() as conn:
        rows = list_rules(conn)
    table = Table("ad", "durum", "kanallar", "sessiz saat", "filtre")
    for r in rows:
        quiet = r["quiet_hours"]
        table.add_row(
            r["name"],
            "açık" if r["enabled"] else "kapalı",
            ",".join(r["channels"] or []),
            f"{quiet['start']}-{quiet['end']}" if quiet else "genel",
            r["filter"],
        )
    console.print(table)


@rules_app.command("add")
def rules_add(
    name: str = typer.Argument(..., help="Kural adı (benzersiz)."),
    filter_text: str = typer.Argument(..., metavar="FILTER", help="Filtre, ör. 'scope = \"holdings\"'."),
    channel: list[str] = typer.Option(
        ["telegram", "macos"], "--channel", "-c", help="telegram ve/veya macos."
    ),
    quiet: str | None = typer.Option(None, "--quiet", help="Kurala özel sessiz saat, ör. 22:00-08:00."),
) -> None:
    """Yeni kural ekle (filtre doğrulanır)."""
    quiet_hours = None
    if quiet:
        start, _, end = quiet.partition("-")
        try:
            datetime.strptime(start.strip(), "%H:%M")
            datetime.strptime(end.strip(), "%H:%M")
        except ValueError:
            _fail("Sessiz saat biçimi: SS:DD-SS:DD (ör. 22:00-08:00)")
            return
        quiet_hours = {"start": start.strip(), "end": end.strip()}
    try:
        with db.transaction() as conn:
            add_rule(conn, name, filter_text, channels=channel, quiet_hours=quiet_hours)
    except RuleSyntaxError as exc:
        _fail(f"Filtre hatalı: {exc}")
        return
    except ValueError as exc:
        _fail(str(exc))
        return
    console.print(f"Eklendi: {name}")


@rules_app.command("disable")
def rules_disable(ref: str = typer.Argument(..., help="Kural adı veya id.")) -> None:
    """Kuralı kapat."""
    with db.transaction() as conn:
        found = set_enabled(conn, ref, False)
    if not found:
        _fail(f"Kural bulunamadı: {ref}")
    console.print(f"Kapatıldı: {ref}")


@rules_app.command("enable")
def rules_enable(ref: str = typer.Argument(..., help="Kural adı veya id.")) -> None:
    """Kapalı kuralı yeniden aç."""
    with db.transaction() as conn:
        found = set_enabled(conn, ref, True)
    if not found:
        _fail(f"Kural bulunamadı: {ref}")
    console.print(f"Açıldı: {ref}")
