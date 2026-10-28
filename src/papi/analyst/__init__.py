"""Claude analyst pipeline: context packs, analyst/checker calls, claims and citations.

PRD: docs/prd/04-agents.md §2, §4.1, §4.3, §8; invariants docs/prd/06-data-model.md §5.2.

- ``papi.analyst.context.build_context(conn, event_id) -> ContextPack``
- ``papi.analyst.pipeline.analyse_event(conn, event_id, ...) -> Outcome`` (handler ``analyst.analyse``)
- ``papi.analyst.checker.run_for_call(conn, analysis_ids)`` (job kind ``analyst:check``)
- prompts in ``papi/analyst/prompts/``; schemas ``contracts/schemas/analyst.v1.json``, ``checker.v1.json``
"""
