# contracts/

Source of truth for machine-readable contracts shared by papi's slices, the Claude CLI calls
(`claude --json-schema`) and future clients (PRD [09 §2](../docs/prd/09-platform-infrastructure.md),
[06](../docs/prd/06-data-model.md)).

## Layout

| Path | Holds |
|---|---|
| `schemas/<name>.v<N>.json` | JSON Schema (draft 2020-12) per contract, one file per **major** version |
| `events.md` | Internal event types (CloudEvents `type`) and their `data` fields |

Naming of `<name>`:

| Kind | Name pattern | Example |
|---|---|---|
| Event payload (taxonomy code) | `event.<code>` | `schemas/event.business.contract.award.v1.json` (`$id`: `event.business.contract.award/1`) |
| Claude call I/O | `<agent>.output` / `<agent>.input` | `schemas/analyst.output.v1.json`, `schemas/chat.output.v1.json` |
| Event `data` (if formalised) | `bus.<event type without .vN>` | `schemas/bus.disclosure.normalized.v1.json` |

## Rules

- `$id` = `<name>/<N>`; every schema sets `"$schema": "https://json-schema.org/draft/2020-12/schema"`.
- Adding optional fields is non-breaking (same file). Renaming/removing/retyping a field is breaking →
  new file `v<N+1>`; keep the old one while anything still produces it.
- Unknown ≠ zero: absent facts are `null`; numeric fields are `["number","null"]`.
- Validate with `jsonschema` (already a dependency): `jsonschema.Draft202012Validator(schema).validate(obj)`.
- Pydantic models in code mirror these schemas; the schema file wins on conflict.
