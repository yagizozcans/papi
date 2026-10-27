"""Event taxonomy (PRD 06 §5.1) loaded from the jurisdiction pack's ``taxonomy.yaml``.

``load()`` validates the file (families exist, priors in range, schema files present) and is cached
per path. ``Taxonomy.label(code, lang)`` / ``family_of(code)`` are the helpers other slices use.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from papi.core.config import get_settings, resource_dir

UNCLASSIFIED = "other.unclassified"
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class TaxonomyError(ValueError):
    """The taxonomy file is malformed."""


@dataclass(frozen=True)
class Family:
    code: str
    label_tr: str
    label_en: str
    prior: int


@dataclass(frozen=True)
class EventCode:
    code: str
    family: str
    label_tr: str
    label_en: str
    prior: int  # effective prior (code override, else family prior)
    recurring: bool = False
    schema: str | None = None  # e.g. "event.distribution.dividend.cash.v1"


@dataclass(frozen=True, eq=False)
class Taxonomy:
    version: str
    families: dict[str, Family]
    codes: dict[str, EventCode]
    path: Path | None = None
    _schemas: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False, compare=False)

    def get(self, code: str) -> EventCode | None:
        return self.codes.get(code)

    def __contains__(self, code: object) -> bool:
        return code in self.codes

    def family_of(self, code: str) -> str:
        ec = self.codes.get(code)
        return ec.family if ec else code.split(".", 1)[0]

    def label(self, code: str, lang: str = "tr") -> str:
        ec = self.codes.get(code)
        if ec is None:
            return code
        return ec.label_en if lang == "en" else ec.label_tr

    def schema_for(self, code: str) -> dict[str, Any] | None:
        """Parsed JSON Schema of the code's payload (``None`` if the code has none)."""
        ec = self.codes.get(code)
        if ec is None or ec.schema is None:
            return None
        return self._schemas.get(ec.schema)


def default_path() -> Path:
    return get_settings().pack_dir / "taxonomy.yaml"


def schema_dir() -> Path:
    return resource_dir("contracts") / "schemas"


def load(path: Path | None = None) -> Taxonomy:
    """Load and validate the taxonomy (cached per resolved path)."""
    return _load_cached(str((path or default_path()).resolve()))


def clear_cache() -> None:
    _load_cached.cache_clear()


@functools.cache
def _load_cached(path_str: str) -> Taxonomy:
    path = Path(path_str)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaxonomyError(f"taxonomy file not found: {path}") from exc
    return parse(raw, path=path, schemas_root=schema_dir())


def _prior(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise TaxonomyError(f"{where}: prior must be an integer 0–100, got {value!r}")
    return value


def _label(entry: dict[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TaxonomyError(f"{where}: missing {key}")
    return value.strip()


def parse(raw: Any, *, path: Path | None = None, schemas_root: Path | None = None) -> Taxonomy:
    """Build a ``Taxonomy`` from the parsed YAML mapping, raising ``TaxonomyError`` on any problem."""
    if not isinstance(raw, dict):
        raise TaxonomyError("taxonomy root must be a mapping")
    version = raw.get("taxonomy_version")
    if not isinstance(version, str) or not version:
        raise TaxonomyError("taxonomy_version missing")
    fam_raw = raw.get("families")
    codes_raw = raw.get("codes")
    if not isinstance(fam_raw, dict) or not fam_raw:
        raise TaxonomyError("families missing")
    if not isinstance(codes_raw, dict) or not codes_raw:
        raise TaxonomyError("codes missing")

    families: dict[str, Family] = {}
    for name, entry in fam_raw.items():
        where = f"family {name}"
        if not isinstance(entry, dict):
            raise TaxonomyError(f"{where}: must be a mapping")
        families[name] = Family(
            code=name,
            label_tr=_label(entry, "label_tr", where),
            label_en=_label(entry, "label_en", where),
            prior=_prior(entry.get("prior"), where),
        )

    codes: dict[str, EventCode] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for code, entry in codes_raw.items():
        where = f"code {code}"
        if not isinstance(code, str) or not _CODE_RE.match(code):
            raise TaxonomyError(f"{where}: invalid code syntax")
        if not isinstance(entry, dict):
            raise TaxonomyError(f"{where}: must be a mapping")
        family = code.split(".", 1)[0]
        if family not in families:
            raise TaxonomyError(f"{where}: unknown family {family!r}")
        prior = _prior(entry["prior"], where) if "prior" in entry else families[family].prior
        schema = entry.get("schema")
        if schema is not None:
            if schema != f"event.{code}.v{schema.rsplit('.v', 1)[-1]}":
                raise TaxonomyError(f"{where}: schema name {schema!r} must be event.{code}.v<N>")
            if schemas_root is not None:
                schema_path = schemas_root / f"{schema}.json"
                if not schema_path.is_file():
                    raise TaxonomyError(f"{where}: schema file missing: {schema_path}")
                schemas[schema] = json.loads(schema_path.read_text(encoding="utf-8"))
        codes[code] = EventCode(
            code=code,
            family=family,
            label_tr=_label(entry, "label_tr", where),
            label_en=_label(entry, "label_en", where),
            prior=prior,
            recurring=bool(entry.get("recurring", False)),
            schema=schema,
        )
    if UNCLASSIFIED not in codes:
        raise TaxonomyError(f"{UNCLASSIFIED} must be defined")
    return Taxonomy(version=version, families=families, codes=codes, path=path, _schemas=schemas)
