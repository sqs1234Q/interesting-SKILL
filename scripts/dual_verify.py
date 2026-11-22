#!/usr/bin/env python3
"""Targeted dual-channel OCR cross-verification and hallucination circuit-breaker."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from typing import Any, Dict, List, Set, Tuple

# Match standalone numbers while avoiding partial splits of identifiers like arXiv:2504.18493v1
NUM_RE = re.compile(r"(?<![A-Za-z0-9\.])\d+(?:\.\d+)?(?![A-Za-z0-9\.])")
WORD_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")


def _select_tesseract_lang() -> str:
    if not shutil.which("tesseract"):
        return "eng"
    try:
        proc = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        langs = {line.strip() for line in (proc.stdout + "\n" + proc.stderr).splitlines()}
        if "chi_sim" in langs and "eng" in langs:
            return "eng+chi_sim"
        if "eng" in langs:
            return "eng"
    except Exception:
        pass
    return "eng"


def _render_page_png(pdf_path: str, page_num: int, out_png: str, dpi: int = 200) -> str | None:
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_path)
        page = doc[page_num - 1]
        scale = dpi / 72.0
        bitmap = page.render(scale=scale)
        pil_img = bitmap.to_pil()
        pil_img.save(out_png)
        doc.close()
        return out_png
    except Exception:
        pass

    try:
        import fitz

        doc = fitz.open(pdf_path)
        pix = doc[page_num - 1].get_pixmap(dpi=dpi)
        pix.save(out_png)
        doc.close()
        return out_png
    except Exception:
        pass

    if shutil.which("pdftoppm"):
        prefix = out_png[:-4] if out_png.endswith(".png") else out_png
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-r",
                    str(dpi),
                    "-f",
                    str(page_num),
                    "-l",
                    str(page_num),
                    "-singlefile",
                    pdf_path,
                    prefix,
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            candidate = prefix + ".png"
            if os.path.exists(candidate):
                return candidate
        except Exception:
            pass
    return None


def _get_large_image_boxes(pdf_path: str, page_num: int) -> List[Tuple[float, float, float, float]]:
    """Return normalized (0..1) bboxes of large embedded raster images to mask out before digital-page OCR."""
    boxes: List[Tuple[float, float, float, float]] = []
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_num - 1]
            pw = float(page.width or 1.0)
            ph = float(page.height or 1.0)
            p_area = max(pw * ph, 1.0)
            for im in (page.images or []):
                x0 = max(0.0, float(im.get("x0", 0.0)))
                x1 = min(pw, float(im.get("x1", 0.0)))
                top = max(0.0, float(im.get("top", 0.0)))
                bot = min(ph, float(im.get("bottom", 0.0)))
                area = max(0.0, x1 - x0) * max(0.0, bot - top)
                if 0.03 * p_area <= area <= 0.70 * p_area:
                    boxes.append((x0 / pw, top / ph, x1 / pw, bot / ph))
    except Exception:
        pass
    return boxes


def _run_ocr_on_png(
    png_path: str,
    lang: str,
    crop_margins: bool = False,
    mask_boxes: List[Tuple[float, float, float, float]] | None = None,
) -> Dict[str, Any]:
    try:
        import pytesseract
        from PIL import Image, ImageDraw

        img = Image.open(png_path).convert("RGB")
        w, h = img.size
        if mask_boxes:
            draw = ImageDraw.Draw(img)
            for rx0, rtop, rx1, rbot in mask_boxes:
                draw.rectangle([int(rx0 * w), int(rtop * h), int(rx1 * w), int(rbot * h)], fill=(255, 255, 255))
        if crop_margins:
            img = img.crop((0, int(h * 0.08), w, int(h * 0.92)))
        text = pytesseract.image_to_string(img, lang=lang)
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
        confs = [float(c) for c in data.get("conf", []) if str(c).strip() not in {"", "-1"} and float(c) >= 0]
        avg_conf = round(sum(confs) / len(confs), 2) if confs else None
        return {"ok": True, "backend": "pytesseract", "text": text.strip(), "avg_confidence": avg_conf}
    except Exception:
        pass

    if shutil.which("tesseract"):
        try:
            proc = subprocess.run(
                ["tesseract", png_path, "stdout", "-l", lang],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            if proc.returncode == 0:
                return {"ok": True, "backend": "tesseract_cli", "text": proc.stdout.strip(), "avg_confidence": None}
            return {"ok": False, "backend": "tesseract_cli", "text": "", "error": proc.stderr.strip()}
        except Exception as exc:
            return {"ok": False, "backend": "tesseract_cli", "text": "", "error": str(exc)}

    return {"ok": False, "backend": "none", "text": "", "error": "No OCR backend installed"}


def _extract_number_set(text: str) -> Set[str]:
    return set(NUM_RE.findall(text or ""))


def _normalize_tokens(text: str) -> List[str]:
    return [t.lower() for t in WORD_RE.findall(text or "")]


def _token_f1(tok_a: List[str], tok_b: List[str]) -> float:
    if not tok_a and not tok_b:
        return 1.0
    if not tok_a or not tok_b:
        return 0.0
    ca = Counter(tok_a)
    cb = Counter(tok_b)
    overlap = sum((ca & cb).values())
    return round((2.0 * overlap) / (len(tok_a) + len(tok_b)), 3)


def _compare_number_sets(
    nums_a: Set[str], nums_b: Set[str]
) -> Tuple[float, List[str], List[str]]:
    """Compare numeric tokens while tolerating common OCR decimal-drop and LaTeX 10^k artifacts."""
    if not nums_a and not nums_b:
        return 1.0, [], []

    b_nodot = {n.replace(".", "") for n in nums_b}
    a_nodot = {n.replace(".", "") for n in nums_a}

    matched_a: Set[str] = set()
    for a in nums_a:
        if a in nums_b:
            matched_a.add(a)
        elif "." in a and a.replace(".", "") in b_nodot:
            # OCR occasionally drops a decimal point (e.g. 17.1 -> 171)
            matched_a.add(a)
        elif a.startswith("10") and len(a) in {3, 4} and "10" in nums_b:
            # LaTeX scientific notation 10^{19} extracted as 1019 in text layer
            matched_a.add(a)

    matched_b: Set[str] = set()
    for b in nums_b:
        if b in nums_a:
            matched_b.add(b)
        elif b in a_nodot or ("." in b and b.replace(".", "") in a_nodot):
            matched_b.add(b)

    missing_in_ocr = sorted(nums_a - matched_a)[:15]
    only_in_ocr = sorted(nums_b - matched_b)[:15]

    denom = max(len(nums_a) + len(only_in_ocr), 1)
    overlap = round(len(matched_a) / denom, 3)
    return overlap, missing_in_ocr, only_in_ocr


def verify_pages(
    pdf_path: str,
    bundle: Dict[str, Any],
    pages_to_check: List[int],
    render_dir: str,
    dpi: int = 200,
) -> Dict[str, Any]:
    """Run render + OCR on flagged pages and cross-check against text layer."""
    lang = _select_tesseract_lang()
    page_map = {p["page"]: p for p in bundle.get("pages", [])}
    verifications: List[Dict[str, Any]] = []

    for pno in pages_to_check:
        page_obj = page_map.get(pno)
        if not page_obj:
            continue

        png_path = os.path.join(render_dir, f"page_{pno:03d}.png")
        rendered = _render_page_png(pdf_path, pno, png_path, dpi=dpi)
        if not rendered:
            verifications.append(
                {
                    "page": pno,
                    "status": "unreadable_render_failed",
                    "render_path": None,
                    "reason": "Could not render page to PNG with available backends.",
                }
            )
            continue

        has_marginalia = bool(page_obj.get("marginalia"))
        route = page_obj.get("route", "layout_text")
        mask_boxes = _get_large_image_boxes(pdf_path, pno) if route != "ocr_full" else None
        ocr_res = _run_ocr_on_png(rendered, lang=lang, crop_margins=has_marginalia, mask_boxes=mask_boxes)
        if not ocr_res.get("ok"):
            verifications.append(
                {
                    "page": pno,
                    "status": "ocr_unavailable",
                    "render_path": rendered,
                    "reason": ocr_res.get("error", "OCR failed"),
                }
            )
            continue

        ocr_text = ocr_res.get("text", "")
        text_layer_parts = [b["text"] for b in page_obj.get("blocks", [])] + [
            tbl["markdown"] for tbl in page_obj.get("tables", [])
        ]
        text_layer = "\n".join(text_layer_parts).strip()

        route = page_obj.get("route", "layout_text")
        if route == "ocr_full" or len(re.sub(r"\s+", "", text_layer)) < 30:
            status = "ocr_recovered" if len(re.sub(r"\s+", "", ocr_text)) >= 15 else "unreadable"
            if ocr_text:
                page_obj.setdefault("ocr_blocks", []).append(
                    {
                        "evidence_id": f"p{pno}_ocr_b01",
                        "page": pno,
                        "status": status,
                        "avg_confidence": ocr_res.get("avg_confidence"),
                        "render_path": rendered,
                        "text": ocr_text,
                    }
                )
            verifications.append(
                {
                    "page": pno,
                    "route": route,
                    "status": status,
                    "render_path": rendered,
                    "ocr_backend": ocr_res.get("backend"),
                    "ocr_avg_confidence": ocr_res.get("avg_confidence"),
                    "ocr_char_count": len(re.sub(r"\s+", "", ocr_text)),
                    "agreement_score": None,
                    "number_mismatch": False,
                }
            )
            continue

        tok_a = _normalize_tokens(text_layer)
        tok_b = _normalize_tokens(ocr_text)
        seq_agreement = round(difflib.SequenceMatcher(None, tok_a[:800], tok_b[:800]).ratio(), 3)
        bow_f1 = _token_f1(tok_a, tok_b)
        ca, cb = Counter(tok_a), Counter(tok_b)
        text_recall = round(sum((ca & cb).values()) / max(len(tok_a), 1), 3) if tok_a else 1.0
        garbled_flag = "garbled_font_or_cid" in page_obj.get("risk_flags", [])
        # On clean digital pages, OCR often reads extra vector-chart labels; text_recall checks if text layer is confirmed
        agreement = max(seq_agreement, bow_f1) if garbled_flag else max(seq_agreement, bow_f1, text_recall)

        nums_a = _extract_number_set(text_layer)
        nums_b = _extract_number_set(ocr_text)
        num_overlap, missing_in_ocr, only_in_ocr = _compare_number_sets(nums_a, nums_b)
        text_num_recall = round((len(nums_a) - len(missing_in_ocr)) / max(len(nums_a), 1), 3) if nums_a else 1.0
        effective_num_score = num_overlap if garbled_flag else max(num_overlap, text_num_recall)
        num_threshold = 0.70 if garbled_flag else 0.60
        number_mismatch = bool(len(nums_a) >= 3 and effective_num_score < num_threshold)

        if garbled_flag and len(ocr_text) > 30:
            status = "conflict_needs_review"
            page_obj.setdefault("ocr_blocks", []).append(
                {
                    "evidence_id": f"p{pno}_ocr_b01",
                    "page": pno,
                    "status": status,
                    "avg_confidence": ocr_res.get("avg_confidence"),
                    "render_path": rendered,
                    "text": ocr_text,
                }
            )
        elif agreement < 0.65 or number_mismatch:
            status = "conflict_needs_review"
            page_obj.setdefault("ocr_blocks", []).append(
                {
                    "evidence_id": f"p{pno}_ocr_b01",
                    "page": pno,
                    "status": status,
                    "avg_confidence": ocr_res.get("avg_confidence"),
                    "render_path": rendered,
                    "text": ocr_text,
                }
            )
        else:
            status = "verified"

        verifications.append(
            {
                "page": pno,
                "route": route,
                "status": status,
                "render_path": rendered,
                "ocr_backend": ocr_res.get("backend"),
                "ocr_avg_confidence": ocr_res.get("avg_confidence"),
                "agreement_score": agreement,
                "sequence_agreement": seq_agreement,
                "token_f1": bow_f1,
                "text_recall": text_recall,
                "number_overlap": effective_num_score,
                "number_mismatch": number_mismatch,
                "numbers_only_in_text_layer": missing_in_ocr,
                "numbers_only_in_ocr_layer": only_in_ocr,
            }
        )

    return {
        "ocr_lang": lang,
        "pages_checked": pages_to_check,
        "results": verifications,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run targeted dual-channel OCR verification.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--bundle", required=True, help="Path to evidence_bundle.json")
    parser.add_argument("--pages", required=True, help="Comma-separated 1-indexed page numbers")
    parser.add_argument("--render-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    with open(args.bundle, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    pages = [int(x.strip()) for x in args.pages.split(",") if x.strip().isdigit()]
    report = verify_pages(args.pdf, bundle, pages, args.render_dir, dpi=args.dpi)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(args.bundle, "w", encoding="utf-8") as f:
        bundle["dual_verification"] = report
        json.dump(bundle, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
