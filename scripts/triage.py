#!/usr/bin/env python3
"""Page-level PDF triage: detects encryption, scans, columns, garbled fonts, and table/math risk."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple

logging.getLogger("pdfminer").setLevel(logging.ERROR)

CID_RE = re.compile(r"\(cid:\d+\)")
GREEK_MATH_RE = re.compile(r"[\u0370-\u03FF\u2200-\u22FF\u2070-\u209F±×÷≤≥≠≈∞∑∏∫∂∇√]")
NUM_TOKEN_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?(?:%|e[+-]?\d+)?(?![A-Za-z])", re.I)


def _compute_garble_metrics(text: str) -> Dict[str, float]:
    if not text:
        return {"cid_ratio": 0.0, "replacement_ratio": 0.0, "pua_ratio": 0.0}
    total = max(len(text), 1)
    cid_chars = sum(len(m.group(0)) for m in CID_RE.finditer(text))
    replacement_chars = text.count("\ufffd")
    pua_chars = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    return {
        "cid_ratio": round(cid_chars / total, 4),
        "replacement_ratio": round(replacement_chars / total, 4),
        "pua_ratio": round(pua_chars / total, 4),
    }


def _bbox_overlaps(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
    pad: float = 2.0,
) -> bool:
    return not (
        a[2] < b[0] - pad
        or a[0] > b[2] + pad
        or a[3] < b[1] - pad
        or a[1] > b[3] + pad
    )


def _detect_columns(
    words: List[Dict[str, Any]],
    page_width: float,
    table_bboxes: List[Tuple[float, float, float, float]] | None = None,
) -> Tuple[str, float | None]:
    """Detect true two-column layouts via vertical gutter scanning (resilient to full-width titles/tables)."""
    if len(words) < 30 or page_width <= 0:
        return "single_column", None

    # Exclude words inside detected tables so wide tables do not mask a two-column body
    filtered_words = words
    if table_bboxes:
        non_tbl = [
            w
            for w in words
            if not any(
                _bbox_overlaps(
                    (float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"])),
                    tb,
                    pad=2.0,
                )
                for tb in table_bboxes
            )
        ]
        if len(non_tbl) >= 25:
            filtered_words = non_tbl

    sorted_words = sorted(filtered_words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: List[List[Dict[str, Any]]] = []
    for w in sorted_words:
        if not lines:
            lines.append([w])
            continue
        prev_top = sum(x["top"] for x in lines[-1]) / len(lines[-1])
        if abs(w["top"] - prev_top) <= 4.0:
            lines[-1].append(w)
        else:
            lines.append([w])

    if len(lines) < 6:
        return "single_column", None

    # Precompute line y-centers and sorted words
    line_records: List[Tuple[float, List[Dict[str, Any]]]] = []
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        y_mid = sum((w["top"] + w["bottom"]) / 2.0 for w in ln) / len(ln)
        line_records.append((y_mid, ln))

    best_score = -1e9
    best_ties: List[Tuple[float, int, int, int, int, int, int, float]] = []

    x_start = int(page_width * 0.40)
    x_end = int(page_width * 0.60)
    for cand_x in range(x_start, x_end + 1, 2):
        gx = float(cand_x)
        left_lines = 0
        right_lines = 0
        both_side_lines = 0
        crossing_lines = 0
        left_words_cnt = 0
        right_words_cnt = 0
        left_ys: List[float] = []
        right_ys: List[float] = []

        for y_mid, ln in line_records:
            crosses = any(w["x0"] < gx - 1.5 and w["x1"] > gx + 1.5 for w in ln)
            if crosses:
                crossing_lines += 1
                continue

            l_w = [w for w in ln if (w["x0"] + w["x1"]) / 2.0 < gx]
            r_w = [w for w in ln if (w["x0"] + w["x1"]) / 2.0 >= gx]

            if l_w and r_w:
                gap = min(w["x0"] for w in r_w) - max(w["x1"] for w in l_w)
                if gap < 6.5:
                    crossing_lines += 1
                    continue
                both_side_lines += 1
                left_lines += 1
                right_lines += 1
                left_words_cnt += len(l_w)
                right_words_cnt += len(r_w)
                left_ys.append(y_mid)
                right_ys.append(y_mid)
            elif l_w:
                left_lines += 1
                left_words_cnt += len(l_w)
                left_ys.append(y_mid)
            elif r_w:
                right_lines += 1
                right_words_cnt += len(r_w)
                right_ys.append(y_mid)

        # Compute vertical overlap ratio between left and right columns
        if left_ys and right_ys:
            ov_top = max(min(left_ys), min(right_ys))
            ov_bot = min(max(left_ys), max(right_ys))
            span = max(max(left_ys + right_ys) - min(left_ys + right_ys), 1.0)
            v_overlap = max(0.0, ov_bot - ov_top) / span
        else:
            v_overlap = 0.0

        score = both_side_lines * 2.5 + min(left_lines, right_lines) * 1.5 - crossing_lines * 2.5
        stat_tuple = (
            gx,
            left_lines,
            right_lines,
            both_side_lines,
            crossing_lines,
            left_words_cnt,
            right_words_cnt,
            v_overlap,
        )
        if score > best_score:
            best_score = score
            best_ties = [stat_tuple]
        elif score == best_score:
            best_ties.append(stat_tuple)

    best_stats = best_ties[len(best_ties) // 2] if best_ties else None
    if best_stats is not None:
        (
            gx,
            left_lines,
            right_lines,
            both_side_lines,
            crossing_lines,
            left_words_cnt,
            right_words_cnt,
            v_overlap,
        ) = best_stats
        total_lines = len(line_records)
        non_crossing_ratio = (total_lines - crossing_lines) / max(total_lines, 1)
        if (
            min(left_lines, right_lines) >= 4
            and min(left_words_cnt, right_words_cnt) >= 12
            and v_overlap >= 0.20
            and non_crossing_ratio >= 0.55
            and (both_side_lines >= 3 or min(left_lines, right_lines) >= 6)
        ):
            return "two_column", round(gx, 2)

    return "single_column", None


def _has_real_table_rules(page: Any, page_width: float) -> bool:
    """Distinguish real table rules (including three-line tables) from short hyperlink underlines."""
    long_h_rules = 0
    v_rules = 0
    for ln in (page.lines or []):
        dx = abs(float(ln.get("x1", 0)) - float(ln.get("x0", 0)))
        dy = abs(float(ln.get("bottom", 0)) - float(ln.get("top", 0)))
        if dy <= 2.0 and dx >= page_width * 0.32:
            long_h_rules += 1
        elif dx <= 2.0 and dy >= 18.0:
            v_rules += 1
    for rc in (page.rects or []):
        dx = abs(float(rc.get("x1", 0)) - float(rc.get("x0", 0)))
        dy = abs(float(rc.get("bottom", 0)) - float(rc.get("top", 0)))
        if dy <= 2.5 and dx >= page_width * 0.32:
            long_h_rules += 1
        elif dx <= 2.5 and dy >= 18.0:
            v_rules += 1
    return (long_h_rules >= 3) or (long_h_rules >= 2 and v_rules >= 2)


def triage_pdf(pdf_path: str) -> Dict[str, Any]:
    """Inspect PDF structure and return page-level routing & risk diagnostics."""
    if not os.path.exists(pdf_path):
        return {
            "pdf_path": pdf_path,
            "document_status": "missing_file",
            "error": f"File not found: {pdf_path}",
            "page_count": 0,
            "pages": [],
        }

    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            try:
                dec = reader.decrypt("")
                if dec == 0:
                    return {
                        "pdf_path": pdf_path,
                        "document_status": "encrypted",
                        "error": "PDF is password-protected and cannot be decrypted with an empty password.",
                        "page_count": 0,
                        "pages": [],
                    }
            except Exception as exc:
                return {
                    "pdf_path": pdf_path,
                    "document_status": "encrypted",
                    "error": f"PDF encryption check failed: {exc}",
                    "page_count": 0,
                    "pages": [],
                }
    except ImportError:
        pass
    except Exception as exc:
        return {
            "pdf_path": pdf_path,
            "document_status": "corrupted",
            "error": f"Unable to open PDF with pypdf: {exc}",
            "page_count": 0,
            "pages": [],
        }

    try:
        import pdfplumber
    except ImportError as exc:
        return {
            "pdf_path": pdf_path,
            "document_status": "backend_missing",
            "error": f"pdfplumber not installed: {exc}",
            "page_count": 0,
            "pages": [],
        }

    pages_diag: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            if page_count == 0:
                return {
                    "pdf_path": pdf_path,
                    "document_status": "empty",
                    "page_count": 0,
                    "pages": [],
                }

            for idx, page in enumerate(pdf.pages, start=1):
                width = float(page.width or 0)
                height = float(page.height or 0)
                page_area = max(width * height, 1.0)

                text = page.extract_text() or ""
                stripped = re.sub(r"\s+", "", text)
                char_count = len(stripped)

                words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
                body_words = [
                    w for w in words
                    if w.get("top", 0) > height * 0.08 and w.get("bottom", height) < height * 0.92
                ]
                body_char_count = sum(len(w.get("text", "")) for w in body_words)

                images = page.images or []
                img_area = sum(
                    max(0.0, float(im.get("x1", 0)) - float(im.get("x0", 0)))
                    * max(0.0, float(im.get("bottom", 0)) - float(im.get("top", 0)))
                    for im in images
                )
                image_coverage = round(min(img_area / page_area, 1.0), 3)

                found_tables = [
                    t for t in (page.find_tables() or [])
                    if len([r for r in (t.extract() or []) if any((c or "").strip() for c in r)]) >= 2
                ]
                detected_tables = len(found_tables)
                table_bboxes = [tuple(float(v) for v in t.bbox) for t in found_tables]

                garble = _compute_garble_metrics(text)
                layout_type, gutter_x = _detect_columns(
                    body_words or words, width, table_bboxes=table_bboxes
                )

                has_table_rules = _has_real_table_rules(page, width)
                math_hits = len(GREEK_MATH_RE.findall(text))
                num_hits = len(NUM_TOKEN_RE.findall(text))

                risk_flags: List[str] = []
                if body_char_count < 30 and (image_coverage >= 0.15 or len(images) > 0):
                    risk_flags.append("scanned_or_image_only")
                elif body_char_count < 15:
                    risk_flags.append("near_empty_text_layer")

                if (
                    garble["cid_ratio"] > 0.02
                    or garble["replacement_ratio"] > 0.01
                    or garble["pua_ratio"] > 0.03
                ):
                    risk_flags.append("garbled_font_or_cid")

                if detected_tables >= 1 or (has_table_rules and num_hits >= 10):
                    risk_flags.append("table_dense")

                if math_hits >= 6:
                    risk_flags.append("formula_symbol_dense")

                if "scanned_or_image_only" in risk_flags:
                    route = "ocr_full"
                elif (
                    "garbled_font_or_cid" in risk_flags
                    or "table_dense" in risk_flags
                    or "formula_symbol_dense" in risk_flags
                ):
                    route = "layout_plus_dual_verify"
                else:
                    route = "layout_text"

                pages_diag.append(
                    {
                        "page": idx,
                        "width": round(width, 2),
                        "height": round(height, 2),
                        "char_count": char_count,
                        "body_char_count": body_char_count,
                        "word_count": len(words),
                        "image_count": len(images),
                        "image_coverage": image_coverage,
                        "detected_tables": detected_tables,
                        "has_table_rules": has_table_rules,
                        "math_symbol_count": math_hits,
                        "numeric_token_count": num_hits,
                        "garble_metrics": garble,
                        "layout_type": layout_type,
                        "gutter_x": gutter_x,
                        "risk_flags": risk_flags,
                        "route": route,
                    }
                )
    except Exception as exc:
        return {
            "pdf_path": pdf_path,
            "document_status": "corrupted",
            "error": f"Failed during pdfplumber triage: {exc}",
            "page_count": 0,
            "pages": [],
        }

    # Document-level column consensus: prevent stray wide tables/figures on single-column papers
    # from triggering false two_column splits on isolated pages
    if len(pages_diag) >= 4:
        raw_two_col = [p for p in pages_diag if p["layout_type"] == "two_column"]
        two_col_ratio = len(raw_two_col) / len(pages_diag)
        if two_col_ratio < 0.45:
            for p in raw_two_col:
                p["layout_type"] = "single_column"
                p["gutter_x"] = None
        elif two_col_ratio >= 0.65 and raw_two_col:
            gutters = sorted(p["gutter_x"] for p in raw_two_col if p.get("gutter_x") is not None)
            if gutters:
                med_gutter = gutters[len(gutters) // 2]
                for p in pages_diag:
                    if p["layout_type"] == "two_column" and p.get("gutter_x") is not None:
                        if abs(p["gutter_x"] - med_gutter) > 25.0:
                            p["gutter_x"] = med_gutter

    ocr_pages = [p["page"] for p in pages_diag if p["route"] == "ocr_full"]
    dual_pages = [p["page"] for p in pages_diag if p["route"] == "layout_plus_dual_verify"]

    doc_status = "ok"
    if len(ocr_pages) == len(pages_diag):
        doc_status = "scanned_all"
    elif ocr_pages:
        doc_status = "partial_ocr"

    return {
        "pdf_path": pdf_path,
        "document_status": doc_status,
        "page_count": len(pages_diag),
        "summary": {
            "ocr_full_pages": ocr_pages,
            "dual_verify_pages": dual_pages,
            "two_column_pages": [
                p["page"] for p in pages_diag if p["layout_type"] == "two_column"
            ],
        },
        "pages": pages_diag,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Page-level PDF triage diagnostics.")
    parser.add_argument("--pdf", required=True, help="Path to input PDF")
    parser.add_argument("--out", help="Optional output JSON path")
    args = parser.parse_args()

    report = triage_pdf(args.pdf)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
    else:
        print(payload)


if __name__ == "__main__":
    main()
