"""Deterministic Telegram commands (08 §4.2): ``/yardim``, ``/portfoy``, ``/izle``, ``/birak``,
``/mutabakat``, ``/geri``, ``/bugun``, ``/bekleyen``, ``/iptal``.

Callback data: ``rc:ok`` / ``rc:no`` (reconcile), ``pa:ok:<pending_action>`` / ``pa:no:<pending_action>``.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from papi.core.text import normalize_tr, parse_tr_number
from papi.core.timeutil import IST, to_ist
from papi.notify.alerts import format_line
from papi.notify.context import events_since, load_event_facts, load_universe
from papi.portfolio.fmt import fmt_date, fmt_num, fmt_pct, fmt_qty
from papi.portfolio.service import (
    apply_pending_action,
    apply_reconcile,
    dismiss_pending_action,
    holding,
    pending_actions,
    positions,
    reconcile,
    unwatch,
    watch,
)
from papi.telegram.api import button, esc, keyboard
from papi.telegram.conversation import Turn, clear_pending, get_pending, set_pending
from papi.telegram.trade_flow import last_undoable, undo

HELP_TEXT = """🤖 <b>papi</b> — KAP bildirimleri ve portföy asistanı

<b>İşlem kaydı</b> (✅ olmadan hiçbir şey yazılmaz)
• <code>THYAO 100 lot 285,40'tan aldım</code>
• <code>100 adet ASELS 62,5 TL'den sattım</code>
• <code>ASELS'in yarısını 62'den sattım</code> · <code>dün</code> / <code>27.10.2026</code>
• <code>/al THYAO 100 285,40 [tarih]</code> · <code>/sat ASELS 50 62,5</code>

