# 13 · Agent base system (skills, tools, binds)

Status: base, hard read-gate, dashboard page and `kap-analyst` v0 built and smoke-run (2026-11-03).
`strategy-analyst` + thesis format added 2026-11-06. Not yet wired into the event pipeline.

## Why
The pack-based analyst (04 §4) pre-assembles one trimmed context and makes one blind call. Agents now
choose their own tools from skill instructions and read the whole document. Every agent (KAP analyst now,
others later) runs on the same base; only skills, tools and prompt differ.

## Layout
```
agents/<agent>/agent.toml   model, max_turns, max_budget_usd, timeout_s, priority, task, prompt, skills
agents/<agent>/system.md    system prompt        agents/<agent>/output.schema.json  (optional)
agents/skills/<skill>/SKILL.md (+ references/ assets/ scripts/)
src/papi/agents/tools/<tool>.py   one public function = one tool (docstring = description)
src/papi/agents/{skills,registry,definition,mcp_server,runner,cli}.py
```

## Contracts
- **Binds live in skills.** `SKILL.md` frontmatter `allowed-tools` lists tool names; the agent's tool set
  is the union over its skills. Unknown tool or non-`read` effect fails at load (`papi agent list`).
  Claude Code's built-in `WebSearch` / `WebFetch` are bindable the same way (`definition.BUILTIN_TOOLS`);
  they run inside claude, so they are not in the transcript and the agent must cite URLs.
- **Prompt:** `agent.toml` `prompt` is the user message with `{ref}` = the run subject
  (`"Analyse KAP disclosure {ref}."`, `"Review investment thesis {ref}."`).
- **Tools:** typed kwargs → schema; return a dict; failures are `{"success": false, "error"}` (guard
  converts exceptions); results over `MAX_RESULT_CHARS` (default 20 000, per-module override) are refused,
  so tools paginate. `EFFECT = "read"` default; write tools are not bindable yet.
- **Runtime:** `claude -p --tools "<bound built-ins>" --strict-mcp-config --mcp-config <stdio server>
  --allowedTools mcp__papi__*,<built-ins>` through the governed `llm.call` (admission, ledger, pause unchanged).
  The server serves exactly the agent's tools + `use_skill` / `read_skill_file`.
- **Linear loop:** the model picks each next tool; bounds are `max_turns`, `--max-budget-usd` and the
  governor caps. No repair retry (a loop is not cheap).
- **Cache:** system prompt + skill index + tool results form a stable growing prefix; later turns read it
  from the prompt cache (`usage.cache_read_input_tokens` in the run summary).
- **Run records** under `$PAPI_HOME/agent-runs/<agent>-<run>.*`: `.jsonl` transcript (every tool call: args,
  result, ms; the verifier's source log), `.meta.json` (status, cost, turns, cache tokens, answer), `.state.json`
  (per-run tool state, survives a resume).
- **Hard gate:** a tool result may carry `blocking` (null or a message). After the agent answers, the newest
  result with that key decides; non-null → the run is resumed **once** in the same Claude session
  (`--resume`, session persistence on for agent runs, session file deleted afterwards) with the message. Still
  blocking → status `incomplete`. The cost budget is per run: the resume gets what is left (skipped below $0.05).
  `kap_get_disclosure` / `kap_read_document` set `blocking` while any document is unread.
- `agent_version` = hash of prompt, schema, model, skill files and bound tool docs.

## CLI
`papi agent list | tools | run <agent> <ref> [--run-id]`; dashboard page `/agents` (start a run, list runs,
`/agents/runs/<id>` polls a fragment every 2 s while the run is active: tool calls appear live, then the answer).

## kap-analyst tools
`kap_get_disclosure`, `kap_read_document` (paged, `[S<n>]` span markers as in the context pack,
per-run reading status), `kap_event_schema`, `kap_issuer_context`, `portfolio_position`, `kb_search`.

## strategy-analyst (thesis review)
The user writes a thesis in a fixed template (`papi thesis new <TICKER>` → `$PAPI_HOME/theses/<TICKER>-<date>.md`;
sections and aliases in `papi.thesis.document.SECTIONS`). `papi thesis check <name>` parses it deterministically:
*errors* (bad front matter, unknown/repeated heading, stray text) stop a review; *gaps* (empty required
section, trigger without window/probability, kill signal without threshold) are reported and reviewed.
Every sentence/bullet gets a `[T<n>]` marker in template order. The agent (`papi agent run strategy-analyst
<name>`, skill `thesis-review`) tests the claims against real data and returns verdicts, what the price
assumes, edge (BAIT), triggers with calibrated probabilities, bull/base/bear scenarios (sum 1), kill-signal
status and a pre-mortem (`agents/strategy-analyst/output.schema.json`); the run page renders it.
Tools: `thesis_read`; `market_history` (İş Yatırım stock/index series incl. USD price and market cap, Yahoo
macro series: BRENT, USDTRY, US10Y …; fetched live, not stored); `company_financials` (İş Yatırım `MaliTablo`,
industrial layout only; YTD-vs-prior-year first, Q1–Q3 single quarters, full year as reported, balance,
valuation); `kap_search` (line search over the issuer's ingested disclosures incl. attachments, `[S<n>]`
markers); `portfolio_position`; `WebSearch`, `WebFetch`.

**Upload** (`/theses` page, `papi thesis import`; `papi.thesis.extract` + `papi.thesis.importer`): files go to
`$PAPI_HOME/theses/sources/<name>/files/`; a detached import process extracts text page by page (PDF text
layer via pdfplumber; pages with < 25 chars, photos and HEIC are transcribed by a governed `claude -p` call,
task `ocr`, model opus, built-in `Read` only, image written into the sandbox and deleted after; DOCX split
at Word page breaks), writes `pages.json` + `status.json`, builds `<name>.md` deterministically
(`document.heading_in_line`: section-name lines open sections, repeated sections merge, text before the
first heading becomes a comment + warning, missing sections are added empty), checks it and, if asked
and error-free, runs `strategy-analyst`. The page polls while active, shows each page's text with
uncertain words, and has an editor (`POST /theses/<name>/save`) and a run button.

## Open
- Persist output (analysis/claim/citation rows) and run the verifier over the transcript's span log
  (today `analyst.pipeline` does this for the pack); then cut `analyst.context` over and delete it.
- Record cache-read tokens in `llm_usage_ledger`.
- Eval-harness comparison against the pack analyst (11).
