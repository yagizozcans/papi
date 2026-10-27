"""Legal corpus manifest (``packs/<jurisdiction>/legal/manifest.yaml``, PRD 03 §2)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from papi.core.config import get_settings

Kind = Literal["law", "communique", "guideline", "directive", "board_decision", "exchange_rule"]
Priority = Literal["P0", "P1", "P2"]
Shape = Literal["articles", "sections"]

_REFRESH_WORDS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 91, "yearly": 365}
_REFRESH_DAYS = re.compile(r"^(\d+)\s*d$")


class HistoricalSource(BaseModel):
    """An older (e.g. originally published) text of the same document with its start date."""

    model_config = ConfigDict(extra="forbid")

    url: str
    effective_from: date
    label: str


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    official_code: str
    title: str
    authority: str
    kind: Kind
    source_urls: list[str] = Field(min_length=1)
    priority: Priority
    refresh: str
    citation_prefix: str = Field(pattern=r"^tr:[a-z-]+:[^:\s]+$")
    shape: Shape = "articles"
    history: list[HistoricalSource] = Field(default_factory=list)

    @field_validator("refresh")
    @classmethod
    def _check_refresh(cls, v: str) -> str:
        refresh_days(v)
        return v

    @property
    def refresh_seconds(self) -> float:
        return refresh_days(self.refresh) * 86400.0


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    documents: list[ManifestEntry]

    @field_validator("documents")
    @classmethod
    def _unique(cls, docs: list[ManifestEntry]) -> list[ManifestEntry]:
        for attr in ("id", "citation_prefix"):
            values = [getattr(d, attr) for d in docs]
            dupes = {v for v in values if values.count(v) > 1}
            if dupes:
                raise ValueError(f"duplicate {attr}: {sorted(dupes)}")
        return docs

    def get(self, entry_id: str) -> ManifestEntry:
        for d in self.documents:
            if d.id == entry_id:
                return d
        raise KeyError(f"manifest has no document {entry_id!r}")

    def select(self, *, ids: list[str] | None = None, priority: str | None = None) -> list[ManifestEntry]:
        docs = [self.get(i) for i in ids] if ids else list(self.documents)
        if priority:
            docs = [d for d in docs if d.priority == priority]
        return docs


def refresh_days(value: str) -> int:
    v = value.strip().lower()
    if v in _REFRESH_WORDS:
        return _REFRESH_WORDS[v]
    m = _REFRESH_DAYS.match(v)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    raise ValueError(f"refresh must be one of {sorted(_REFRESH_WORDS)} or '<n>d', got {value!r}")


def manifest_path() -> Path:
    return get_settings().pack_dir / "legal" / "manifest.yaml"


def load_manifest(path: Path | None = None) -> Manifest:
    p = path or manifest_path()
    with p.open(encoding="utf-8") as fh:
        return Manifest.model_validate(yaml.safe_load(fh))
