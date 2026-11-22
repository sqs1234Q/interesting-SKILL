# Reading-01: Evidence-Grounded Scientific PDF Parser Skill

A deterministic, triage-routed scientific PDF parsing Skill designed for the Bohrium Harbor `product-arena-08` (**阅读01 | 文献文档解析**) challenge.

## Design Philosophy

Standard LLM agents struggle with scientific PDFs because they write ad-hoc extraction scripts across many turns, scramble multi-column reading order, mix running headers/footers into body text, silently corrupt tables/equations, and hallucinate when fonts are garbled or pages are scanned.

This Skill combines two core architectural ideas:
1. **Page-Level Triage & Layout-Aware Routing (`triage.py` + `extract_layout.py`)**: Inspects every page before extraction to detect encryption, scanned pages, single vs. multi-column gutters, repeating running headers/footers, table/formula density, and CID/font-encoding anomalies, then extracts structured blocks with deterministic `evidence_id` and `bbox` coordinates.
2. **Targeted Dual-Channel Cross-Verification & Hallucination Circuit-Breaker (`dual_verify.py`)**: On flagged high-risk pages (scanned pages, garbled fonts, dense numeric tables, or formula-heavy regions), renders the page to PNG and runs OCR as a second independent channel. Numeric and token agreement is computed automatically; unresolvable conflicts or unreadable spans trip a circuit breaker (`conflict_needs_review` / `unreadable`) instead of guessing.

## Directory Structure

```text
.
├── SKILL.md                      # Agent workflow, routing policy, and exception matrix
├── README.md                     # Architecture, dependencies, usage, and limitations
├── GITHUB_URL.txt                # Public repository link for bonus evaluation
├── examples/
│   └── minimal_example.md        # End-to-end invocation example and sample output
└── scripts/
    ├── __init__.py
    ├── env_probe.py              # Auto-detects installed PDF, rendering, and OCR backends
    ├── triage.py                 # Per-page layout, scan, garble, and risk diagnosis
    ├── extract_layout.py         # Column-aware text, marginalia, table, and outline extraction
    ├── dual_verify.py            # Render + OCR cross-check and hallucination circuit-breaker
    ├── validate_contract.py      # Validates result.json against case.json output_contract
    └── run_pipeline.py           # Unified single-command CLI entrypoint
```

## Dependencies & Runtime Compatibility

All scripts use only Python 3 standard library plus defensive, auto-probed adapters for pre-installed Harbor scientific tools:
- **PDF text & layout**: `pdfplumber` (preferred), `fitz` (PyMuPDF, optional), `pypdf`, or Poppler `pdftotext` / `pdfinfo` CLI.
- **PDF rendering**: `pypdfium2` (preferred), `fitz`, or Poppler `pdftoppm` CLI.
- **OCR**: `pytesseract` + `Pillow`, or `tesseract` CLI.

Run `python3 scripts/env_probe.py` to verify which backends are active in your environment.

## Quick Usage

```bash
# 1. Run full pipeline (triage -> layout extraction -> targeted dual-channel verification)
python3 scripts/run_pipeline.py \
  --pdf /input/sample.pdf \
  --out-dir /app/work/parsed \
  --dual-verify auto \
  --max-ocr-pages 12

# 2. Validate final /app/output/result.json against /input/case.json
python3 scripts/validate_contract.py \
  --case /input/case.json \
  --result /app/output/result.json \
  --evidence /app/work/parsed/evidence_bundle.json
```

## Outputs Generated in `--out-dir`

- `triage_report.json`: Document status and per-page diagnostic metrics (route, layout type, garble ratio, table/math risk).
- `evidence_bundle.json`: Full structured representation containing `pages`, `blocks` (with `evidence_id`, `bbox`, `column`, `block_type`), `tables` (2D grid + Markdown + `bbox`), `marginalia`, `section_outline`, and `dual_verification`.
- `document_structured.md`: Clean reading-order Markdown with inline `[evidence_id | p.N]` anchors for fast LLM consumption.
- `dual_verification_report.json`: Agreement scores, numeric discrepancies, and circuit-breaker flags for high-risk pages.

## Known Limitations & Self-Check

- **Complex visual equations**: Inline Unicode/Greek/subscripts in digital PDFs are preserved verbatim; purely rasterized multi-line equations without LaTeX OCR models are flagged as `formula_visual_only` with page/bbox coordinates rather than guessed.
- **Borderless tables**: Heuristic line-based and whitespace-aligned tables are extracted via `pdfplumber`; highly irregular borderless tables may require inspecting the rendered PNG crop saved during dual verification.
- **Self-check**: Run `scripts/validate_contract.py` after generating `result.json` to ensure zero schema errors and 100% traceable `evidence_id` references.
