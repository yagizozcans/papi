"""Turkish text for the Claude usage governor state (``papi.llm.governor.status``): used by
``/kullanim`` and the weekly review."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from papi.notify.alerts import fmt_local
from papi.portfolio.fmt import fmt_num
from papi.telegram.api import esc

PAUSE_REASON_TR = {"manual": "elle", "limit": "kullanım limiti", "telegram": "Telegram /durdur"}
BARS = (("five_hour", "5 saatlik"), ("seven_day", "haftalık"))


def usd(value: float | None) -> str:
    return "—" if value is None else f"${fmt_num(Decimal(str(value)), 2)}"


def _share(used: float, cap: float) -> str:
    line = f"{usd(used)} / {usd(cap)}"
    if cap > 0:
        line += f" (%{fmt_num(Decimal(str(used / cap * 100)), 0)})"
    return line


def pause_line(st: dict[str, Any]) -> str:
    if not st.get("paused"):
        return "▶️ Claude analizi çalışıyor."
    reason = PAUSE_REASON_TR.get(st.get("pause_reason") or "", st.get("pause_reason") or "")
    until: datetime | None = st.get("paused_until")
    when = f"{fmt_local(until)}'e kadar" if until else "/devam yazılana kadar"
    return f"⏸ Claude analizi durduruldu ({esc(reason)}) — {when}."


def usage_lines(st: dict[str, Any], *, include_tasks: bool = False) -> list[str]:
    """Pause state, papi's usage vs caps, live subscription bars and deferred LLM jobs."""
    lines = [pause_line(st), "", "<b>papi kullanımı</b> (tahmini maliyet / pay)"]
    lines.append(f"• 5 saat (analiz): {_share(st['used_5h'], st['cap_5h'])}")
    lines.append(f"• 5 saat (sohbet): {_share(st['used_chat_5h'], st['cap_chat_5h'])}")
    lines.append(f"• 7 gün: {_share(st['used_week'], st['cap_week'])}")
    if st.get("calibration_mode"):
        lines.append("<i>Kalibrasyon modu açık: paylar ayrıca abonelik çubuklarının %10'u ile sınırlı.</i>")
    bars = st.get("subscription_bars")
    bar_lines = []
    for key, label in BARS:
        bar = (bars or {}).get(key)
        if bar and bar.get("utilization") is not None:
            pct = fmt_num(Decimal(str(bar["utilization"])) * 100, 0)
            reset = f" · sıfırlanma {fmt_local(bar['resets_at'])}" if bar.get("resets_at") else ""
            bar_lines.append(f"• {label} çubuk: %{pct}{reset}")
    if bar_lines:
        lines += ["", f"<b>Claude aboneliği</b> (tüm kullanım, {fmt_local(bars['observed_at'])} itibarıyla)"]
        lines += bar_lines
    queue = st.get("queue") or {}
    total = int(queue.get("llm_deferred_total") or 0)
    lines.append("")
    if total:
        parts = ", ".join(f"{k} {v}" for k, v in sorted((queue.get("llm_deferred") or {}).items()))
        lines.append(f"⏳ Bekleyen Claude işleri: {total} ({esc(parts)})")
    else:
        lines.append("Bekleyen Claude işi yok.")
    if include_tasks and st.get("by_task"):
        lines += ["", "<b>Görevler (7 gün)</b>"]
        for task, t in sorted(st["by_task"].items()):
            fail = f", {t['failures']} hatalı" if t.get("failures") else ""
            lines.append(f"• {esc(task)}: {t['calls']} çağrı, {usd(t['cost'])}{fail}")
    return lines
