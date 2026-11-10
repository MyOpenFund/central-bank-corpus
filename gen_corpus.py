"""Regenerate CORPUS.md from the per-bank manifests (reproducible data reference).

Reads every `data/manifest/<bank>.jsonl` through the storage layer (which also
falls back to a legacy single `data/manifest.jsonl` on pre-split repos).
CORPUS.md is gitignored: a local snapshot, never committed.

Run:  python3.13 gen_corpus.py [--data-dir data] [--out CORPUS.md]
"""
import argparse
import collections
import subprocess
from pathlib import Path

from cb_corpus.banks import BIS_63
from cb_corpus.config import Config
from cb_corpus.storage import iter_manifest_rows


def _raw_size(raw_dir: Path) -> str:
    """Human-readable size of raw/ via `du -sh`; "n/a" when absent/unreadable."""
    if not raw_dir.is_dir():
        return "n/a"
    try:
        res = subprocess.run(["du", "-sh", str(raw_dir)], capture_output=True, text=True)
        return res.stdout.split()[0] if res.returncode == 0 and res.stdout.split() else "n/a"
    except OSError:
        return "n/a"


def build_corpus_md(cfg: Config) -> str:
    """CORPUS.md content for the corpus under `cfg.data_dir`."""
    names = {b.code: b.name for b in BIS_63}
    rows = list(iter_manifest_rows(cfg))

    bt = collections.defaultdict(collections.Counter)
    bytype = collections.Counter(); prov = collections.Counter(); lang = collections.Counter()
    bankyears = collections.defaultdict(list)
    ty_dec = collections.defaultdict(collections.Counter)
    for d in rows:
        c, t = d["bank_code"], d["doc_type"]
        bt[c][t] += 1; bytype[t] += 1
        prov[d.get("provenance", "?")] += 1; lang[d.get("language", "?")] += 1
        if d.get("year"):
            bankyears[c].append(d["year"]); ty_dec[t][(d["year"] // 5) * 5] += 1

    total = len(rows)
    nbanks = len(bt)
    allyears = [y for ys in bankyears.values() for y in ys]
    size = _raw_size(cfg.raw_dir)
    tot_by_bank = collections.Counter({c: sum(v.values()) for c, v in bt.items()})
    TYPES_PRESENT = sorted(t for t in bytype if bytype[t])   # every type actually present
    missing = [b.code for b in BIS_63 if b.code not in bt]
    period = f"{min(allyears)} → {max(allyears)}" if allyears else "n/a"

    out = []
    W = out.append
    W("# Central bank corpus — detailed documentation\n")
    W("*Data reference — **auto-regenerated** from `data/manifest/<bank>.jsonl` "
      "via `gen_corpus.py`.*\n")
    W("> RAG ingestion: `INGESTION_RAG.md` · Banks and adapters: `BANKS.md` "
      "· Working papers: `docs/WP_V3_SUMMARY.md`\n")

    W("\n## 1. Overview\n")
    W("| Metric | Value |")
    W("|---|---|")
    W(f"| Documents | **{total:,}** |".replace(",", " "))
    W(f"| Central banks | **{nbanks}** (out of 63 targeted) |")
    W(f"| Period | **{period}** |")
    W(f"| Language | **{', '.join(lang) or 'n/a'}** |")
    W(f"| Size | **{size}** |")
    W(f"| Index | `data/manifest/<bank>.jsonl` (1 JSON line / document) |")
    W("\n**Principle:** first-hand official documents only (the bank's site, "
      "`bis.org` for speeches, or IDEAS/RePEc → publisher PDF for working papers). "
      "Dedup by `doc_id` + `sha256`.\n")

    W("\n## 2. Data schema (`data/manifest/<bank>.jsonl`)\n")
    W("| Field | RAG use |")
    W("|---|---|")
    for f, u in [("bank_code", "bank/country **filter**"), ("doc_type", "type **filter**"),
                 ("title", "title / embedding"), ("date / year", "**temporal filter**"),
                 ("pdf_url", "**citation** (official source)"), ("source_url", "origin page"),
                 ("provenance", "`bis_index` / `bank_site` / `repec_discovery`"),
                 ("mime_type", "`application/pdf` / `text/html`"),
                 ("local_path", "**file path** to read"), ("html_path", "HTML source (if rendered)"),
                 ("sha256", "dedup / integrity"), ("doc_id", "stable primary key"),
                 ("date_precision / date_source", "date quality (working papers)")]:
        W(f"| `{f}` | {u} |")
    W("\nLayout: `data/raw/<bank>/<doc_type>/<year>/<doc_id>.<pdf|html>`\n")

    W("\n## 3. Inventory by document type\n")
    W("| Type | Description | Count | Source |")
    W("|---|---|---:|---|")
    desc = {"A1": ("Rate decisions", "bank sites"),
            "A2": ("Monetary policy statements", "bank sites"),
            "A3": ("Minutes / accounts", "bank sites"),
            "B1": ("Press conferences (Q&A)", "ECB/Fed"),
            "C1": ("Speeches", "BIS index"),
            "C2": ("Interviews / op-eds / testimonies", "ECB"),
            "D1": ("Working papers", "bank sites + RePEc/IDEAS"),
            "D2": ("Occasional / discussion papers", "RePEc/IDEAS"),
            "D3": ("Blog / research letters", "bank sites"),
            "E1": ("Monetary policy / inflation reports", "bank sites"),
            "E2": ("Financial stability (FSR, macropru.)", "bank sites"),
            "E3": ("Annual / convergence reports", "ECB"),
            "E4": ("Economic bulletins", "ECB"),
            "F1": ("Projections / forecasts (SEP)", "Fed"),
            "G2": ("Surveys / statistical publications", "ECB")}
    for t in TYPES_PRESENT:
        de, s = desc.get(t, (t, "—"))
        W(f"| **{t}** | {de} | {bytype[t]:,} | {s} |".replace(",", " "))
    W(f"\n**Provenance:** " + " · ".join(f"`{k}` {v:,}".replace(",", " ")
                                          for k, v in prov.most_common()) + "\n")

    W("\n## 4. Inventory by country\n")
    W(f"{nbanks} banks, sorted by volume. Columns = documents per type.\n")
    W("| # | Code | Central bank | Total | Years | " + " | ".join(TYPES_PRESENT) + " |")
    W("|---:|---|---|---:|---|" + "|".join("---:" for _ in TYPES_PRESENT) + "|")
    for i, (c, n) in enumerate(tot_by_bank.most_common(), 1):
        ys = bankyears[c]; yr = f"{min(ys)}-{max(ys)}" if ys else "—"
        cells = " | ".join(str(bt[c][t]) if bt[c][t] else "—" for t in TYPES_PRESENT)
        W(f"| {i} | `{c}` | {names.get(c, c)} | {n:,} | {yr} | {cells} |".replace(",", " "))
    W("\n### Targeted banks absent (0 docs)")
    for c in missing:
        W(f"- `{c}` {names[c]}")

    W("\n## 5. Temporal coverage (documents per 5-year bucket)\n")
    decs = sorted({(y // 5) * 5 for y in allyears})
    W("| Type | " + " | ".join(f"{d}" for d in decs) + " |")
    W("|---|" + "|".join("---:" for _ in decs) + "|")
    for t in TYPES_PRESENT:
        W(f"| {t} | " + " | ".join(str(ty_dec[t].get(d, 0) or "—") for d in decs) + " |")

    W("\n## 6. RAG ingestion notes (see `INGESTION_RAG.md` for detail)\n")
    W("- Filters: `bank_code`, `doc_type`, `year`, `provenance`.")
    W("- Citation: `pdf_url` + `title` + `date`. Loading: `local_path`.")
    W("- Dedup already done (`sha256`). `text/html` rows are indexed documents too.")
    if total:
        W(f"- Composition: speeches {100*bytype['C1']//total}% · working papers "
          f"{100*(bytype['D1']+bytype['D2'])//total}% · others. Weight retrieval as needed.")

    W("\n## 7. RePEc completeness (D1/D2)\n")
    W("Measured live, never from constants: `python -m cb_corpus repec-check` "
      "(papers IDEAS lists that the corpus lacks, and manifest rows RePEc does not list). "
      "June 2026 results: `docs/WP_V3_SUMMARY.md`.\n")

    W("\n## 8. Reproducibility\n")
    W("```bash")
    W("python -m cb_corpus bis-sitemap --download     # C1 speeches")
    W("python -m cb_corpus repec --download           # D1/D2 working papers (paginated, dated)")
    W("python -m cb_corpus discover --download --rounds 3   # native A/B/E/F per bank")
    W("python3.13 gen_corpus.py                        # regenerate this file")
    W("```")
    W("Idempotent (`doc_id`+`sha256`), convergence (`--rounds`), failures traced in "
      "`data/discovery_errors.jsonl`.\n")

    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="data", help="corpus data root (default ./data)")
    ap.add_argument("--out", default="CORPUS.md", help="output file (default ./CORPUS.md)")
    args = ap.parse_args(argv)
    cfg = Config(data_dir=Path(args.data_dir))
    md = build_corpus_md(cfg)
    Path(args.out).write_text(md)
    print(f"{args.out} regenerated: {len(md)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
