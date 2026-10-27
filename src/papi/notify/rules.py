"""Alert-rule filter language (08 §3.1).

Examples::

    scope = "holdings"
    watchlist = "Savunma" AND family IN ("business", "capital") AND materiality >= 60
    index = "XU030" AND code = "market.measure"
    scope = "holdings" AND code = "ownership.insider_trade" AND payload.net_value_try >= 5_000_000
    text ~ "geri alım" OR (ticker = "ASELS" AND NOT revision = true)

Grammar (keywords case-insensitive)::

    expr     := or
    or       := and ("OR" and)*
    and      := unary ("AND" unary)*
    unary    := "NOT" unary | "(" expr ")" | cond
    cond     := FIELD op value | FIELD ["NOT"] "IN" "(" value ("," value)* ")"
    op       := "=" | "!=" | ">" | ">=" | "<" | "<=" | "~"
    value    := "string" | 'string' | number (underscores allowed) | true | false

Fields: ``scope`` (``holdings``/``watchlist``/``all``/``ops``), ``ticker``, ``sector``, ``index``,
``watchlist``, ``code``, ``family``, ``materiality`` (0–100), ``band`` (ordered low < medium < high <
critical), ``priority`` (ordered P1 < P2 < P3 < P4, so ``priority <= "P2"`` means P1 or P2),
``revision``, ``late`` (KAP late-filing flag), ``catchup`` (seen after papi was offline),
``payload.<field>[.<sub>]`` and ``text`` (``~`` only).

Semantics: list-valued fields (``scope``, ``ticker``, ``sector``, ``index``, ``watchlist``) match when
**any** element matches (``!=``/``NOT IN``: when none does). String comparisons and ``~`` (substring)
are Turkish-normalised (``İ``/``i``, ``ş``/``s`` …). A string value ending in ``.*`` is a prefix match
(``code = "ownership.*"``). A missing value never matches (use ``NOT`` to invert).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from papi.core.text import normalize_tr

__all__ = ["FIELDS", "Rule", "RuleSyntaxError", "parse"]

LIST_FIELDS = {"scope", "ticker", "sector", "index", "watchlist"}
STRING_FIELDS = {"code", "family"}
BOOL_FIELDS = {"revision", "late", "catchup"}
FIELDS = LIST_FIELDS | STRING_FIELDS | BOOL_FIELDS | {"materiality", "band", "priority", "text"}
BAND_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
ORDER_OPS = {">", ">=", "<", "<="}


class RuleSyntaxError(ValueError):
    pass


# ------------------------------------------------------------------------------------------ lexer

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<num>-?\d[\d_]*(?:\.\d+)?)
      | (?P<op>!=|>=|<=|=|>|<|~)
      | (?P<punct>[(),])
      | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    )""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: Any
    pos: int


def _lex(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise RuleSyntaxError(f"beklenmeyen karakter {text[pos : pos + 10]!r} (konum {pos})")
        group = m.lastgroup or ""
        kind, raw, start = group, m.group(group), m.start(group)
        if kind == "str":
            value: Any = re.sub(r"\\(.)", r"\1", raw[1:-1])
        elif kind == "num":
            value = Decimal(raw.replace("_", ""))
        elif kind == "ident":
            upper = raw.upper()
            if upper in {"AND", "OR", "NOT", "IN"}:
                kind, value = "kw", upper
            elif raw.lower() in {"true", "false"}:
                kind, value = "bool", raw.lower() == "true"
            else:
                value = raw
        else:
            value = raw
        toks.append(_Tok(kind, value, start))
        pos = m.end()
    return toks


# ------------------------------------------------------------------------------------------ AST


@dataclass(frozen=True)
class _Cond:
    field: str
    op: str  # = != > >= < <= ~ in notin
    values: tuple[Any, ...]

    def eval(self, ctx: Mapping[str, Any]) -> bool:
        actual = _lookup(ctx, self.field)
        if actual is None:
            return False
        items = list(actual) if isinstance(actual, list | tuple | set | frozenset) else [actual]
        if not items:
            return False
        if self.op in {"!=", "notin"}:
            return not any(_eq(self.field, a, v) for a in items for v in self.values)
        if self.op in {"=", "in"}:
            return any(_eq(self.field, a, v) for a in items for v in self.values)
        if self.op == "~":
            needle = normalize_tr(str(self.values[0]))
            return any(needle in normalize_tr(str(a)) for a in items)
        return any(_order(self.field, self.op, a, self.values[0]) for a in items)


@dataclass(frozen=True)
class _Not:
    inner: Any

    def eval(self, ctx: Mapping[str, Any]) -> bool:
        return not self.inner.eval(ctx)


@dataclass(frozen=True)
class _BoolOp:
    op: str  # AND | OR
    parts: tuple[Any, ...]

    def eval(self, ctx: Mapping[str, Any]) -> bool:
        if self.op == "AND":
            return all(p.eval(ctx) for p in self.parts)
        return any(p.eval(ctx) for p in self.parts)


def _lookup(ctx: Mapping[str, Any], field: str) -> Any:
    if field.startswith("payload."):
        cur: Any = ctx.get("payload")
        for part in field.split(".")[1:]:
            if not isinstance(cur, Mapping):
                return None
            cur = cur.get(part)
        return cur
    return ctx.get(field)


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            from papi.core.text import parse_tr_number

            return parse_tr_number(value)
        except (ValueError, InvalidOperation):
            return None
    return None


def _rank(field: str, value: Any) -> int | None:
    if field == "band":
        return BAND_RANK.get(str(value).lower())
    if field == "priority":
        return PRIORITY_RANK.get(str(value).upper())
    return None


def _eq(field: str, actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, Decimal):
        a = _num(actual)
        return a is not None and a == expected
    exp = str(expected)
    if exp.endswith(".*"):
        return normalize_tr(str(actual)).startswith(normalize_tr(exp[:-2]))
    if isinstance(actual, int | float | Decimal):
        e = _num(exp)
        return e is not None and _num(actual) == e
    return normalize_tr(str(actual)) == normalize_tr(exp)


def _order(field: str, op: str, actual: Any, expected: Any) -> bool:
    if field in {"band", "priority"}:
        a, e = _rank(field, actual), _rank(field, expected)
    else:
        a, e = _num(actual), _num(expected)  # type: ignore[assignment]
    if a is None or e is None:
        return False
    return {">": a > e, ">=": a >= e, "<": a < e, "<=": a <= e}[op]


# ------------------------------------------------------------------------------------------ parser


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = _lex(text)
        self.i = 0

    def peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise RuleSyntaxError("ifade beklenmedik şekilde bitti")
        self.i += 1
        return tok

    def accept(self, kind: str, value: Any = None) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == kind and (value is None or tok.value == value):
            self.i += 1
            return True
        return False

    def expect(self, kind: str, value: Any = None) -> _Tok:
        tok = self.take()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise RuleSyntaxError(f"{value or kind} bekleniyordu, {tok.value!r} bulundu (konum {tok.pos})")
        return tok

    def parse(self) -> Any:
        if not self.toks:
            raise RuleSyntaxError("boş kural")
        node = self.or_()
        if self.peek() is not None:
            tok = self.peek()
            assert tok is not None
            raise RuleSyntaxError(f"fazladan {tok.value!r} (konum {tok.pos})")
        return node

    def or_(self) -> Any:
        parts = [self.and_()]
        while self.accept("kw", "OR"):
            parts.append(self.and_())
        return parts[0] if len(parts) == 1 else _BoolOp("OR", tuple(parts))

    def and_(self) -> Any:
        parts = [self.unary()]
        while self.accept("kw", "AND"):
            parts.append(self.unary())
        return parts[0] if len(parts) == 1 else _BoolOp("AND", tuple(parts))

    def unary(self) -> Any:
        if self.accept("kw", "NOT"):
            return _Not(self.unary())
        if self.accept("punct", "("):
            node = self.or_()
            self.expect("punct", ")")
            return node
        return self.cond()

    def value(self) -> Any:
        tok = self.take()
        if tok.kind not in {"str", "num", "bool"}:
            raise RuleSyntaxError(f"değer bekleniyordu, {tok.value!r} bulundu (konum {tok.pos})")
        return tok.value

    def cond(self) -> _Cond:
        tok = self.expect("ident")
        field = tok.value
        if field not in FIELDS and not (field.startswith("payload.") and len(field) > len("payload.")):
            raise RuleSyntaxError(f"bilinmeyen alan {field!r} (konum {tok.pos})")
        if self.accept("kw", "NOT"):
            self.expect("kw", "IN")
            return self._check(_Cond(field, "notin", self.value_list()), tok)
        if self.accept("kw", "IN"):
            return self._check(_Cond(field, "in", self.value_list()), tok)
        op_tok = self.expect("op")
        return self._check(_Cond(field, op_tok.value, (self.value(),)), tok)

    def value_list(self) -> tuple[Any, ...]:
        self.expect("punct", "(")
        values = [self.value()]
        while self.accept("punct", ","):
            values.append(self.value())
        self.expect("punct", ")")
        return tuple(values)

    @staticmethod
    def _check(cond: _Cond, tok: _Tok) -> _Cond:
        f, op, values = cond.field, cond.op, cond.values
        where = f"(konum {tok.pos})"
        if f == "text" and op != "~":
            raise RuleSyntaxError(f"text yalnız ~ ile kullanılır {where}")
        if op == "~" and not all(isinstance(v, str) for v in values):
            raise RuleSyntaxError(f"~ metin değer ister {where}")
        if f in BOOL_FIELDS and (op not in {"=", "!="} or not all(isinstance(v, bool) for v in values)):
            raise RuleSyntaxError(f"{f} yalnız = true/false ile kullanılır {where}")
        if f == "materiality" and not all(isinstance(v, Decimal) for v in values):
            raise RuleSyntaxError(f"materiality sayı ister {where}")
        if op in ORDER_OPS:
            if f in LIST_FIELDS | STRING_FIELDS or f == "text":
                raise RuleSyntaxError(f"{f} için {op} kullanılamaz {where}")
            if f in {"band", "priority"} and _rank(f, values[0]) is None:
                raise RuleSyntaxError(f"geçersiz {f} değeri {values[0]!r} {where}")
            if f.startswith("payload.") and not isinstance(values[0], Decimal):
                raise RuleSyntaxError(f"{op} sayı ister {where}")
        if f == "band" and op in {"=", "!=", "in", "notin"}:
            for v in values:
                if _rank("band", v) is None:
                    raise RuleSyntaxError(f"geçersiz band {v!r} {where}")
        if f == "priority" and op in {"=", "!=", "in", "notin"}:
            for v in values:
                if _rank("priority", v) is None:
                    raise RuleSyntaxError(f"geçersiz priority {v!r} {where}")
        return cond


@dataclass(frozen=True)
class Rule:
    text: str
    _root: Any

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        """Evaluate against a context dict (see :func:`papi.notify.context.event_context`)."""
        return bool(self._root.eval(ctx))

    @property
    def fields(self) -> set[str]:
        out: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, _Cond):
                out.add(node.field)
            elif isinstance(node, _Not):
                walk(node.inner)
            elif isinstance(node, _BoolOp):
                for p in node.parts:
                    walk(p)

        walk(self._root)
        return out


def parse(text: str) -> Rule:
    """Parse a rule filter; raises :class:`RuleSyntaxError` (Turkish message) on invalid input."""
    return Rule(text, _Parser(text).parse())
