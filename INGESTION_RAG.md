# Corpus → RAG: data contract & handoff

> ℹ️ **Ingestion itself is handled by a separate repo.** This document is the **data
> contract**: what `cb_corpus` exposes (schema, paths, filters, citation, caveats) so that
> any ingester can consume it. The code snippets are a **reference**, to be
> adapted to the ingestion repo's stack — not an imposed implementation. In MyOpenFund, the
> [vault](https://github.com/MyOpenFund/vault) loads the manifests into Postgres and
> [data-orchestrator](https://github.com/MyOpenFund/data-orchestrator) drives the RAG ingestion.

Everything starts from `data/raw/` (files) + the **per-bank manifests** `data/manifest/<bank>.jsonl`
(the index; one file per bank code). The goal on the RAG side:
**metadata filters** + **official-source citations**.

> Data inventory (bank × type × year): `python -m cb_corpus report`. Coverage ~99% vs the official catalogs.

---

## 1. The manifest is your index

`data/manifest/<bank>.jsonl` = 1 JSON line per document. **Don't iterate the disk, iterate the manifest**:
it carries all the metadata and points to the local file.

> 🗄️ **The queryable store is your responsibility (RAG side).** `cb_corpus` produces a **handoff**
> (deduplicated JSONL + raw files); it is up to this repo to ingest it into its **indexed/vector
> store** (SQLite, pgvector, etc.). The builder deliberately stays in JSONL (the queryable store is on the RAG repo side).
> `doc_id` is **date-independent** (hash of bank+type+url) → stable as a document `id`.

Fields useful for ingestion:

> **Field rename, 2026-11-19:** `pdf_url` is now **`document_url`** (the canonical document
> URL: PDF preferred, HTML accepted). Same value, same position in the row, same `doc_id`
> (it hashes the URL value, not the field name). Manifests published before the NAS
> migration still carry `pdf_url`; this repo's code reads only `document_url`, so bring an
> older snapshot to the current form first (`python -m cb_corpus migrate-contract --write`,
> below, renames `pdf_url` too).

> **Documents contract, 2026-11-20.** Every row follows the MyOpenFund `documents` contract
> ([vault-contract 1.0.0](https://github.com/MyOpenFund/vault/tree/master/contract), the
> rules the vault enforces at ingestion). Three changes, same `doc_id`s:
> - **`corpus: "central-bank"`**, the first key of every row (the vault's identity of a
>   document is (`corpus`, `doc_id`));
> - **`bank_code` is now `source_code`** (same value, same position after `corpus`);
> - **`local_path` and `html_path` are relative to the data root** (the directory holding
>   `manifest/`): `raw/ecb/C1/2023/<doc_id>.pdf`, not `data/raw/...`. Open
>   `<data root> / local_path` (in this repo the data root is `data/`).
>
> Undated rows no longer carry a `date_precision` (null) nor a `date_kind`. Manifests
> published before the NAS migration (commit `5fec83a`) still carry `bank_code`, no `corpus`
> and `data/raw/...` paths. This repo's code reads the contract form only (a row in an older
> form stops a command with an error naming the fix); to read an older snapshot, rewrite it
> once with `python -m cb_corpus migrate-contract --write` (README § Maintenance).
>
> The codes a row may use are declared in **`vocabulary.json`**, written at the data root
> next to `manifest/` (generated from the code: `source_codes` = the 63 banks of
> `cb_corpus.banks` with their names, `doc_types` = `cb_corpus.taxonomy` A1…G3 with labels
> and families, `provenances` = `cb_corpus.models.PROVENANCES`, no entity scheme) by every
> `discover` / `bis-sitemap` / `repec` run and by `migrate-contract --write`. It is not
> committed; to rebuild it in a clone:
> `python -c "from pathlib import Path; from cb_corpus.vocabulary import write_vocabulary; write_vocabulary(Path('data'))"`.
> To check a snapshot against the contract (dev requirements install `vault-contract`):
> `python -m vault_contract.check data/manifest --corpus central-bank --vocabulary data/vocabulary.json`
> (add `--no-transition` to judge it as the vault does since the transition cleanup:
> 0 refusals on the committed manifests).


| Field | RAG usage |
|---|---|
| `corpus` | always `central-bank` (the documents contract's corpus code) |
| `local_path` | **path of the file to read** (PDF, or HTML if rendering failed), relative to the data root (`raw/<bank>/<type>/<year>/<doc_id>.<ext>`) |
| `source_code` | the bank: **filter** + metadata (map to full name via `cb_corpus.banks` or `vocabulary.json`) |
| `doc_type` | **filter** + metadata (map to a readable label, cf. §5) |
| `year`, `date` | **temporal filter** / facet. `date` is always a full `YYYY-MM-DD` or `null`; read it with `date_precision` (§1a) |
| `title` | chunk title / display |
| `document_url` | **citation** (official source) |
| `language` | filter (all `en` today) |
| `date_precision` | how much of `date` is real: `day`, `month` (`date` = first day of the month), `year` (`date` = 1 January). Key absent = written before precision was recorded; `null` = undated row, or a producer that stated nothing. See §1a |
| `date_kind` | optional, policy core only: what `date` is — `meeting`, `publication` or `period` (§1a). Absent on every other row and on undated rows |
| `date_source` | where the date comes from: `bank_site`, `pdf_meta`, `wayback`, `repec` (working papers), `sitemap_lastmod` (a sitemap's last-modified day, not a publication date); `null` on some newer undated rows |
| `mime_type` | `application/pdf` or `text/html` (the HTML is the artifact when PDF rendering failed); rarely another official format |
| `html_path` | the kept raw HTML next to a rendered PDF, when there is one (relative to the data root, like `local_path`) |
| `provenance` | how the document was obtained: `bank_site`, `bis_index`, `repec_discovery`, `wayback` (recovered from a Wayback snapshot), `mirror` (recovered from a legitimate co-publication) |
| `alt_urls` | other URLs of the same document (dedup). May include stamped duplicate URLs (other addresses that served the same bytes, possibly dead now): not necessarily valid citations — cite `document_url` |
| `alt_source_urls` | optional, rare: other landing pages that pointed at the same bytes (dedup only) |
| `doc_id` | stable primary key (ideal as a document `id` in the store) |
| `sha256` | change detection / ingestion idempotence. The hash of the document **as the bank served it**: for a rendered page, the HTML's, not the PDF's |

---

## 1a. Dates: precision and kind

**One date field, always a full date.** `date` is `YYYY-MM-DD` or `null`, never a partial
string. When the source only names a month or a year, `date` holds the **first day of that
period** and `date_precision` says so:

| `date_precision` | `date` holds | A date filter should treat it as |
|---|---|---|
| `day` | the day itself | that day |
| `month` | the first day of the month (`2021-06-01` = "June 2021") | the whole month |
| `year` | 1 January (`2016-01-01` = "2016") | the whole year |
| key absent | a date written before precision was recorded: nothing verified | unknown (in practice a day, or an unlabelled first-of-period placeholder) |
| `null` | no date, or a date whose producer stated no precision | undated / unknown |

A date on the first of a month is **not** a placeholder by itself: `2018-08-01` with precision
`day` is a real meeting day. Only `date_precision` tells the two apart. A row whose `date` is
`null` always has a `null` precision.

**Policy core** (doc types A1 A2 A3 B1 E1 F1 of `ecb`, `us`, `gb`, `au`, `ca`, `jp`), after the
`policy-dates` backfill (README § Maintenance): every dated row carries its `date_precision`.
`day` there is a verified fact — the document's own URL or title names that very day, the
first of a month included. The only rows left without the key are those whose stored day the
evidence neither names nor contradicts (none in the manifests of 2026-11-18). Every dated row
also carries `date_kind`, except the rows listed under "No kind" below.

`date_kind` says what the date **is** — one date field does not mean the same thing everywhere:

| `date_kind` | Meaning |
|---|---|
| `meeting` | the day the policy meeting ended / the decision was taken. Minutes dated this way were **published weeks later**: do not use the date as "known on that day" |
| `publication` | the day (or month) the document was released |
| `period` | the issue the document belongs to ("the May 2020 Statement"): a month, not an event |

The kind is decided **row by row** by `cb_corpus/dates.py::policy_date_kind` (the same function
for new rows and for the backfill): the series' kind (`POLICY_DATE_KIND`), minus documented
exceptions that depend on the row's own date and precision (`POLICY_DATE_KIND_EXCEPTIONS`).
`POLICY_DATE_RULES` says where the date is read:

| Bank | Type | `date_kind` | Precision | Date read from |
|---|---|---|---|---|
| `us` | A2 statement, A3 minutes, B1 press conference | `meeting` | day | `YYYYMMDD` in the document URL |
| `us` | F1 projections | `meeting` — **see the warning below** for the `SEPcompilation` files | day | `YYYYMMDD` in the document URL |
| `us` | E1 Monetary Policy Report | `publication` | day | `YYYYMMDD` in the document URL |
| `ecb` | A1 decision, A2 statement, B1 press conference | `meeting` | day | `YYMMDD` in the file name |
| `ecb` | A3 account | `publication` | day | `YYMMDD` in the file name (released four weeks after the meeting) |
| `ecb` | E1 staff projections | `period` | month | `YYYYMM` in the file name |
| `gb` | A3 MPC minutes | `meeting` | day when the title names it ("held on 3 and 4 February 1999" → the 4th, the day the meeting ended), else month | title, then page URL, then file URL |
| `gb` | E1 Monetary Policy Report | `period` | month | title, then page URL, then file URL |
| `au` | A1 decision, dated from 2008-02-05 | `meeting` | day | listing link text (kept at the end of the title) |
| `au` | A1 decision, dated before 2008-02-05 | `publication` (until December 2007 the decision was announced at 9.30 am the morning **after** the board meeting; from the meeting of 5 February 2008, at 2.30 pm on the meeting day — `RBA_SAME_DAY_ANNOUNCEMENT_FROM`; also the decision of 19 March 2020, announced the day after the unscheduled meeting of the 18th — `RBA_ANNOUNCED_THE_DAY_AFTER`) | day | same |
| `au` | A3 board minutes | `meeting` | day | document URL (`YYYY-MM-DD` or `DDMMYYYY`) |
| `au` | E1 Statement on Monetary Policy | `period` | month | `/smp/<year>/<feb\|may\|aug\|nov>/` |
| `ca` | A3 summary of deliberations | `publication` | month | `/<YYYY>/<MM>/` of the page URL (the decision day in the slug is not used) |
| `ca` | E1 Monetary Policy Report, URL with a month only | `period` | month | `/mpr/mpr-YYYY-MM/` |
| `ca` | E1 Monetary Policy Report, URL with a day | `publication` (the day the report was released) | day | `/mpr/mpr-YYYY-MM-DD/` |
| `jp` | A3 minutes | `meeting` | day | `gYYMMDD.pdf` (last day of the meeting) |

> ⚠️ **Federal Reserve individual projections (`us` F1, `…/FOMC<YYYYMMDD>SEPcompilation.pdf`,
> 53 of the 76 F1 rows on 2026-11-18).** These files are dated at their FOMC meeting like the
> projection tables (`fomcprojtabl<YYYYMMDD>.pdf`), so their kind is `meeting` — but the Fed
> **releases them about five years after that meeting**. Never treat one as available on its
> date. Recognise them by the URL pattern, exposed as `cb_corpus.dates.FED_SEP_COMPILATION_URL`
> (`/FOMC\d{8}SEPcompilation\.pdf$`). The same caution applies, with a lag of weeks, to every
> `meeting`-dated set of minutes.

**No kind.** A dated policy row carries no `date_kind` when nothing true can be said:

- its date is a sitemap last-modified day (`date_source = "sitemap_lastmod"`): neither an event
  nor an issue;
- its URL is not a document of the series — the index pages under the Bank of Canada's
  `/publications/mpr/` (they also stay undated unless the sitemap gives a last-modified day);
- it is filed in the wrong series: Bank of England rows catalogued as minutes (`gb` A3) whose
  file or title shows an Inflation Report, a Monetary Policy Report or a press-conference
  transcript (`cb_corpus.dates.BOE_NOT_MINUTES`; 10 rows on 2026-11-18). They are dated like
  the page they were found on (a month) and `policy-dates` reports them as `misfiled`.

No row changes meaning: these rules record what each row's date has always been. A document
has one date; a second one (meeting **and** publication side by side) is not recorded yet.

---

## 2. Minimal ingestion pipeline

```python
import json
from pathlib import Path
import fitz  # PyMuPDF — fast and robust for PDF text extraction

from cb_corpus.banks import get_bank
from cb_corpus.taxonomy import by_code

DATA = Path("/path/to/cb_corpus/data")          # the data root: holds manifest/ and raw/
MANIFESTS = sorted((DATA / "manifest").glob("*.jsonl"))  # one file per bank

def iter_docs():
    for line in (l for m in MANIFESTS for l in m.open()):
        d = json.loads(line)
        # ingest only PDFs actually present
        lp = d.get("local_path")
        if not lp or not lp.endswith(".pdf"):
            continue
        p = DATA / lp                          # relative to the data root
        if p.exists():
            yield d, p

def extract_text(pdf_path: Path) -> str:
    with fitz.open(pdf_path) as doc:
        return "\n".join(page.get_text("text") for page in doc)

def doc_metadata(d: dict) -> dict:
    """Clean metadata, ready for filtering + citation."""
    return {
        "doc_id":   d["doc_id"],
        "source_code": d["source_code"],
        "bank_name": get_bank(d["source_code"]).name,
        "doc_type":  d["doc_type"],
        "doc_type_label": by_code(d["doc_type"]).label,  # ex. "Speech"
        "year":   d.get("year"),
        "date":   d.get("date"),
        "date_precision": d.get("date_precision"),   # day | month | year | None (§1a)
        "date_kind": d.get("date_kind"),             # meeting | publication | period | None
        "title":  d.get("title", ""),
        "source_url": d.get("document_url"),   # <- the official citation
        "language":   d.get("language", "en"),
    }
```

---

## 3. Chunking

Recommendations for this corpus (speeches + reports, often long):

- **Size**: ~800–1,200 tokens per chunk, **overlap** ~100–150 tokens.
- **Semantic splitting** first (paragraphs / line breaks), then group up to the
  target size — avoid cutting in the middle of a sentence.
- **Carry the document metadata onto each chunk** (same `doc_id`, `source_code`,
  `doc_type`, `year`, `source_url`, `title`) + a `chunk_index`. This is what enables
  filtering and citation at the chunk level.

```python
def chunk_text(text, target=1000, overlap=120):
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > target * 4:   # ~4 chars/token, approximation
            chunks.append(cur)
            cur = cur[-overlap * 4:] + "\n" + p
        else:
            cur = (cur + "\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks

def build_records():
    for d, path in iter_docs():
        meta = doc_metadata(d)
        try:
            text = extract_text(path)
        except Exception:
            continue                          # unreadable PDF -> skip
        for i, ch in enumerate(chunk_text(text)):
            yield {**meta, "chunk_index": i, "text": ch,
                   "id": f"{meta['doc_id']}:{i}"}
```

Wire `build_records()` into your vector store (Chroma, Qdrant, pgvector, FAISS…), setting
`source_code` / `doc_type` / `year` as **filterable metadata**.

---

## 4. Filtering at retrieval

Metadata filters make answers targeted and citable:

| Question | Filter |
|---|---|
| "What does the ECB say about inflation in 2023?" | `source_code = "ecb" AND year = 2023` |
| "Fed minutes" | `source_code = "us" AND doc_type = "A3"` |
| "Speeches by euro-area banks since 2020" | `source_code IN (…) AND doc_type = "C1" AND year >= 2020` |

---

## 5. Type labels (for display)

`doc_type` is a code; map it for the user (`cb_corpus.taxonomy.by_code(code).label`):

| Code | Label |
|---|---|
| A1 | Rate-decision press release |
| A2 | Monetary policy statement |
| A3 | Meeting minutes / accounts |
| B1 | Press-conference transcript / Q&A |
| C1 | Speech |
| C2 | Interview / op-ed / testimony |
| D1 / D2 | Working paper / occasional paper |
| D3 | Economic letter / research blog |
| E1 | Monetary policy / inflation report |
| E2 | Financial Stability Review |
| E3 | Annual / convergence report |
| E4 | Economic / quarterly bulletin |
| F1 | Staff economic projections |
| G2 | Statistical release / survey |

---

## 6. Citation

With every answer, return the **official source**: `title` + `bank_name` + `date` + `source_url`
(`document_url`). Write the date as precisely as `date_precision` allows ("June 2021" for a `month`,
"2016" for a `year`), never the stored first day. Example rendering:

> *"…"* — Bank of England, *Andrew Bailey: Monetary policy and the outlook*, 2023-05-18.
> Source: https://www.bis.org/review/r230518a.pdf

---

## 7. Caveats to know for weighting the RAG

- **Composition**: the corpus is dominated by **speeches (C1)**. It is a "central-banker
  voice" base; the quantified decisions (A1/A2) and research (D1/D2) are present. Weight retrieval according to the use case.
- **Language**: 100% English. Speeches from non-English-speaking banks are the **official EN
  versions**, not the originals.
- **HTML artifacts**: about 15 % of rows (6,040 of 39,452 in the 2026-09-03 manifest snapshot,
  mostly working-paper landing pages (us, ca, ch) and ECB HTML-only documents) have `mime_type = "text/html"` and an `.html`
  `local_path`: they are indexed documents, not junk. The §2 snippet keeps PDFs only (it uses a
  PDF extractor); ingest the HTML rows with an HTML text extractor if you want them. Files in
  `data/raw/` that no manifest row points to (and `.DS_Store`) are never reached when you iterate
  the manifests.
- **Working papers (D1/D2)**: dates come from the bank site where migrated (`ecb`, `jp`, `gb`,
  `de` mostly at day precision; `us` mostly month, since the Fed published no day before ~2017),
  otherwise from RePEc/IDEAS (usually month only, `date` = `YYYY-MM-01`) unless `wp-dates`
  recovered the day; `fr` and `se` RePEc-era rows are not migrated yet. Read `date_precision` /
  `date_source` before treating the day as exact (absent = not recorded); a small number of rows
  may be undated (null `year`).
- **Policy documents**: read `date_precision` and `date_kind` (§1a). Bank of England minutes
  and reports, RBA Statements, ECB staff projections and Bank of Canada reports are often known
  to the month only; Fed, RBA, BoJ and BoE minutes are dated at the **meeting**, not at their
  release, and the Fed's individual projections (`SEPcompilation`) come out about five years
  after the meeting they are dated at. A few Bank of England rows are placeholder pages
  captured before publication (title ending in "(to be published at 12pm)").
- **Other document types** still hold first-of-period placeholders without a label (rows written
  before precision was recorded, mostly working papers): outside the policy core, an absent
  `date_precision` on a `-01` or `-01-01` date is not a confirmed day.
- **Idempotence**: re-ingest based on `sha256` (unchanged = already indexed) to avoid
  recomputing everything on each corpus update. A date correction changes `date`, `year`,
  `date_precision`, `date_source` or `date_kind` only — never `doc_id`, `sha256` or
  `local_path`: update the metadata of the document you already hold, do not re-embed it.
