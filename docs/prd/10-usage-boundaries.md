# 10 — Usage Boundaries (personal use)

> Parent: [PRD.md](../../PRD.md)
>
> papi is a personal, single-user, local tool built on public information. No licences are acquired. This document lists the rules that keep it inside that boundary, and what would have to change if it ever left it. It is not legal advice.

## 1. Scope rules

| ID | Rule |
|---|---|
| B-1 | **Single user.** papi serves only me. No accounts, no sharing of the dashboard or API with other people. |
| B-2 | **Public information only.** No non-public, paid or access-restricted data; no logins to third-party sites. |
| B-3 | **No redistribution.** KAP content, price data, and papi's analyses/forecasts are not published, posted, resold or sent to others (no public Telegram channel, no social posting, no website). |
| B-4 | **Decision support, not autopilot.** No broker integration, no order placement. |

Why B-3 also matters beyond data terms: publishing analyses or scenarios about specific stocks to others can fall under SPK rules on investment advice/recommendations (SPKn Art. 109; III-37.1). SPK's own investor guidance says a "YTD" disclaimer does not by itself change that. Keeping papi private avoids the question entirely.

## 2. Source etiquette

| ID | Rule |
|---|---|
| B-10 | **KAP**: KAP states that high-volume requests must use its licensed API rather than the website. papi therefore stays low-volume: one connection, conservative cadence ([02 §3.1](02-ingestion-pipeline.md#31-polling-and-catch-up)), backoff, honest `User-Agent`, rate-limited backfill (night windows). |
| B-11 | **No evasion**: no proxy/IP rotation, no CAPTCHA solving, no bypassing blocks. If a source blocks or throttles papi, papi backs off, alerts me, and waits. |
| B-12 | **Price data** (İş Yatırım pages, Yahoo Finance): personal use only, daily data, cached locally so each day is fetched once. The `isyatirimhisse` author explicitly scopes that library to personal use. |
| B-13 | **Legislation** (SPK, KAP, mevzuat.gov.tr, Borsa İstanbul, Resmî Gazete): fetched once and then on change only. |
| B-14 | Each source's registry row records its rate limit and terms note ([01 §4](01-data-sources.md#4-source-registry-requirements)). |

## 3. Claude subscription use

| ID | Rule |
|---|---|
| B-20 | Use the documented Claude Code CLI non-interactive mode (`claude -p`) with my own logged-in subscription ([04 §5](04-agents.md#5-claude-code-cli-backend)). |
| B-21 | My account is used only by me and my own tool; no proxying Claude access for others. |
| B-22 | Respect limits: the usage governor caps papi's share and pauses on limit errors ([04 §6](04-agents.md#6-usage-governor)). |
| B-23 | Make sure `ANTHROPIC_API_KEY` is not set in papi's environment, so there are no surprise API charges. Decline usage-credit prompts unless I decide otherwise. |
| B-24 | Pipeline prompts contain public data. My portfolio numbers go into prompts only for P1 explain calls and my own Telegram chat questions. That is my own data sent to my own Claude account; acceptable to me, and configurable off (`llm.include_position = false`). |
| B-25 | The Telegram bot answers only my user id in a private chat; it is never added to groups or shared ([08 §4.1](08-local-api-mcp-and-notifications.md#41-transport--security)). |

## 4. Personal data

- KVKK Art. 28(1)(a) excludes processing by a natural person solely for personal/household activities, provided the data isn't given to third parties and security is maintained. papi is in that scope.
- Some disclosures contain names (e.g. insiders in Pay Alım Satım Bildirimi). They are stored locally only and never redistributed (B-3).
- Secrets in the Keychain; data in `~/.papi/` (FileVault-encrypted disk assumed).

## 5. If papi ever goes beyond personal use

Kept as a checklist so today's design doesn't close this door. Each item is a known change, not a rewrite.

| Area | Required change |
|---|---|
| KAP data | KAP Veri Yayın Servisi subscription (Borsa İstanbul data distribution agreement, IP whitelisting, API key); implement `KapOfficialApiClient` ([01 §3.1](01-data-sources.md#31-kap-kamuyu-aydınlatma-platformu)) |
| Prices | Licensed data via a BIST-authorised vendor; display tiers (real-time/delayed/EOD) enforced in UI/API |
| SPK | Written legal opinion on positioning; likely remove position-specific language for others; no buy/sell/target language; consider a licensed-partner route if recommendations are wanted |
| Market-abuse hygiene | Corrections policy, public corrections log, human review of high-materiality items, audit trail (forecast hashes already exist) |
| KVKK / e-communications | Privacy notice, consent, VERBİS assessment, cross-border transfer review, İYS for marketing messages |
| LLM | Move from personal subscription to the Anthropic API (or another provider) under commercial terms via `LlmBackend` |
| Platform | Postgres, real event bus, auth/OIDC, multi-tenant `owner_id`, hosted deployment ([09 §8](09-platform-infrastructure.md#8-future-hosting-path-not-planned-kept-open)) |
| Product | Accounts, public API with scopes, licence-aware field filtering |

## 6. Acceptance criteria

- [ ] Poller defaults and backfill windows documented in config, with rates within B-10.
- [ ] Startup check fails loudly if `ANTHROPIC_API_KEY` is present in papi's environment.
- [ ] Web/API bound to loopback by default ([09 §7](09-platform-infrastructure.md#7-security)).
