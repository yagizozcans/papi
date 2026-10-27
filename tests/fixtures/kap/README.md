# KAP fixtures (recorded live)

Recorded from the public kap.org.tr endpoints on 2026-10-28 (00:56–02:05 Istanbul): 40 requests in total,
made one at a time with ≥ 1.2 s between them and the papi User-Agent. The response bodies are stored
byte-for-byte. Files over about 1 MB are gzip-compressed (`*.json.gz`). Tests serve these files through
respx, so the tests make no network calls.

## Endpoints

| File | Endpoint | Notes |
|---|---|---|
| `list_2026-10-27_2026-10-28.json.gz` | `POST /tr/api/disclosure/members/byCriteria` with `{"fromDate":"2026-10-27","toDate":"2026-10-28","mkkMemberOidList":[],"subjectList":[]}` | 679 items, newest first. Indices run from 1687719 to 1688616; the 219 missing integers are mostly fund disclosures. A 2026-10-01…26 window returned exactly 2000 rows (the cap) — the newest 2000. |
| `companies_IGS.json.gz` | `GET /tr/api/company/items/IGS/A` | 756 listed companies. `stockCode` may hold several codes, e.g. `"ISATR, ISBTR, ISCTR, ISKUR, TIB"` or `"AKM, AKMEN"`. There is no ISIN field. |
| `detail_<index>.json[.gz]` | `GET /tr/api/notification/attachment-detail/<index>` | `[{disclosure:{disclosureBasic, disclosureDetail}, disclosureBody:[html…], attachments:[{objId,fileName,fileExtension}]}]` |
| `detail_1688553_fund.json` | the same detail endpoint | An index **missing from the list**: it is a fund disclosure (`disclosureType` `FON`, `fundType` `SYF`), so a gap probe skips it. |
| `detail_1689500_nonexistent_http500.json` | the same detail endpoint, for an index not yet published | Returns HTTP **500** with body `{"success":false,"errorMessage":"HTTP 500 - "}`. papi treats this as not found. |
| `file_4028328ca16c7de401a17de5e8772c7f.bin` | `GET /tr/api/file/download/<objId>` | The PDF attachment of 1688272 (MHRGY DKB pay alım bildirimi), wrapped in a **Java-serialised `byte[]`** (`AC ED 00 05 75 72 00 02 5B 42 …`). |
| `file_4028328da16c7fe501a17db833540bc8.bin` | the same download endpoint | The **English** PDF attachment "Dividend Distribution Proposal.pdf" of 1688363 (FROTO). It is Java-wrapped too. |
| `bildirimpdf_1688616.pdf` | `GET /tr/api/BildirimPdf/1688616` | The PDF that KAP renders for a disclosure. It is a plain PDF, not wrapped. |

## Disclosure details