<b>Komutlar</b>
/portfoy — pozisyonlar
/bugun — bugünkü bildirimler (portföy + izleme)
/bekleyen — bekleyen kurumsal işlemler
/izle THYAO · /birak THYAO — izleme listesi
/mutabakat — İşCep ile adım adım karşılaştırma
/geri — son Telegram işlemini geri al
/iptal — yarım kalan diyaloğu bırak
/yardim — bu mesaj"""

SHORT_HELP = (
    "Bunu bir işlem olarak anlayamadım. Örnek: <code>THYAO 100 lot 285,40'tan aldım</code>\n"
    "Komutlar için /yardim."
)
QUESTION_REPLY = (
    "Sorulara yanıt verme özelliği henüz açık değil. Şimdilik: /portfoy, /bugun, /bekleyen, /yardim.\n"
    "İşlem kaydı için örnek: <code>THYAO 100 lot 285,40'tan aldım</code>"
)
OK_WORDS = {"ok", "tamam", "evet", "ayni", "dogru", "yes", "same", "+", "✓", "✅"}
DONE_WORDS = {"yok", "bitti", "hayir", "no", "none", "done", "tamam", "ok"}
_PAIR = re.compile(r"([A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü0-9]{1,11})\s*[:=]?\s+(\d[\d.,]*)")


def cmd_help(turn: Turn, arg: str) -> None:
    turn.reply(HELP_TEXT)


def cmd_portfolio(turn: Turn, arg: str) -> None:
    pos = positions(turn.conn)
    if not pos:
        turn.reply("💼 Portföyde açık pozisyon yok.")
        return
    header = f"{'HİSSE':<7}{'ADET':>9}{'ORT.MAL':>10}{'SON':>9}{'AĞIRLIK':>8}"
    rows = [header]
    for p in pos:
        rows.append(
            f"{p.ticker:<7}{fmt_qty(p.quantity):>9}{fmt_num(p.avg_cost):>10}{fmt_num(p.last_close):>9}"
            f"{fmt_pct(p.weight * 100 if p.weight is not None else None):>8}"
        )
    total = sum((p.market_value for p in pos if p.market_value is not None), Decimal(0))
    cost = sum((p.cost_total for p in pos), Decimal(0))
    text = f"💼 <b>Portföy</b> · {fmt_date(to_ist(turn.now).date())}\n<pre>{esc(chr(10).join(rows))}</pre>"
    text += f"\nPiyasa değeri: {fmt_num(total)} ₺ · Maliyet: {fmt_num(cost)} ₺"
    if any(p.last_close is None for p in pos):
        text += "\n<i>Fiyatı olmayan pozisyonlar değere dahil değil.</i>"
    turn.reply(text)


def cmd_watch(turn: Turn, arg: str) -> None:
    if not arg:
        turn.reply("Kullanım: <code>/izle THYAO</code>")
        return
    try:
        watch(turn.conn, arg)
    except ValueError as exc:
        turn.reply(f"❗ {esc(exc)}")
        return
    turn.reply(f"👁 <b>{esc(arg.upper())}</b> izleme listesine eklendi.")


def cmd_unwatch(turn: Turn, arg: str) -> None:
    if not arg:
        turn.reply("Kullanım: <code>/birak THYAO</code>")
        return
    try:
        removed = unwatch(turn.conn, arg)
    except ValueError as exc:
        turn.reply(f"❗ {esc(exc)}")
        return
    if removed:
        turn.reply(f"<b>{esc(arg.upper())}</b> izleme listesinden çıkarıldı.")
    else:
        turn.reply(f"<b>{esc(arg.upper())}</b> izleme listesinde değildi.")


def cmd_undo(turn: Turn, arg: str) -> None:
    txn_id = last_undoable(turn.conn)
    if txn_id is None:
        turn.reply("Geri alınacak Telegram işlemi yok.")
        return
    undo(turn, txn_id)


def cmd_today(turn: Turn, arg: str) -> None:
    local = to_ist(turn.now)
    start = datetime.combine(local.date(), time(0, 0), IST)
    universe = load_universe(turn.conn)
    ids = universe.holdings | universe.watched
    lines = []
    seen: set[str] = set()
    for ev in events_since(turn.conn, start, ids):
        f = load_event_facts(turn.conn, ev["id"], universe)
        if f is not None and ev["id"] not in seen:
            lines.append(format_line(f))
            seen.add(ev["id"])
    title = f"📅 <b>Bugün</b> · {fmt_date(local.date())}"
    if not lines:
        turn.reply(f"{title}\nPortföy ve izleme listende bugün yeni bildirim yok.")
        return
    turn.reply(title + "\n" + "\n".join(lines))


def cmd_pending(turn: Turn, arg: str) -> None:
    items = pending_actions(turn.conn)
    if not items:
        turn.reply("📌 Bekleyen kurumsal işlem yok.")
        return
    for pa in items:
        proposal = pa.get("proposal") or {}
        lines = [f"📌 <b>{esc(pa.get('ticker') or '—')}</b> · {esc(pa.get('kind'))}"]
        if proposal.get("text_tr"):
            lines.append(esc(proposal["text_tr"]))
        if pa.get("ex_date"):
            lines.append(f"Hak kullanım: {fmt_date(pa['ex_date'])}")
        turn.reply(
            "\n".join(lines),
            reply_markup=keyboard(
                [button("✅ Uygula", f"pa:ok:{pa['id']}"), button("🚫 Yoksay", f"pa:no:{pa['id']}")]
            ),
        )


def cmd_cancel(turn: Turn, arg: str) -> None:
    if get_pending(turn.conn) is None:
        turn.reply("Yarım kalan bir işlem yok.")
        return
    clear_pending(turn.conn)
    turn.reply("Tamam, bıraktım.")


# ---------------------------------------------------------------------------------------- reconcile


def cmd_reconcile(turn: Turn, arg: str) -> None:
    items = [
        {"ticker": p.ticker, "quantity": str(p.quantity)}
        for p in sorted(positions(turn.conn), key=lambda p: p.ticker)
    ]
    if not items:
        set_pending(turn.conn, {"kind": "reconcile_extra", "observed": {}})
        turn.reply(
            "🧾 <b>Mutabakat</b>\npapi'de açık pozisyon yok. İşCep'teki hisseleri <code>TICKER adet</code> "
            "şeklinde yaz (ör. <code>ASELS 50</code>), bitince <code>yok</code>."
        )
        return
    set_pending(turn.conn, {"kind": "reconcile", "items": items, "idx": 0, "observed": {}})
    turn.reply("🧾 <b>Mutabakat</b> — her hisse için İşCep'teki adedi yaz ya da <code>ok</code>. "
               "(<code>iptal</code> ile çık)\n\n" + _reconcile_prompt(items, 0))  # fmt: skip


def _reconcile_prompt(items: list[dict[str, Any]], idx: int) -> str:
    it = items[idx]
    qty = fmt_qty(Decimal(it["quantity"]))
    return f"{idx + 1}/{len(items)} · <b>{esc(it['ticker'])}</b>: papi {qty} lot. İşCep?"


def _number(text: str) -> Decimal | None:
    try:
        value = parse_tr_number(text.replace("lot", "").replace("adet", ""))
    except (ValueError, InvalidOperation):
        return None
    return value if value >= 0 else None


def reconcile_reply(turn: Turn, pending: dict[str, Any], text: str) -> None:
    kind = pending["kind"]
    key = normalize_tr(text)
    observed: dict[str, str] = dict(pending.get("observed") or {})
    if kind == "reconcile":
        items, idx = pending["items"], int(pending["idx"])
        item = items[idx]
        if key in OK_WORDS:
            observed[item["ticker"]] = item["quantity"]
        elif key in {"yok", "sifir"}:
            observed[item["ticker"]] = "0"
        elif (value := _number(text)) is not None:
            observed[item["ticker"]] = str(value)
        else:
            turn.reply(
                "Anlayamadım: adet yaz (ör. <code>250</code>) ya da <code>ok</code>.\n"
                + _reconcile_prompt(items, idx)
            )
            return
        idx += 1
        if idx < len(items):
            set_pending(turn.conn, {**pending, "idx": idx, "observed": observed})
            turn.reply(_reconcile_prompt(items, idx))
            return
        set_pending(turn.conn, {"kind": "reconcile_extra", "observed": observed})
        turn.reply(
            "papi'de olmayan bir hisse var mı? <code>TICKER adet</code> yaz (ör. <code>ASELS 50</code>) "
            "ya da <code>yok</code>."
        )
        return
    if kind == "reconcile_extra":
        if key in DONE_WORDS:
            _reconcile_finish(turn, observed)
            return
        pairs = _PAIR.findall(text)
        if not pairs:
            turn.reply(
                "Anlayamadım: <code>TICKER adet</code> (ör. <code>ASELS 50</code>) ya da <code>yok</code>."
            )
            return
        for ticker, qty in pairs:
            value = _number(qty)
            if value is None:
                turn.reply(f"Adet anlaşılamadı: {esc(qty)}")
                return
            observed[ticker.upper()] = str(value)
        set_pending(turn.conn, {"kind": "reconcile_extra", "observed": observed})
        turn.reply("Eklendi. Başka? (<code>yok</code> ile bitir)")
        return
    turn.reply("Farkları uygulamak için yukarıdaki ✅ düğmesine bas ya da <code>iptal</code> yaz.")


def _observed(raw: dict[str, str]) -> dict[str, Decimal]:
    return {k: Decimal(v) for k, v in raw.items()}


def _reconcile_finish(turn: Turn, observed: dict[str, str]) -> None:
    diffs = reconcile(turn.conn, _observed(observed))
    if not diffs:
        clear_pending(turn.conn)
        turn.reply("✅ Mutabakat tamam: papi ile İşCep aynı.")
        return
    set_pending(turn.conn, {"kind": "reconcile_confirm", "observed": observed})
    lines = ["🧾 <b>Farklar</b>"]
    for d in diffs:
        sign = "+" if d.delta > 0 else ""
        line = (
            f"• <b>{esc(d.ticker)}</b>: papi {fmt_qty(d.papi_quantity)} → "
            f"İşCep {fmt_qty(d.observed_quantity)} (fark {sign}{fmt_qty(d.delta)})"
        )
        if d.instrument_id is None:
            line += " — uygulanamaz"
        lines.append(line)
        if d.hint:
            lines.append(f"  <i>{esc(d.hint)}</i>")
    applicable = [d for d in diffs if d.instrument_id is not None]
    if not applicable:
        clear_pending(turn.conn)
        turn.reply("\n".join(lines))
        return
    lines.append("\n✅ Uygula: papi'ye düzeltme kayıtları (reconcile_adjustment) eklenir.")
    turn.reply(
        "\n".join(lines), reply_markup=keyboard([button("✅ Uygula", "rc:ok"), button("❌ Vazgeç", "rc:no")])
    )


def reconcile_callback(turn: Turn, action: str, message_id: int) -> str:
    pending = get_pending(turn.conn)
    turn.client.edit_message_reply_markup(turn.chat, message_id, None)
    if not pending or pending.get("kind") != "reconcile_confirm":
        return "Bu mutabakat artık geçerli değil."
    clear_pending(turn.conn)
    if action != "ok":
        turn.reply("Mutabakat uygulanmadı.")
        return "Vazgeçildi"
    diffs = reconcile(turn.conn, _observed(pending["observed"]))
    ids = apply_reconcile(turn.conn, diffs)
    turn.reply(f"✅ {len(ids)} düzeltme kaydedildi. /portfoy ile kontrol edebilirsin.")
    return "Uygulandı"


def pending_action_callback(turn: Turn, action: str, pending_action_id: str, message_id: int) -> str:
    try:
        if action == "ok":
            apply_pending_action(turn.conn, pending_action_id)
        else:
            dismiss_pending_action(turn.conn, pending_action_id)
    except ValueError as exc:
        turn.client.edit_message_reply_markup(turn.chat, message_id, None)
        return str(exc)[:190]
    row = next((p for p in pending_actions(turn.conn, status="all") if p["id"] == pending_action_id), None)
    turn.client.edit_message_reply_markup(turn.chat, message_id, None)
    if action == "ok":
        pos = holding(turn.conn, row["instrument_id"]) if row else None
        suffix = f" {esc(pos.ticker)}: {fmt_qty(pos.quantity)} lot." if pos else ""
        turn.reply(f"✅ Uygulandı.{suffix}")
        return "Uygulandı"
    turn.reply("🚫 Yoksayıldı.")
    return "Yoksayıldı"


COMMANDS = {
    "yardim": cmd_help,
    "help": cmd_help,
    "start": cmd_help,
    "portfoy": cmd_portfolio,
    "izle": cmd_watch,
    "birak": cmd_unwatch,
    "mutabakat": cmd_reconcile,
    "geri": cmd_undo,
    "bugun": cmd_today,
    "bekleyen": cmd_pending,
    "iptal": cmd_cancel,
}
