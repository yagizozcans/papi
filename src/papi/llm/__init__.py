"""LlmBackend, Claude Code CLI backend, usage governor. PRD: docs/prd/04-agents.md §5-§6.

Modules: ``backend`` (``get_backend``, ``ClaudeCodeCliBackend``, ``check_environment``), ``governor``
(``admit``, ``record_usage``, ``pause``, ``resume``, ``status``), ``call`` (``call``), ``calibration``.
"""

from papi.llm.types import LlmResult

__all__ = ["LlmResult"]
