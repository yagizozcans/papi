# 01 — Data Sources

> Parent: [PRD.md](../../PRD.md) · Contracts: [06](06-data-model.md) · Boundaries: [10](10-usage-boundaries.md)

## 1. Purpose

Catalogue the public sources papi consumes, how each is accessed, and in what order adapters get built. Every source is a row in the `source` registry and an **adapter** behind one interface (§5). Adding a source, or a whole new country, never touches the core.

**Rule:** public information only, fetched at personal volume, never redistributed ([10](10-usage-boundaries.md)).

## 2. Build order

| Order | Source | Milestone |
|---|---|---|
| 1 | KAP disclosures + company list | M1 |
| 2 | Public legislation (KB corpus) | M2 |
| 3 | Price history + index levels | M3 |
| 4 | Financial statements (KAP XBRL; tabular fallback) | M3 |
| 5 | SPK bülten, Borsa İstanbul notices, Resmî Gazete, TCMB, TÜİK | M5 |
| 6 | Rekabet Kurumu, sector regulators, EPİAŞ, etc. | Later |

## 3. Catalogue — Turkey pack (`TR`)

### 3.1 KAP (Kamuyu Aydınlatma Platformu)

| Attribute | Detail |
|---|---|
| Operator | MKK (Merkezi Kayıt Kuruluşu A.Ş.) |
| Content | All disclosures by listed companies, funds, intermediaries, auditors, rating agencies; SPK / Borsa İstanbul notices relayed via KAP |
| KAP UI categories | Özel Durum Açıklaması (ÖDA), Finansal Rapor (FR), Düzenleyici Kurum Bildirimleri, Diğer (DG) |
| Observed raw fields | `disclosureIndex` (monotonic int), `disclosureId` (hex uid), `disclosureClass` (`ODA`, `FR`, `DG`, `DKB`, …), `disclosureType`, `disclosureCategory`, `subject`, `summary`, `relatedStocks`, `stockCodes`, `publishDate`, `isLate`, `modifyStatus`, `attachmentCount`, `relatedDisclosureOid`, `isChanged`, `isBlocked` |
| Formats | HTML form body (per-template tables, e.g. `tbl_oda-10400_…`), PDF/XLS/DOC attachments, XBRL statements, TR and sometimes EN |
| Volume | ~440 index increments per calendar day (Dec 2023 → Oct 2026 estimate, upper bound), with large bursts near reporting deadlines |

**Access modes (behind one `KapClient` interface)**

| Client | Description | Status |
|---|---|---|
| `KapPublicWebClient` | JSON endpoints used by the kap.org.tr front-end (community-documented, unofficial): disclosure list by criteria (POST; responses capped at ~2000 rows), disclosure detail by index, attachment download (file bytes wrapped in a Java-serialised `byte[]`), KAP-rendered PDF per disclosure, company lists by member type | **Default.** Personal, low-volume use. May change without notice; drift detection is required ([02](02-ingestion-pipeline.md)). |
| `KapOfficialApiClient` | KAP Veri Yayın Servisi REST API: `disclosures`, `disclosureDetail`, `downloadAttachment`, `lastDisclosureIndex`, `members`, `memberDetail`, `funds`, `fundDetail`, `memberSecurities`, `blockedDisclosures`, `caEventStatus`. Requires a Borsa İstanbul data distribution agreement, IP whitelisting and an API key. | **Not built** (no licence). Because the pipeline depends only on `KapClient`, adding it later is a new class plus config. |

### 3.2 Public legislation (knowledge-base corpus)

Fetched and versioned by papi. See [03](03-regulatory-knowledge-base.md) for the document list and manifest.

| Source | What it gives |
|---|---|
| `kap.org.tr` mevzuat / "about" content files | PDFs of key SPK communiqués and guidelines relevant to disclosure (e.g. II-15.1 Özel Durumlar Tebliği, Özel Durumlar Rehberi) and KAP rules |
| `spk.gov.tr` | Communiqués, guidelines, ilke kararları, announcements of amendments |
| `mevzuat.gov.tr` | Consolidated laws (6362 SPKn, 6102 TTK, etc.) and regulations |
| `borsaistanbul.com` | Exchange rules and procedures, market-measure (tedbir) procedures, index ground rules |
| `resmigazete.gov.tr` | Publication of amendments; effective dates (from M5 it is also an event source) |

