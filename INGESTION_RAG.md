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

| Field | RAG usage |
|---|---|
| `local_path` | **path of the file to read** (PDF, or HTML if rendering failed) |
| `bank_code` | **filter** + metadata (map to full name via `cb_corpus.banks`) |
| `doc_type` | **filter** + metadata (map to a readable label, cf. §5) |
| `year`, `date` | **temporal filter** / facet |
| `title` | chunk title / display |
| `pdf_url` | **citation** (official source) |
| `language` | filter (all `en` today) |
| `date_precision` | `day` / `month` / `year` when set (mostly working papers); absent = not recorded (older rows: do not assume a day) |
| `date_source` | where the date comes from: `bank_site`, `pdf_meta`, `wayback`, `repec` (working papers) |
| `mime_type` | `application/pdf` or `text/html` (the HTML is the artifact when PDF rendering failed); rarely another official format |
| `html_path` | the kept raw HTML next to a rendered PDF, when there is one |
| `alt_urls` | other official URLs of the same document (dedup; also valid citations) |
| `doc_id` | stable primary key (ideal as a document `id` in the store) |
| `sha256` | change detection / ingestion idempotence |

---

## 2. Minimal ingestion pipeline

```python
import json
from pathlib import Path
import fitz  # PyMuPDF — fast and robust for PDF text extraction

from cb_corpus.banks import get_bank
from cb_corpus.taxonomy import by_code

ROOT = Path("/path/to/cb_corpus")
MANIFESTS = sorted((ROOT / "data" / "manifest").glob("*.jsonl"))  # one file per bank

def iter_docs():
    for line in (l for m in MANIFESTS for l in m.open()):
        d = json.loads(line)
        # ingest only PDFs actually present
        lp = d.get("local_path")
        if not lp or not lp.endswith(".pdf"):
            continue
        p = ROOT / lp
        if p.exists():
            yield d, p

def extract_text(pdf_path: Path) -> str:
    with fitz.open(pdf_path) as doc:
        return "\n".join(page.get_text("text") for page in doc)

def doc_metadata(d: dict) -> dict:
    """Clean metadata, ready for filtering + citation."""
    return {
        "doc_id":   d["doc_id"],
        "bank_code": d["bank_code"],
        "bank_name": get_bank(d["bank_code"]).name,
        "doc_type":  d["doc_type"],
        "doc_type_label": by_code(d["doc_type"]).label,  # ex. "Speech"
        "year":   d.get("year"),
        "date":   d.get("date"),
        "title":  d.get("title", ""),
        "source_url": d.get("pdf_url"),   # <- the official citation
        "language":   d.get("language", "en"),
    }
```

---

## 3. Chunking

Recommendations for this corpus (speeches + reports, often long):

- **Size**: ~800–1,200 tokens per chunk, **overlap** ~100–150 tokens.
- **Semantic splitting** first (paragraphs / line breaks), then group up to the
  target size — avoid cutting in the middle of a sentence.
- **Carry the document metadata onto each chunk** (same `doc_id`, `bank_code`,
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
`bank_code` / `doc_type` / `year` as **filterable metadata**.

---

## 4. Filtering at retrieval

Metadata filters make answers targeted and citable:

| Question | Filter |
|---|---|
| "What does the ECB say about inflation in 2023?" | `bank_code = "ecb" AND year = 2023` |
| "Fed minutes" | `bank_code = "us" AND doc_type = "A3"` |
| "Speeches by euro-area banks since 2020" | `bank_code IN (…) AND doc_type = "C1" AND year >= 2020` |

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
(`pdf_url`). Example rendering:

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
- **Idempotence**: re-ingest based on `sha256` (unchanged = already indexed) to avoid
  recomputing everything on each corpus update.
