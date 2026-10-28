"""Regulatory knowledge base (provisions, obligations, snapshots). PRD: docs/prd/03-regulatory-knowledge-
base.md. Public retrieval API (contract C8) re-exported from ``papi.kb.api``.
"""

from papi.kb.api import (
    ExpectedStep,
    Obligation,
    ProvisionHit,
    ProvisionVersion,
    current_snapshot,
    lookup,
    obligations,
    search,
    search_for_event,
    timeline,
    versions,
)

__all__ = [
    "ExpectedStep",
    "Obligation",
    "ProvisionHit",
    "ProvisionVersion",
    "current_snapshot",
    "lookup",
    "obligations",
    "search",
    "search_for_event",
    "timeline",
    "versions",
]
