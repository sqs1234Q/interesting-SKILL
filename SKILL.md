---
name: reading-01-evidence-pdf-parser
description: Triage-routed scientific PDF parser with layout-aware reading order, table/formula preservation, targeted dual-channel OCR cross-verification, hallucination circuit-breaking, and strict Harbor output_contract validation.
---

# Scientific Document Parsing & Evidence-Grounded Reading Skill

You are executing a scientific literature parsing and evidence-grounded reading task in the Harbor runtime (`/input/case.json`, `/input/task.md`, working directory `/app/work`, final output `/app/output/result.json`).

## Core Principles

1. **Deterministic Scripts First (Budget & Stability)**: Never spend multiple turns writing ad-hoc PDF extraction scripts from scratch. Immediately run the bundled pipeline CLI (`scripts/run_pipeline.py`) to obtain a deterministic, layout-ordered, evidence-indexed JSON bundle and readable Markdown view.
2. **Evidence-Grounded Answers (`核得回`)**: Every factual claim, extracted value, table cell, equation, or summary point MUST cite its exact `evidence_id`, `page`, and `bbox` (or section/table/caption label) from `evidence_bundle.json`.
3. **Hallucination Circuit-Breaker**: Never guess or reconstruct unreadable, garbled, encrypted, or conflicting text from prior knowledge. If `dual_verify.py` flags a block as `conflict_needs_review` or `unreadable`, explicitly report the uncertainty, quote both channels if available, and mark the item as unverified/unreported per `output_contract`.
4. **Strict Output Contract**: Preserve `case_id` and every required field defined in `/input/case.json` (`output_contract`), and always run `scripts/validate_contract.py` before finishing.

## Standard Execution Workflow

Follow these 4 steps sequentially. Target **3 to 6 total model turns** per case to stay well within the 64-call / 900-second budget.

### Step 1: Inspect Task & Contract
Read `/input/case.json` and `/input/task.md` (if present). Identify:
- `case_id` and exact input PDF path(s).
- Task objective (full parsing, structured extraction, table/formula extraction, QA, translation, or verification).
- Exact `output_contract` schema and required keys.

### Step 2: Run the Triage + Layout + Dual-Verify Pipeline
Locate this skill directory (typically `/skills/weekly-skill` or the directory containing this `SKILL.md`) and run a single command:

```bash
python3 /skills/weekly-skill/scripts/run_pipeline.py \
  --pdf "<PDF_PATH>" \
  --out-dir /app/work/parsed \
  --dual-verify auto \
  --max-ocr-pages 12
```

- If the PDF is encrypted, corrupted, or missing a text layer, `run_pipeline.py` will not crash; it records `document_status` (`ok`, `partial_ocr`, `encrypted`, `corrupted`, `empty`) and continues with fallback channels where possible.
- Inspect `/app/work/parsed/triage_report.json` and `/app/work/parsed/document_structured.md` (plus `/app/work/parsed/evidence_bundle.json` for exact coordinates and tables).
- For very long PDFs, read `triage_report.json` and `section_outline` first, then inspect only the pages/blocks relevant to `task.md` to stay within the 16,384-token response limit.

### Step 3: Targeted Deep-Dive (Only When Needed)
- If `dual_verify` flagged specific high-risk blocks (`conflict_needs_review`, `garbled_font`, or `table_number_mismatch`), inspect the rendered PNG crop path listed in `dual_verification_report.json` or re-run `scripts/dual_verify.py --pdf "<PDF_PATH>" --pages "<page_list>" --dpi 300` for higher-resolution OCR.
- If a table spans multiple pages (flagged as `possible_continuation: true`), stitch rows explicitly while preserving the original page and table IDs for each row.
- For formulas and special characters, preserve raw Unicode/Greek/subscripts/superscripts verbatim and attach the formula number (e.g., `Eq. (3)`) and `evidence_id`.

### Step 4: Write & Validate `/app/output/result.json`
Write the final structured response to `/app/output/result.json`, ensuring:
1. All `output_contract` fields and `case_id` from `/input/case.json` are preserved.
2. Key findings include traceable evidence anchors (`evidence_id`, `page`, `bbox`, `quote`).
3. Unreported, unreadable, or conflicting items are explicitly listed under uncertainty/limitations rather than silently omitted or guessed.
4. Run the contract & evidence validator:

```bash
python3 /skills/weekly-skill/scripts/validate_contract.py \
  --case /input/case.json \
  --result /app/output/result.json \
  --evidence /app/work/parsed/evidence_bundle.json
```

If validation reports any missing required key, `case_id` mismatch, or invalid `evidence_id`, fix `/app/output/result.json` and re-run validation until it exits with code `0`.

## Exception Handling Matrix

| Failure Mode | Detection Signal | Required Action |
| :--- | :--- | :--- |
| **Encrypted / Password PDF** | `triage_report.document_status == "encrypted"` | Do not fabricate content. Set run status to `rejected` / `unreadable`, explain encryption, and specify manual takeover point. |
| **Pure Scanned / Image PDF** | `page.route == "ocr_full"` | Pipeline automatically renders via `pypdfium2`/`pdftoppm` and runs `tesseract` OCR. Report OCR confidence and flag low-confidence spans. |
| **Garbled Font / CID Encoding** | `cid_ratio > 0.02` or `replacement_char_ratio > 0.01` | Pipeline triggers `dual_verify.py` visual OCR fallback. Prefer OCR text only if verified; otherwise mark `conflict_needs_review`. |
| **Multi-Column Scrambling** | `page.layout_type == "multi_column"` | Pipeline applies vertical gutter detection + band splitting (full-width headers/abstracts vs. left-then-right columns). |
| **Table Number Discrepancy** | `dual_verify.number_mismatch == true` | Report both text-layer and OCR-layer values with `bbox`, and flag the cell for human verification instead of silently picking one. |
| **Running Headers / Footers** | Repeated top/bottom margin text across pages | Stripped from main body flow and stored separately under `page.marginalia` so they never break cross-page sentences. |
