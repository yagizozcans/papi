# packs/tr — Türkiye jurisdiction pack

Everything Turkey-specific that the market-agnostic core must not hard-code
(PRD [06 §1](../../docs/prd/06-data-model.md), [09 §3](../../docs/prd/09-platform-infrastructure.md)).
Located at runtime via `get_settings().pack_dir` (`packs.dir` / `packs.jurisdiction`, default
`<repo>/packs/tr`; bundled into the wheel as `papi/_resources/packs/tr`).

| File | Owner slice | Content |
|---|---|---|
| `taxonomy.yaml` | triage | Event codes (`family.sub.leaf`), `taxonomy_version`, per-code family prior for rules materiality, payload schema name (`contracts/schemas/event.<code>.v<N>.json`) |
| `kap_subject_map.yaml` | triage | KAP `subject` (+ optional form template / `source_class`/`source_type`) → candidate event code(s); Turkish-normalised matching (`papi.core.text.normalize_tr`); unmapped → `other.unclassified` |
| `triage_payload.yaml` | triage | KAP form-field / table label → event payload field mappings for deterministic payload extraction |
| `kap_subject_frequencies.csv` | triage | Harvested KAP `subject` × `disclosureClass` volume table used to build and check the subject map |
| `kap/templates.yaml`, `kap/schema_keys.yaml` | sources.kap | Known KAP form template ids, and recorded list/detail JSON key sets for schema-drift detection |
| `legal/obligations.yaml` | kb | Drafted obligations per event code (citations, actor, deadline rule, required fields, downstream steps); `approved: false` until approved via `papi kb approve-obligation` |
| `legal/manifest.yaml` | sources.legal / kb | Legal corpus manifest: regulation id, kind, official code (e.g. `6362`, `II-15.1`), title, authority, source URL, `refresh` cadence, citation-id prefix (`tr:spk:II-15.1`) |
| `calendar/` (e.g. `calendar/xist_<year>.yaml`) | sources.prices / run | Borsa İstanbul trading calendar: holidays, half days, session open/close (Europe/Istanbul). Loaded into `trading_calendar` (`calendar_id = XIST`); session times are never hard-coded |

Conventions: YAML, UTF-8, Turkish text verbatim (no ASCII folding in source files — fold at load time).
Each file carries a top-level `version:`; changes that rename codes are breaking and bump the version.
