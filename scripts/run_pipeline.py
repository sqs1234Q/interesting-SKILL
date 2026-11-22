#!/usr/bin/env python3
"""Single-command entrypoint: triage -> layout extraction -> targeted dual-channel verification."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from dual_verify import verify_pages
from env_probe import probe_environment
from extract_layout import extract_document_layout, render_structured_markdown
from triage import triage_pdf


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run triage, layout-aware extraction, and targeted dual-channel OCR verification."
    )
    parser.add_argument("--pdf", required=True, help="Path to input PDF file")
    parser.add_argument("--out-dir", required=True, help="Directory to store parsed artifacts")
    parser.add_argument(
        "--dual-verify",
        choices=["auto", "all", "none"],
        default="auto",
        help="auto: verify scanned/garbled/table/math flagged pages; all: verify all pages; none: skip OCR",
    )
    parser.add_argument("--max-ocr-pages", type=int, default=12, help="Max pages for OCR to stay within runtime budget")
    parser.add_argument("--dpi", type=int, default=200, help="Render DPI for dual-channel OCR")
    args = parser.parse_args()

    t0 = time.time()
    os.makedirs(args.out_dir, exist_ok=True)
    render_dir = os.path.join(args.out_dir, "renders")

    env_info = probe_environment()
    triage_report = triage_pdf(args.pdf)
    triage_path = os.path.join(args.out_dir, "triage_report.json")
    with open(triage_path, "w", encoding="utf-8") as f:
        json.dump(triage_report, f, ensure_ascii=False, indent=2)

    bundle = extract_document_layout(args.pdf, triage_report)
    bundle["environment"] = {
        "text_backend": env_info["recommended_text_backend"],
        "render_backend": env_info["recommended_render_backend"],
        "ocr_available": env_info["ocr_available"],
    }

    pages_to_verify: List[int] = []
    if triage_report.get("document_status") in {"ok", "partial_ocr", "scanned_all"}:
        if args.dual_verify == "all":
            pages_to_verify = [p["page"] for p in triage_report.get("pages", [])]
        elif args.dual_verify == "auto":
            summary = triage_report.get("summary", {})
            # Prioritize scanned pages first, then garbled/table/math high-risk pages
            pages_to_verify = list(summary.get("ocr_full_pages", []))
            for pno in summary.get("dual_verify_pages", []):
                if pno not in pages_to_verify:
                    pages_to_verify.append(pno)
        pages_to_verify = pages_to_verify[: max(0, args.max_ocr_pages)]

    dual_report = {"ocr_lang": None, "pages_checked": [], "results": []}
    if pages_to_verify:
        dual_report = verify_pages(
            pdf_path=args.pdf,
            bundle=bundle,
            pages_to_check=pages_to_verify,
            render_dir=render_dir,
            dpi=args.dpi,
        )

    dual_path = os.path.join(args.out_dir, "dual_verification_report.json")
    with open(dual_path, "w", encoding="utf-8") as f:
        json.dump(dual_report, f, ensure_ascii=False, indent=2)

    bundle["dual_verification"] = dual_report
    bundle["elapsed_seconds"] = round(time.time() - t0, 2)

    bundle_path = os.path.join(args.out_dir, "evidence_bundle.json")
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(args.out_dir, "document_structured.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_structured_markdown(bundle))

    summary_out = {
        "document_status": bundle.get("document_status"),
        "page_count": bundle.get("page_count"),
        "pages_dual_verified": pages_to_verify,
        "elapsed_seconds": bundle["elapsed_seconds"],
        "artifacts": {
            "triage_report": triage_path,
            "evidence_bundle": bundle_path,
            "structured_markdown": md_path,
            "dual_verification": dual_path,
        },
    }
    print(json.dumps(summary_out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
