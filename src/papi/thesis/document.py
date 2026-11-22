"""The thesis document: fixed sections, deterministic parsing and checks.

A thesis is Markdown with YAML front matter (``ticker``, ``date``, ``direction``, ``horizon``) and
``## <Section>`` headings from :data:`SECTIONS` (aliases accepted, matched after ``normalize_tr``).
HTML comments are guidance and are ignored. Every sentence or bullet gets a marker ``[T<n>]`` that
the analyst cites. Trigger lines are ``- event | window | probability`` (``%40``, ``40%`` or ``40``);
kill-signal lines are ``- signal | threshold``.

*Errors* make the document unreadable (bad front matter, unknown or repeated heading, stray text) and
stop a review. *Gaps* (empty required section, malformed trigger/kill line) are reported but the
document can still be reviewed: they are part of the review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from papi.core.config import papi_home
from papi.core.text import normalize_tr

DIRECTIONS = ("long", "short", "izle")
TICKER_RE = re.compile(r"^[A-Z0-9]{3,6}$")


@dataclass(frozen=True)
class SectionSpec:
    key: str
    title: str
    required: bool
    guidance: str
    aliases: tuple[str, ...] = ()


SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        "fikir",
        "Fikir",
        True,
        "Tek paragraf: ne görüyorsun, hangi uyumsuzluk dikkatini çekti?",
        ("idea", "tez"),
    ),
    SectionSpec(
        "konsensus",
        "Konsensüs",
        True,
        "Bugünkü fiyat neyi varsayıyor? Piyasanın beklentisini yaz, kendi görüşünü değil "
        "(ör. 'F/K 3 ise piyasa kârın kalıcı olmadığını düşünüyor').",
        ("consensus", "piyasa beklentisi"),
    ),
    SectionSpec(
        "farkli_gorus",
        "Farklı Görüş",
        True,
        "Seni konsensüsten ayıran, doğruysa fiyatı değiştirecek görüş. Neden haklısın?",
        ("farkli gorus", "karsit fikir", "karsit gorus", "variant perception"),
    ),
    SectionSpec(
        "edge",
        "Edge",
        True,
        "Kaynağı seç: davranışsal / analitik / bilgi / teknik-yapısal. Neden bu avantaj sende de var?",
        ("edge kaynagi", "avantaj"),
    ),
    SectionSpec(
        "karsi_taraf",
        "Karşı Taraf",
        True,
        "Bu hisseyi sana kim satıyor (veya senden kim alıyor) ve neden?",
        ("karsi taraf neden satiyor", "karsi taraf kim", "kim satiyor"),
    ),
    SectionSpec(
        "tetikleyiciler",
        "Tetikleyiciler",
        True,
        "Her satır: - olay | zaman aralığı | senin olasılık tahminin (ör. %40). "
        "Tezi fiyata yansıtacak somut olaylar.",
        ("tetikleyici", "trigger", "triggers", "katalizor", "katalizorler"),
    ),
    SectionSpec(
        "asagi_senaryo",
        "Aşağı Yönlü Senaryo",
        True,
        "Yanılırsan ne olur, fiyat nereye gider, neden?",
        ("asagi yonlu senaryo", "dusus senaryosu", "downside"),
    ),
    SectionSpec(
        "curutucu_sinyaller",
        "Tezi Çürütecek Sinyaller",
        True,
        "Her satır: - sinyal | ölçüt veya eşik. Görürsen tezi bırakacağın, ölçülebilir işaretler.",
        ("tezi curutecek sinyal", "curutucu sinyaller", "curutucu sinyal", "kill signals"),
    ),
    SectionSpec(
        "pre_mortem",
        "Pre-mortem",
        True,
        "Bir yıl sonra bu tez felaketle bitti. Neden? En olası 3 neden.",
        ("premortem", "mortem", "on otopsi"),
    ),
    SectionSpec(
        "dipnot",
        "Dipnot",
        False,
        "Bakılması gereken veriler, açık sorular.",
        ("dipnotlar", "notlar", "not"),
    ),
)
SPEC_BY_KEY = {s.key: s for s in SECTIONS}
LINE_SECTIONS = {"tetikleyiciler": 3, "curutucu_sinyaller": 2}  # pipe fields expected per bullet

_ALIASES: dict[str, str] = {}
for _spec in SECTIONS:
    for _name in (_spec.title, _spec.key.replace("_", " "), *_spec.aliases):
        _ALIASES[normalize_tr(_name)] = _spec.key

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=\S)")
_PROB = re.compile(r"^%?\s*(\d+(?:[.,]\d+)?)\s*%?$")


class ThesisError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Item:
    marker: int
    text: str
    bullet: bool


@dataclass(frozen=True)
class Trigger:
    marker: int
    event: str
    window: str | None
    probability: float | None  # 0..1, the user's own estimate


@dataclass(frozen=True)
class KillSignal:
    marker: int
    signal: str
    threshold: str | None


@dataclass
class Thesis:
    ref: str
    ticker: str
    date: date
    direction: str
    horizon: str
    sections: dict[str, list[Item]]
    triggers: list[Trigger] = field(default_factory=list)
    kill_signals: list[KillSignal] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """What the analyst reads: sections in template order with ``[T<n>]`` markers."""
        return {
            "ref": self.ref,
            "ticker": self.ticker,
            "date": self.date.isoformat(),
            "direction": self.direction,
            "horizon": self.horizon,
            "sections": [
                {
                    "key": spec.key,
                    "title": spec.title,
                    "required": spec.required,
                    "text": [f"[T{i.marker}] {i.text}" for i in self.sections.get(spec.key, [])],
                }
                for spec in SECTIONS
            ],
            "triggers": [
                {"marker": t.marker, "event": t.event, "window": t.window, "user_probability": t.probability}
                for t in self.triggers
            ],
            "kill_signals": [
                {"marker": k.marker, "signal": k.signal, "threshold": k.threshold} for k in self.kill_signals
            ],
            "gaps": self.gaps,
        }


def _heading_key(title: str) -> str | None:
    return _ALIASES.get(normalize_tr(title))


def heading_in_line(line: str) -> tuple[str | None, str]:
    """``(section key, text after the heading)`` when a line of a free-form paper is a section
    heading: the whole line is a section name (``→ Fikir``, ``## Konsensüs``, ``Karşı Taraf Neden
    Satıyor?``) or a name followed by ``:`` and text (``Dipnot: …``); else ``(None, line)``."""
    key = _heading_key(line)
    if key:
        return key, ""
    head, sep, rest = line.partition(":")
    if sep:
        key = _heading_key(head)
        if key:
            return key, rest.strip()
    return None, line


def _probability(raw: str) -> float | None:
    m = _PROB.match(raw.strip())
    if not m:
        raise ValueError(raw)
    value = float(m.group(1).replace(",", "."))
    if not 0 <= value <= 100:
        raise ValueError(raw)
    return value / 100


def _meta(raw: str, errors: list[str]) -> tuple[str, date, str, str]:
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        errors.append(f"ön bilgi (front matter) okunamadı: {exc}")
        meta = {}
    if not isinstance(meta, dict):
        errors.append("ön bilgi bir anahtar: değer listesi olmalı")
        meta = {}
    unknown = set(meta) - {"ticker", "date", "direction", "horizon"}
    if unknown:
        errors.append(f"bilinmeyen ön bilgi alanları: {sorted(unknown)}")
    ticker = str(meta.get("ticker") or "").strip().upper()
    if not TICKER_RE.match(ticker):
        errors.append("ticker: BIST kodu olmalı (ör. THYAO)")
    day = meta.get("date")
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            day = None
    if not isinstance(day, date):
        errors.append("date: YYYY-AA-GG olmalı")
        day = date.min
    direction = str(meta.get("direction") or "").strip().lower()
    if direction not in DIRECTIONS:
        errors.append(f"direction: {' | '.join(DIRECTIONS)} olmalı")
    horizon = str(meta.get("horizon") or "").strip()
    if not horizon:
        errors.append("horizon: vade yazılmalı (ör. 6-12 ay)")
    return ticker, day, direction, horizon


def _blocks(lines: list[str]) -> list[tuple[str, bool]]:
    """(text, is_bullet) blocks: each bullet is one block, consecutive prose lines form a paragraph."""
    out: list[tuple[str, bool]] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append((" ".join(para), False))
            para.clear()

    for line in lines:
        if not line.strip():
            flush()
            continue
        m = _BULLET.match(line)
        if m:
            flush()
            out.append((m.group(1).strip(), True))
        elif out and out[-1][1] and line.startswith((" ", "\t")) and not para:
            out[-1] = (f"{out[-1][0]} {line.strip()}", True)  # wrapped bullet
        else:
            para.append(line.strip())
    flush()
    return [(t, b) for t, b in out if t]


def parse(text: str, ref: str) -> Thesis:
    """Parse and check a thesis; raises :class:`ThesisError` with every error found."""
    errors: list[str] = []
    m = _FRONTMATTER.match(text.replace("\r\n", "\n"))
    if not m:
        raise ThesisError(["dosya '---' ile başlayan ön bilgi (ticker, date, direction, horizon) içermeli"])
    ticker, day, direction, horizon = _meta(m.group(1), errors)
    body = _COMMENT.sub("", m.group(2))

    raw: dict[str, list[str]] = {}
    current: str | None = None
    for n, line in enumerate(body.split("\n"), start=1):
        if line.startswith("## "):
            title = line[3:].strip().rstrip(":?").strip()
            key = _heading_key(title)
            if key is None:
                valid = ", ".join(s.title for s in SECTIONS)
                errors.append(f"bilinmeyen başlık '## {title}' (geçerli: {valid})")
                current = None
            elif key in raw:
                errors.append(f"'{SPEC_BY_KEY[key].title}' bölümü iki kez yazılmış")
                current = None
            else:
                raw[key] = []
                current = key
        elif line.startswith("# ") and current is None and not raw:
            continue  # optional document title
        elif current is not None:
            raw[current].append(line)
        elif line.strip() and not errors:
            errors.append(f"satır {n}: başlık altında olmayan metin (önce '## <Bölüm>' yaz)")
    if errors:
        raise ThesisError(errors)

    thesis = Thesis(ref=ref, ticker=ticker, date=day, direction=direction, horizon=horizon, sections={})
    marker = 0
    for spec in SECTIONS:
        items: list[Item] = []
        for block, bullet in _blocks(raw.get(spec.key, [])):
            parts = [block] if bullet else [s.strip() for s in _SENTENCE_END.split(block) if s.strip()]
            for part in parts:
                marker += 1
                items.append(Item(marker, part, bullet))
        thesis.sections[spec.key] = items
        if spec.required and not items:
            thesis.gaps.append(f"'{spec.title}' bölümü boş veya yok")
        if spec.key in LINE_SECTIONS:
            _parse_lines(thesis, spec, items)
    return thesis


def _parse_lines(thesis: Thesis, spec: SectionSpec, items: list[Item]) -> None:
    want = LINE_SECTIONS[spec.key]
    for item in items:
        fields = [f.strip() for f in item.text.split("|")]
        if not item.bullet or len(fields) > want:
            thesis.gaps.append(
                f"T{item.marker} ('{spec.title}'): satır '- "
                + (" | ".join(["olay", "zaman", "olasılık"]) if want == 3 else "sinyal | eşik")
                + "' biçiminde olmalı"
            )
            continue
        if spec.key == "tetikleyiciler":
            prob: float | None = None
            if len(fields) == 3 and fields[2]:
                try:
                    prob = _probability(fields[2])
                except ValueError:
                    thesis.gaps.append(f"T{item.marker}: olasılık '{fields[2]}' okunamadı (ör. %40)")
            else:
                thesis.gaps.append(f"T{item.marker}: tetikleyiciye kendi olasılık tahminin yazılmamış")
            window = fields[1] if len(fields) > 1 and fields[1] else None
            if window is None:
                thesis.gaps.append(f"T{item.marker}: tetikleyicinin zaman aralığı yok")
            thesis.triggers.append(Trigger(item.marker, fields[0], window, prob))
        else:
            threshold = fields[1] if len(fields) > 1 and fields[1] else None
            if threshold is None:
                thesis.gaps.append(f"T{item.marker}: sinyalin ölçütü / eşiği yok")
            thesis.kill_signals.append(KillSignal(item.marker, fields[0], threshold))


# ------------------------------------------------------------------------------------------ storage


def theses_dir() -> Path:
    path = papi_home() / "theses"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(ref: str) -> Path:
    """Thesis file for ``ref``: a path to a ``.md`` file or a name in ``$PAPI_HOME/theses``."""
    candidate = Path(ref).expanduser()
    if candidate.suffix == ".md" and candidate.is_file():
        return candidate.resolve()
    path = theses_dir() / f"{Path(ref).stem}.md"
    if not path.is_file():
        names = ", ".join(p.stem for p in sorted(theses_dir().glob("*.md"))) or "-"
        raise FileNotFoundError(f"tez bulunamadı: {ref!r} (mevcut: {names})")
    return path


def load(ref: str) -> Thesis:
    path = resolve(ref)
    return parse(path.read_text(encoding="utf-8"), path.stem)


def template(ticker: str, day: date) -> str:
    lines = [
        "---",
        f"ticker: {ticker.upper()}",
        f"date: {day.isoformat()}",
        "direction: long  # long | short | izle",
        "horizon: 6-12 ay",
        "---",
        "",
    ]
    for spec in SECTIONS:
        optional = "" if spec.required else " (isteğe bağlı)"
        lines += [f"## {spec.title}", f"<!-- {spec.guidance}{optional} -->", ""]
    return "\n".join(lines)


def sources_dir(name: str) -> Path:
    """Uploaded originals and extracted pages of thesis ``name``."""
    return theses_dir() / "sources" / name


def new_path(ticker: str, day: date) -> Path:
    """Next free ``<TICKER>-<date>[-n].md`` (a name with uploaded sources counts as taken)."""
    base = theses_dir() / f"{ticker.upper()}-{day.isoformat()}"
    path, n = base.with_suffix(".md"), 2
    while path.exists() or sources_dir(path.stem).exists():
        path, n = base.with_name(f"{base.name}-{n}").with_suffix(".md"), n + 1
    return path
