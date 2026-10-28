"""Guarded access to other slices (portfolio, LLM governor, triage taxonomy).

The dashboard must render even when an optional slice is not installed: each helper returns ``None``
(or raises :class:`SliceUnavailable` for actions) only when the *slice module itself* is missing; any
other import error or runtime error propagates.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from functools import lru_cache
from types import ModuleType
from typing import Any

from sqlalchemy import Connection


class SliceUnavailable(RuntimeError):
    """The slice module providing a feature is not installed."""

    def __init__(self, module: str) -> None:
        super().__init__(f"{module} yüklü değil")
        self.module = module


def _optional(modname: str) -> ModuleType | None:
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as exc:
        if exc.name and (modname == exc.name or modname.startswith(exc.name + ".")):
            return None
        raise


def _required(modname: str) -> ModuleType:
    mod = _optional(modname)
    if mod is None:
        raise SliceUnavailable(modname)
    return mod


# ------------------------------------------------------------------------------------------ portfolio


def positions(conn: Connection) -> list[Any] | None:
    """``papi.portfolio.service.positions`` for the default portfolio; None if the slice is missing."""
    mod = _optional("papi.portfolio.service")
    if mod is None:
        return None
    return list(mod.positions(conn))


def pending_actions(conn: Connection, status: str = "expected") -> list[dict] | None:
    mod = _optional("papi.portfolio.service")
    if mod is None:
        return None
    return list(mod.pending_actions(conn, status=status))


def apply_pending_action(conn: Connection, pending_action_id: str, quantity_override: Decimal | None) -> str:
    mod = _required("papi.portfolio.service")
    return mod.apply_pending_action(conn, pending_action_id, quantity_override=quantity_override)


def dismiss_pending_action(conn: Connection, pending_action_id: str) -> None:
    mod = _required("papi.portfolio.service")
    mod.dismiss_pending_action(conn, pending_action_id)


# ------------------------------------------------------------------------------------------ LLM governor


def governor_status(conn: Connection) -> dict | None:
    mod = _optional("papi.llm.governor")
    if mod is None:
        return None
    return dict(mod.status(conn))


def governor_pause(conn: Connection) -> None:
    _required("papi.llm.governor").pause(conn, reason="manual")


def governor_resume(conn: Connection) -> None:
    _required("papi.llm.governor").resume(conn)


# ------------------------------------------------------------------------------------------ taxonomy


@lru_cache(maxsize=1)
def _taxonomy() -> Any | None:
    mod = _optional("papi.triage.taxonomy")
    if mod is None:
        return None
    return mod.load()


def code_label(code: str | None, lang: str = "tr") -> str:
    if not code:
        return ""
    tax = _taxonomy()
    return tax.label(code, lang) if tax is not None else code


def family_of(code: str | None) -> str:
    if not code:
        return ""
    tax = _taxonomy()
    return tax.family_of(code) if tax is not None else code.split(".", 1)[0]


def families(lang: str = "tr") -> list[tuple[str, str]] | None:
    """``[(family_code, label)]`` from the taxonomy; None if triage is missing."""
    tax = _taxonomy()
    if tax is None:
        return None
    return [(f.code, f.label_tr if lang == "tr" else f.label_en) for f in tax.families.values()]


def family_codes(family: str) -> list[str] | None:
    """Every event code of ``family`` per the taxonomy; None if triage is missing."""
    tax = _taxonomy()
    if tax is None:
        return None
    return [c.code for c in tax.codes.values() if c.family == family]


def band_floors() -> dict[str, int] | None:
    """Lowest materiality score of each band (``low`` 0 … ``critical``) from triage; None if missing."""
    mod = _optional("papi.triage.materiality")
    if mod is None:
        return None
    floors = {}
    lower = 0
    for bound, name in mod.BANDS:  # ((30, "low"), (60, "medium"), (80, "high")): score < bound → name
        floors[name] = lower
        lower = bound
    floors["critical"] = lower
    return floors
