"""Obligations layer (PRD 03 §3.3): load ``packs/tr/legal/obligations.yaml``, approval gate.

Storage (no schema change): one ``obligation`` row per YAML item, anchored on its primary provision
(``provision_id``), with ``event_codes``, ``actor`` and ``deadline_rule`` in their columns. The
schema has no column for the trigger, downstream steps, extra citations or the approval flag, so
``required_fields`` (the only JSON column) holds a wrapper::

    {"fields": [<required field names>],
     "meta": {"key", "title_tr", "trigger", "downstream_steps", "citation_ids", "approved",
              "approved_at", "content_hash", "source", "drafted_by", "note"}}

``papi.kb.api`` reads ``fields`` as ``Obligation.required_fields`` and ``meta.approved`` for the
gate. Re-loading keeps approvals unless the item's content changed (then it needs review again).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, func, select

from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.schema import obligation, provision
from papi.core.timeutil import iso_utc, now_utc

DEADLINE_RE = re.compile(
    r"^(?:immediately|by_next_session_open|within_n_business_days\((\d+)\)|within_n_days\((\d+)\)|"
    r"before_event|no_deadline)$"
)


def parse_deadline_rule(rule: str) -> tuple[str, int | None]:
    """``"within_n_business_days(10)"`` → ``("within_n_business_days", 10)``."""
    m = DEADLINE_RE.match(rule)
    if not m:
        raise ValueError(f"unknown deadline_rule {rule!r}")
    n = m.group(1) or m.group(2)
    return rule.split("(")[0], int(n) if n else None


def _check_rule(v: str | None) -> str | None:
    if v is not None:
        parse_deadline_rule(v)
    return v


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9_]+$")
    label_tr: str
    actor: str | None = None
    deadline_rule: str | None = None
    after: str | None = None
    cites: list[str] = Field(default_factory=list)
    note: str | None = None

    @field_validator("deadline_rule")
    @classmethod
    def _rule(cls, v: str | None) -> str | None:
        return _check_rule(v)


class ObligationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    event_codes: list[str] = Field(min_length=1)
    title_tr: str
    citation: str
    also_cites: list[str] = Field(default_factory=list)
    actor: Literal["issuer", "board", "insider", "shareholder", "offeror", "exchange", "regulator"]
    trigger: str
    deadline_rule: str
    required_fields: list[str] = Field(default_factory=list)
    downstream_steps: list[StepSpec] = Field(default_factory=list)
    approved: bool = False
    note: str | None = None

    @field_validator("deadline_rule")
    @classmethod
    def _rule(cls, v: str) -> str:
        _check_rule(v)
        return v

    def content_hash(self) -> str:
        data = self.model_dump(exclude={"approved"})
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ObligationFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    drafted_by: str
    obligations: list[ObligationSpec]


@dataclass
class LoadReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    approvals_reset: list[str] = field(default_factory=list)
    missing_provisions: list[tuple[str, str]] = field(default_factory=list)  # (obligation id, citation)


def obligations_path() -> Path:
    return get_settings().pack_dir / "legal" / "obligations.yaml"


def read_obligations(path: Path | None = None) -> ObligationFile:
    with (path or obligations_path()).open(encoding="utf-8") as fh:
        return ObligationFile.model_validate(yaml.safe_load(fh))


def _by_key(conn: Connection) -> dict[str, Any]:
    rows = conn.execute(select(obligation)).all()
    return {((r.required_fields or {}).get("meta") or {}).get("key"): r for r in rows}


def load_obligations(conn: Connection, path: Path | None = None) -> LoadReport:
    """Import/refresh obligation rows from YAML (caller commits). Items whose primary provision is not
    parsed yet are skipped and reported."""
    spec = read_obligations(path)
    report = LoadReport()
    existing = _by_key(conn)
    src = str(path or obligations_path())
    for item in spec.obligations:
        pid = conn.execute(select(provision.c.id).where(provision.c.citation_id == item.citation)).scalar()
        if pid is None:
            report.missing_provisions.append((item.id, item.citation))
            continue
        h = item.content_hash()
        row = existing.get(item.id)
        old_meta = ((row.required_fields or {}).get("meta") or {}) if row is not None else {}
        approved = bool(old_meta.get("approved")) or item.approved
        approved_at = old_meta.get("approved_at") or (iso_utc(now_utc()) if item.approved else None)
        if (
            row is not None
            and old_meta.get("content_hash") != h
            and old_meta.get("approved")
            and not item.approved
        ):
            approved, approved_at = False, None
            report.approvals_reset.append(item.id)
        wrapper = {
            "fields": item.required_fields,
            "meta": {
                "key": item.id,
                "title_tr": item.title_tr,
                "trigger": item.trigger,
                "downstream_steps": [s.model_dump(exclude_none=True) for s in item.downstream_steps],
                "citation_ids": [item.citation, *item.also_cites],
                "approved": approved,
                "approved_at": approved_at,
                "content_hash": h,
                "source": src,
                "drafted_by": spec.drafted_by,
                "note": item.note,
            },
        }
        values = {
            "provision_id": pid,
            "event_codes": item.event_codes,
            "actor": item.actor,
            "deadline_rule": item.deadline_rule,
            "required_fields": wrapper,
        }
        if row is None:
            conn.execute(obligation.insert().values(id=uuid7(), **values))
            report.inserted += 1
        elif (
            old_meta.get("content_hash") == h
            and row.provision_id == pid
            and old_meta.get("approved") == approved
        ):
            report.unchanged += 1
        else:
            conn.execute(obligation.update().where(obligation.c.id == row.id).values(**values))
            report.updated += 1
    return report


def _set_approved(conn: Connection, row: Any, approved: bool) -> None:
    wrapper = dict(row.required_fields or {})
    meta = dict(wrapper.get("meta") or {})
    meta["approved"] = approved
    meta["approved_at"] = iso_utc(now_utc()) if approved else None
    wrapper["meta"] = meta
    conn.execute(obligation.update().where(obligation.c.id == row.id).values(required_fields=wrapper))


def approve(conn: Connection, ref: str, *, approved: bool = True) -> bool:
    """Approve (or un-approve) one obligation by YAML id or row id; False if not found."""
    row = conn.execute(select(obligation).where(obligation.c.id == ref)).first()
    if row is None:
        row = conn.execute(
            select(obligation).where(func.json_extract(obligation.c.required_fields, "$.meta.key") == ref)
        ).first()
    if row is None:
        return False
    _set_approved(conn, row, approved)
    return True


def approve_all_for(conn: Connection, event_code: str) -> list[str]:
    """Approve every obligation listing ``event_code``; returns their YAML ids."""
    done = []
    for row in conn.execute(select(obligation)).all():
        if event_code in (row.event_codes or []):
            _set_approved(conn, row, True)
            done.append(((row.required_fields or {}).get("meta") or {}).get("key", row.id))
    return done
