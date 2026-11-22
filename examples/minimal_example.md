# Minimal Invocation Example (Illustrative)

> Note: The inputs and outputs below are **illustrative examples** demonstrating the expected invocation flow and schema.

## 1. Example Input (`/input/case.json`)

```json
{
  "case_id": "demo-paper-001",
  "pdf_path": "/input/demo_two_column_paper.pdf",
  "output_contract": {
    "required_fields": ["case_id", "status", "summary", "tables", "evidence", "uncertainties"]
  }
}
```

## 2. Example Command

```bash
python3 /skills/weekly-skill/scripts/run_pipeline.py \
  --pdf /input/demo_two_column_paper.pdf \
  --out-dir /app/work/parsed \
  --dual-verify auto
```

## 3. Illustrative Pipeline Output (`evidence_bundle.json` excerpt)

```json
{
  "document_status": "ok",
  "page_count": 2,
  "pages": [
    {
      "page": 1,
      "layout_type": "two_column",
      "route": "layout_text",
      "blocks": [
        {
          "evidence_id": "p1_full_b01",
          "page": 1,
          "column": "full",
          "block_type": "title",
          "bbox": [72.0, 58.2, 540.0, 84.5],
          "text": "Phase-Sensitive Terahertz Spectroscopy of Organic Crystals"
        },
        {
          "evidence_id": "p1_col1_b02",
          "page": 1,
          "column": "left",
          "block_type": "body",
          "bbox": [54.0, 142.0, 288.0, 240.6],
          "text": "1. Introduction. Crystal nucleation kinetics depend strongly on..."
        }
      ],
      "tables": [
        {
          "evidence_id": "p1_tbl01",
          "page": 1,
          "bbox": [310.0, 320.0, 545.0, 445.0],
          "rows": [
            ["Sample", "Temp (K)", "Peak (THz)"],
            ["Alpha-Gly", "298", "1.42"]
          ],
          "markdown": "| Sample | Temp (K) | Peak (THz) |\n| --- | --- | --- |\n| Alpha-Gly | 298 | 1.42 |"
        }
      ]
    }
  ]
}
```

## 4. Illustrative Final Output (`/app/output/result.json`)

```json
{
  "case_id": "demo-paper-001",
  "status": "completed_with_warnings",
  "summary": "The paper measures THz absorption peaks of organic crystals; Alpha-Gly shows a peak at 1.42 THz at 298 K [p1_tbl01].",
  "tables": [
    {
      "evidence_id": "p1_tbl01",
      "page": 1,
      "markdown": "| Sample | Temp (K) | Peak (THz) |\n| --- | --- | --- |\n| Alpha-Gly | 298 | 1.42 |"
    }
  ],
  "evidence": [
    {
      "evidence_id": "p1_tbl01",
      "page": 1,
      "bbox": [310.0, 320.0, 545.0, 445.0],
      "quote": "Alpha-Gly | 298 | 1.42"
    }
  ],
  "uncertainties": [
    {
      "page": 2,
      "evidence_id": "p2_ocr_b01",
      "reason": "Scanned page recovered via OCR; low-contrast subscript requires human check."
    }
  ]
}
```

## 5. Contract Validation Command

```bash
python3 /skills/weekly-skill/scripts/validate_contract.py \
  --case /input/case.json \
  --result /app/output/result.json \
  --evidence /app/work/parsed/evidence_bundle.json
```