| File | Index | Class (detail) | Subject | Filer / related | Template (outer table class) | Att. | isChanged |
|---|---|---|---|---|---|---|---|
| `detail_1688616.json` | 1688616 | ODA | **ÖDA genel** — Özel Durum Açıklaması (Genel), **with an English version** (content-en cells) | PASEU | tbl_oda-10000_Material-Event-Disclosure-General | 0 | |
| `detail_1688272.json` | 1688272 | DUY (list: DKB) | **Pay Alım Satım Bildirimi (DKB)** relayed by KAP. The PDF attachment holds the trade table. | → MHRGY | tbl_GENEL_DUYURU_GONDERIMI | 1 | |
| `detail_1688533.json` | 1688533 | ODA | Pay Alım Satım Bildirimi (ODA form, dimensional trade table, EN) | MTRYO → METRO | tbl_oda-10400_Shares-Transaction-Notification | 0 | |
| `detail_1688549.json.gz` | 1688549 | FR | **Finansal Rapor** (5 XBRL role tables, multi-period) | KORDS | tbl_general_role_210015 … 610000 | 1 | |
| `detail_1688379.json.gz` | 1688379 | FR | Finansal Rapor (insurance), **later corrected** | ANSGR | tbl_insurance_role_210004 … | 1 | DUZELTILEN |
| `detail_1688576.json.gz` | 1688576 | FR | **Correction (düzeltme)** of 1688379: `relatedDisclosureOid` = 1688379's `disclosureId`, `relatedDisclosureIndex` 1688379 | ANSGR | tbl_insurance_role_210004 … | 1 | DUZENLENEN |
| `detail_1688363.json` | 1688363 | ODA | **Kar payı dağıtımı** (proposal; cash table with share-group ISINs) | FROTO | tbl_KP_Report_ID | 2 (TR + EN PDF) | |
| `detail_1687750.json` | 1687750 | ODA | **Sermaye artırımı** (bedelli) | CEMAS | tbl_SA_Report_ID | 1 | |
| `detail_1688011.json` | 1688011 | ODA | **Yeni İş İlişkisi** (EN) | MEKAG | tbl_oda-12000_New-Business-Relation | 0 | |
| `detail_1688368.json` | 1688368 | ODA | **Genel Kurul** (olağanüstü GK daveti; two-column rows without bold labels) | FROTO | tbl_GK_Report_ID | 6 | |
| `detail_1688504.json` | 1688504 | ODA | Payların Geri Alınması (daily buyback). Its update flag is "Evet" and `relatedDisclosureOid` points to the buyback programme. Cells hold "SAHOL, TRASAHOL91Q5". | SAHOL | tbl_GAI_Report_ID | 0 | |
| `detail_1688545.json` | 1688545 | ODA | Payların Geri Alınması, **correction** (correction flag "Evet") of a disclosure outside the window | TATEN | tbl_GAI_Report_ID | 0 | DUZENLENEN |
| `detail_1688254.json` | 1688254 | DUY (list: DKB) | Pay Bazında Devre Kesici Bildirimi (the most frequent subject) | → PSGYO | tbl_oda-23900_Notification-About-Suspension-of-BIST-Stock | 0 | |
| `detail_1688591.json` | 1688591 | DUY | SPK Tedbir Kararı (a free-text block with **embedded HTML tables**) | → MANAS, PASEU, SELEC | tbl_oda-24100_Capital-Market-Board-Administrative-Mesasures | 0 | |
| `detail_1688605.json` | 1688605 | DG | Şirket Genel Bilgi Formu (board table) | MHRGY | tbl_oda-21900_General-Info-Form | 0 | |
| `detail_1688613.json` | 1688613 | FR | Sorumluluk Beyanı | KONKA | tbl_oda-26500_Representation-Letter | 0 | |
| `detail_1688615.json` | 1688615 | FR | Faaliyet Raporu | KONKA | tbl_oda-34000_Unconsolidated-Operating-Review | 1 | |
| `detail_1688597.json` | 1688597 | ODA | Esas Sözleşme Tadili | MARMR | tbl_oda-21300_Change-In-Articles-Of-Association | 0 | |
| `detail_1688581.json` | 1688581 | DG | Genel Açıklama (filer ABO, related codes are 3-letter member codes) | ABO → ABJ, GZNMI, … | tbl_oda-36000_General-Notification | 0 | |
| `detail_1688568.json` | 1688568 | DUY | SPK Bülteni | – | tbl_oda-12800_CMB-Bulletin | 0 | |
| `detail_1688480.json` | 1688480 | ODA | Pay Dışında SPA İşlemleri (Faiz İçeren) | OYA, OYYAT | tbl_SPAF_Report_ID | 0 | |
| `detail_1688336.json` | 1688336 | ODA | Kredi Derecelendirmesi | KAYSE | tbl_oda-29000_Credit-Rating | 1 | |
| `detail_1688292.json` | 1688292 | DG | Finansal Takvim | INVEO | tbl_oda-54000_Financial-Calender | 0 | |
| `detail_1688235.json` | 1688235 | ODA | İhale Süreci / Sonucu | MEDTR | tbl_oda-32000_Winning-A-Tender | 0 | |
| `detail_1688153.json` | 1688153 | DUY | Pay Mali Hak Kullanım İşlemi – Nakit Ödeme | → MCARD | tbl_MKK_CSV_Report_ID | 0 | |
| `detail_1688079.json` | 1688079 | ODA | İhraç Tavanına İlişkin Bildirim | EKTVK → EKT | tbl_IT_Report_ID | 0 | |
| `detail_1688065.json` | 1688065 | DUY | Temerrüt İşlemi | → ECOGR | tbl_oda-14700_Default-Transaction | 0 | |
| `detail_1687728.json` | 1687728 | DUY | KAP Duyurusu | many member codes | tbl_oda-24400_Public-Disclosure-Platform-Announcement | 1 | |

## Observations from the recording

- **Class names differ between list and detail.** For relays by KAP, the list says `disclosureClass` `DKB` while the detail says `DUY` (`memberType` `DDK`). papi keeps the list value and stores the detail value in `extra.detail_class`.
- **Revisions.** On a correction, `isChanged` / `modifyStatus` is `DUZENLENEN` and `relatedDisclosureOid` holds the original's `disclosureId`. The corrected original is re-listed with `DUZELTILEN`. A `relatedDisclosureOid` without `isChanged` marks a follow-up ("Güncelleme" flag), which papi treats as `update`.
- No **blocked** disclosure (`isBlocked: true`) and no XLS/XLSX attachment appeared in this window. Tests derive the blocked case from 1688616 and build XLSX/DOCX files synthetically.