### 3.3 Price history, index levels, financial statements

No paid licence, so prices come from public pages for **personal use only**. Two sources are cross-checked.

| Adapter | Source | Notes |
|---|---|---|
| `isyatirim_prices` | İş Yatırım public historical price and financial-table pages (the endpoints the `isyatirimhisse` library wraps; its author states personal use only) | Primary for BIST daily OHLCV, index levels, and tabular financials |
| `yahoo_prices` | Yahoo Finance chart data for `<TICKER>.IS` symbols | Cross-check and fallback; adjusted series for BIST corporate actions must not be trusted blindly |
| `kap_xbrl` | XBRL financial statements from KAP FR disclosures | Authoritative financials; tabular fallback from İş Yatırım |

Cross-check rules: daily close differences above a threshold, or a missing day, open a data-quality issue. Adjusted prices are computed by papi from **raw closes + KAP corporate actions** ([05](05-simulation-engine.md), [12](12-portfolio.md)), not taken from either source.

Data is daily (end of day) for v1. Intraday is out of scope until there is a legitimate source.

### 3.4 Regulators and macro (M5)

| Source | Content | Why it matters |
|---|---|---|
| SPK Bülteni | Approvals (capital increases, IPOs, bond issues, tender offers), fines, trading bans | Approval is the real trigger of many events |
| Borsa İstanbul duyuruları | Market measures (brüt takas, kredili işlem yasağı, açığa satış yasağı, tek fiyat), index changes, segment moves, halts | Direct price/liquidity impact; many are also relayed via KAP, so they are de-duplicated |
| Resmî Gazete | Laws, regulations, presidential decisions (tax, incentives, tariffs) | KB amendments, macro/sector events |
| TCMB | Policy rate decisions, FX/reserve regulations, EVDS series | Macro shocks; FX for TRY↔USD views |
| TÜİK | CPI/PPI, GDP, etc. | Inflation context for real returns |
| Rekabet Kurumu (later) | Merger approvals, investigations | M&A completion and legal risk |

### 3.5 Future jurisdictions (kept possible, not planned)

US (SEC EDGAR) and EU/UK (OAMs, RNS/NSM) map onto the same adapter + pack model.

## 4. Source registry requirements

| ID | Requirement |
|---|---|
| SRC-1 | Registry row per source: `code`, jurisdiction, access mode, terms/robots notes, **rate limit**, cadence, contact/User-Agent string. |
| SRC-2 | Declared **identity key** and **ordering guarantee** (e.g. KAP `disclosureIndex` is monotonic, so gaps can be detected). |
| SRC-3 | Declared **revision semantics** (corrections, updates, takedowns). |
| SRC-4 | Freshness/health expectation used by the ops view (calendar-aware). |
| SRC-5 | Cross-source de-dup rules (e.g. a BIST measure on both KAP and borsaistanbul.com yields one `event` from two `disclosure`s). |

## 5. Adapter interface

```text
interface SourceAdapter {
  code: string                           // "kap"
  discover(cursor) -> [Ref], cursor      // what's new since cursor
  fetch(ref) -> [RawObject]              // bytes untouched
  parse(raw) -> [Document]               // pure: bytes + parser version → output
  normalize(docs, ctx) -> [Disclosure]   // pure: map to core model
  revisions(ref) -> [Ref]                // corrections / takedowns
  backfill(range) -> iterator[Ref]
  health() -> Status
}
```

Network I/O lives only in `discover`/`fetch`. `parse`/`normalize` are deterministic, so all history can be reprocessed.

## 6. Acceptance criteria

- [ ] `KapPublicWebClient` passes contract tests on recorded fixtures for every observed form template.
- [ ] Pipeline code imports only the `KapClient` interface, never `KapPublicWebClient` directly.
- [ ] Price adapters cross-check one year of closes for all holdings with < 0.1 % unexplained mismatch days.
- [ ] Adding an M5 source touches only its adapter, its taxonomy mapping and registry config.
