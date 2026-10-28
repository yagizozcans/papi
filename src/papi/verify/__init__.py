"""Deterministic verifier for analyst output. PRD: docs/prd/04-agents.md, docs/prd/11-evaluation-and-
quality.md.

Public API: :func:`verify` (``papi.verify.verifier``), :class:`VerifySources`, :class:`VerifyReport`;
number tracing in ``papi.verify.numbers``, the advice phrase list in ``papi.verify.phrases``.
"""

from papi.verify.phrases import find_advice
from papi.verify.verifier import ClaimCheck, TextCheck, VerifyReport, VerifySources, flatten, verify

__all__ = ["ClaimCheck", "TextCheck", "VerifyReport", "VerifySources", "find_advice", "flatten", "verify"]
