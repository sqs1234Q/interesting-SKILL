#!/usr/bin/env python3
"""Layout-aware PDF extractor with header/footer stripping, true 2-column reading order, tables, and Evidence IDs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

logging.getLogger("pdfminer").setLevel(logging.ERROR)

NUMBERED_HEADING_RE = re.compile(
    r"^(?:[1-9]\d?(?:\.\d{1,2})*\.?\s+[A-Z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff\-\+\s:/()]{2,85})$"
)
KEYWORD_HEADING_RE = re.compile(
    r"^(?:ABSTRACT|INTRODUCTION|BACKGROUND|METHODS?|MATERIALS\s+AND\s+METHODS|"
    r"EXPERIMENTAL(?:\s+SECTION)?|RESULTS?(?:\s+AND\s+DISCUSSION)?|DISCUSSION|"
    r"CONCLUSION(?:S)?|REFERENCES|ACKNOWLEDGMENTS?|APPENDIX(?:\s+[A-Z0-9]+(?:\..+)?)?|"
    r"AUTHOR\s+DECLARATIONS?|CONFLICT\s+OF\s+INTEREST|AUTHOR\s+CONTRIBUTIONS?|"
    r"CREDIT\s+AUTHORSHIP\s+CONTRIBUTION\s+STATEMENT|DECLARATION\s+OF\s+COMPETING\s+INTEREST|"
    r"DATA\s+AVAILABILITY|SUPPLEMENTARY\s+MATERIAL|ARTICLE\s+INFO|摘要|引言|实验方法|结果与讨论|结论|参考文献)$",
    re.I,
)
CAPTION_RE = re.compile(r"^(?:FIG(?:URE|\.)?|Table|Scheme|Eq(?:uation|\.)?|图|表)\s*\d+", re.I)
FORMULA_NUM_RE = re.compile(r"\((?!19\d\d|20\d\d)\d{1,2}(?:\.\d{1,2})?\)\s*$")
MATH_OP_RE = re.compile(r"[=±×÷≤≥≠≈∞∑∏∫∂∇√]")
DIGITS_RE = re.compile(r"\d+")
PUA_RE = re.compile(r"[\uE000-\uF8FF]+")
SOFT_HYPHEN_RE = re.compile(r"\u00ad")
CID_MINUS_RE = re.compile(r"\(cid:0\)")
CID_SPACE_RE = re.compile(r"\(cid:32\)")


def _clean_text(text: str) -> str:
    text = SOFT_HYPHEN_RE.sub("", text)
    text = PUA_RE.sub("", text)
    # In Elsevier/LaTeX PDFs, (cid:0) commonly encodes minus and (cid:32) encodes space/diacritic gap
    text = CID_MINUS_RE.sub("−", text)
    text = CID_SPACE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_marginalia(text: str) -> str:
    return re.sub(r"\s+", " ", DIGITS_RE.sub("#", text.strip().lower()))


def _bbox_overlaps(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], pad: float = 2.0) -> bool:
    return not (
        a[2] < b[0] - pad
        or a[0] > b[2] + pad
        or a[3] < b[1] - pad
        or a[1] > b[3] + pad
    )


def _make_line_dict(line_words: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not line_words:
        return None
    line_words = sorted(line_words, key=lambda w: w["x0"])
    text = _clean_text(" ".join(w["text"] for w in line_words))
    if not text:
        return None
    sizes = [float(w.get("size", 10.0)) for w in line_words if "size" in w]
    avg_size = round(sum(sizes) / len(sizes), 2) if sizes else 10.0
    bold_hits = sum(
        1 for w in line_words
        if any(tok in str(w.get("fontname", "")).lower() for tok in ("bold", "demi", "heavy", "black", "medi", "-b", "bx"))
    )
    is_bold = bool(line_words and (bold_hits / len(line_words)) >= 0.70)
    return {
        "text": text,
        "x0": round(min(w["x0"] for w in line_words), 2),
        "top": round(min(w["top"] for w in line_words), 2),
        "x1": round(max(w["x1"] for w in line_words), 2),
        "bottom": round(max(w["bottom"] for w in line_words), 2),
        "avg_size": avg_size,
        "is_bold": is_bold,
    }


def _group_words_to_lines(words: List[Dict[str, Any]], y_tol: float = 3.5) -> List[Dict[str, Any]]:
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    bands: List[List[Dict[str, Any]]] = []
    for w in sorted_words:
        if not bands:
            bands.append([w])
            continue
        prev = bands[-1]
        prev_top = sum(x["top"] for x in prev) / len(prev)
        if abs(w["top"] - prev_top) <= y_tol:
            prev.append(w)
        else:
            bands.append([w])

    result: List[Dict[str, Any]] = []
    for band in bands:
        ln = _make_line_dict(band)
        if ln:
            result.append(ln)
    return result


def _is_heading_line(text: str, avg_size: float, median_body_size: float, is_bold: bool = False) -> bool:
    t = text.strip()
    if not t or len(t) > 160:
        return False
    if KEYWORD_HEADING_RE.match(t):
        return True
    if "," in t or t.endswith(".") or t.endswith(";") or t.endswith(":"):
        return False
    if NUMBERED_HEADING_RE.match(t) and len(t.split()) <= 14:
        return True
    if avg_size >= median_body_size + 2.0 and len(t.split()) <= 18 and not CAPTION_RE.match(t):
        return True
    # Nature / Springer unnumbered bold section headings (e.g. 'Results', 'Methods', 'Online content')
    if (
        is_bold
        and avg_size >= median_body_size - 0.5
        and 1 <= len(t.split()) <= 10
        and 4 <= len(t) <= 85
        and t[0].isupper()
        and not CAPTION_RE.match(t)
        and not re.match(r"^(?:Extended\s+Data|Supplementary|Source\s+Data|Reporting\s+Summary)", t, re.I)
    ):
        return True
    return False


def _merge_lines_to_blocks(
    lines: List[Dict[str, Any]],
    col_label: str,
    median_body_size: float,
    line_gap_factor: float = 1.15,
) -> List[Dict[str, Any]]:
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: (l["top"], l["x0"]))
    blocks: List[List[Dict[str, Any]]] = [[lines[0]]]
    for line in lines[1:]:
        prev = blocks[-1][-1]
        prev_h = max(prev["bottom"] - prev["top"], 8.0)
        gap = line["top"] - prev["bottom"]
        is_heading_break = _is_heading_line(line["text"], line["avg_size"], median_body_size, line.get("is_bold", False))
        prev_was_heading = _is_heading_line(prev["text"], prev["avg_size"], median_body_size, prev.get("is_bold", False))
        is_caption_break = bool(CAPTION_RE.match(line["text"]))

        # Merge wrapped multi-line titles (including 3+ line titles) or hyphenated multi-line headings
        same_heading_style = (
            abs(line["avg_size"] - prev["avg_size"]) <= 0.8
            and gap <= prev_h * 1.15
            and not KEYWORD_HEADING_RE.match(line["text"].strip())
            and not NUMBERED_HEADING_RE.match(line["text"].strip())
            and not is_caption_break
        )
        is_wrapped_heading = prev_was_heading and same_heading_style and (
            prev["text"].strip().endswith("-")
            or (
                prev["avg_size"] >= median_body_size + 1.2
                and line["avg_size"] >= median_body_size + 1.2
                and not KEYWORD_HEADING_RE.match(prev["text"].strip())
            )
        )

        if is_wrapped_heading:
            blocks[-1].append(line)
            continue

        # Do not split tiny isolated superscripts/subscripts away from surrounding text if gap is small
        is_tiny_script = len(line["text"]) <= 4 and gap <= prev_h * 0.8
        size_jump = (abs(line["avg_size"] - prev["avg_size"]) >= 2.2) and not is_tiny_script
        if gap > prev_h * line_gap_factor or is_heading_break or prev_was_heading or is_caption_break or size_jump:
            blocks.append([line])
        else:
            blocks[-1].append(line)

    out: List[Dict[str, Any]] = []
    for blk in blocks:
        joined_lines: List[str] = []
        for ln in blk:
            t = ln["text"].strip()
            if joined_lines and joined_lines[-1].endswith("-") and not joined_lines[-1].endswith(" -"):
                joined_lines[-1] = joined_lines[-1][:-1] + t
            else:
                joined_lines.append(t)
        text = " ".join(joined_lines).strip()
        if not text:
            continue
        avg_size = round(sum(l["avg_size"] for l in blk) / len(blk), 2)
        if CAPTION_RE.match(text):
            btype = "caption"
        elif _is_heading_line(text, avg_size, median_body_size, all(l.get("is_bold", False) for l in blk)):
            btype = "heading"
        elif FORMULA_NUM_RE.search(text) and MATH_OP_RE.search(text) and len(text) < 200:
            btype = "formula_line"
        else:
            btype = "body"
        out.append(
            {
                "column": col_label,
                "block_type": btype,
                "avg_font_size": avg_size,
                "bbox": [
                    round(min(l["x0"] for l in blk), 2),
                    round(min(l["top"] for l in blk), 2),
                    round(max(l["x1"] for l in blk), 2),
                    round(max(l["bottom"] for l in blk), 2),
                ],
                "text": text,
            }
        )
    return out


def _table_to_markdown(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    clean_rows = []
    for r in rows:
        padded = [_clean_text((c or "").replace("\n", " ").replace("|", "\\|")) for c in r]
        padded += [""] * (col_count - len(padded))
        clean_rows.append(padded)
    header = clean_rows[0]
    sep = ["---"] * col_count
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for r in clean_rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _expand_collapsed_table_rows(page: Any, tbl: Any, raw_rows: List[List[str | None]]) -> List[List[str | None]]:
    """Expand academic tables whose data rows lack internal horizontal rules (e.g. LaTeX \hline / booktabs)."""
    non_empty = [r for r in raw_rows if any((c or "").strip() for c in r)]
    if not non_empty:
        return []

    # Check whether any row has multiple newline-separated entries across cells
    has_collapsed_multiline = any(
        sum(1 for c in r if c and "\n" in c.strip()) >= 2
        for r in non_empty
    )
    if not has_collapsed_multiline:
        return non_empty

    # Extract exact vertical column boundaries from the detected table cells
    col_xs: List[float] = []
    for cell in (tbl.cells or []):
        if cell and len(cell) == 4:
            col_xs.append(round(float(cell[0]), 2))
            col_xs.append(round(float(cell[2]), 2))
    dedup_xs: List[float] = []
    for x in sorted(col_xs):
        if not dedup_xs or x - dedup_xs[-1] > 2.5:
            dedup_xs.append(x)

    n_cols = len(dedup_xs) - 1
    if n_cols < 2:
        return non_empty

    try:
        x0, top, x1, bottom = (float(v) for v in tbl.bbox)
        # Strictly crop inside tbl.bbox (slight inset vertically to avoid touching captions outside rules)
        crop_box = (
            max(0.0, x0),
            max(0.0, top + 0.5),
            min(float(page.width), x1),
            min(float(page.height), bottom - 0.5),
        )
        raw_words = page.crop(crop_box).extract_words(keep_blank_chars=False, use_text_flow=False) or []
        words = [
            w for w in raw_words
            if ((float(w["top"]) + float(w["bottom"])) / 2.0) >= top + 1.5
            and ((float(w["top"]) + float(w["bottom"])) / 2.0) <= bottom - 1.5
        ]
        if not words:
            return non_empty

        # Group words into physical rows by vertical center (tolerance 4.5pt keeps superscripts/subscripts on the same row)
        words_sorted = sorted(words, key=lambda w: ((float(w["top"]) + float(w["bottom"])) / 2.0, float(w["x0"])))
        row_bands: List[List[Dict[str, Any]]] = []
        for w in words_sorted:
            yc = (float(w["top"]) + float(w["bottom"])) / 2.0
            if not row_bands:
                row_bands.append([w])
                continue
            prev_yc = sum((float(x["top"]) + float(x["bottom"])) / 2.0 for x in row_bands[-1]) / len(row_bands[-1])
            if abs(yc - prev_yc) <= 4.5:
                row_bands[-1].append(w)
            else:
                row_bands.append([w])

        re_rows: List[List[str | None]] = []
        for band in row_bands:
            cells: List[List[Dict[str, Any]]] = [[] for _ in range(n_cols)]
            for w in band:
                wx = (float(w["x0"]) + float(w["x1"])) / 2.0
                col_idx = 0
                for ci in range(n_cols):
                    if dedup_xs[ci] - 2.0 <= wx <= dedup_xs[ci + 1] + 2.0:
                        col_idx = ci
                        break
                    if wx > dedup_xs[ci + 1]:
                        col_idx = min(ci + 1, n_cols - 1)
                cells[col_idx].append(w)
            row_vals = [
                _clean_text(" ".join(w["text"] for w in sorted(cw, key=lambda z: float(z["x0"]))))
                for cw in cells
            ]
            if any(row_vals):
                re_rows.append(row_vals)

        if len(re_rows) > len(non_empty):
            return re_rows
    except Exception:
        pass

    return non_empty



def _is_plausible_table(rows: List[List[str | None]]) -> bool:
    """Filter out chart grids, attention heatmaps, and single-column framed boxes."""
    if len(rows) < 2:
        return False
    col_count = max(len(r) for r in rows)
    if col_count < 2 or col_count > 12:
        return False
    total_cells = len(rows) * col_count
    non_empty_cells = sum(1 for r in rows for c in r if (c or "").strip())
    if total_cells > 0 and (non_empty_cells / total_cells) < 0.35:
        return False
    # Reject grids where almost all cells are single characters (plot ticks/heatmaps)
    cell_lens = [len((c or "").strip()) for r in rows for c in r if (c or "").strip()]
    if col_count >= 8 and cell_lens and (sum(cell_lens) / len(cell_lens)) < 2.2:
        return False
    return True


def _extract_booktabs_tables(
    page: Any,
    existing_bboxes: List[Tuple[float, float, float, float]],
) -> List[Tuple[Tuple[float, float, float, float], List[List[str]]]]:
    """Detect and extract borderless LaTeX booktabs three-line tables (horizontal rules only, no vertical lines)."""
    width = float(page.width or 0.0)
    height = float(page.height or 0.0)
    if width <= 0 or height <= 0:
        return []

    # Collect long horizontal rules in body area
    h_rules: List[Tuple[float, float, float]] = []  # (y, x0, x1)
    for obj in list(page.lines or []) + list(page.rects or []):
        x0 = float(obj.get("x0", 0.0))
        x1 = float(obj.get("x1", 0.0))
        top = float(obj.get("top", 0.0))
        bot = float(obj.get("bottom", 0.0))
        dx = abs(x1 - x0)
        dy = abs(bot - top)
        y = (top + bot) / 2.0
        if dy <= 2.5 and dx >= width * 0.22 and height * 0.08 < y < height * 0.92:
            # Skip rules inside already-extracted tables
            if any(_bbox_overlaps((x0, top, x1, bot), eb, pad=3.0) for eb in existing_bboxes):
                continue
            h_rules.append((round(y, 2), round(min(x0, x1), 2), round(max(x0, x1), 2)))

    if len(h_rules) < 3:
        return []

    # Deduplicate overlapping horizontal rules at nearly identical y
    h_rules.sort()
    dedup_rules: List[Tuple[float, float, float]] = []
    for y, x0, x1 in h_rules:
        if dedup_rules and abs(y - dedup_rules[-1][0]) <= 2.5 and abs(x0 - dedup_rules[-1][1]) <= 15.0:
            prev_y, px0, px1 = dedup_rules[-1]
            dedup_rules[-1] = (prev_y, min(px0, x0), max(px1, x1))
        else:
            dedup_rules.append((y, x0, x1))

    # Group aligned rules (similar x0 and x1) into candidate table spans
    used = [False] * len(dedup_rules)
    extracted: List[Tuple[Tuple[float, float, float, float], List[List[str]]]] = []
    for i in range(len(dedup_rules)):
        if used[i]:
            continue
        y_i, x0_i, x1_i = dedup_rules[i]
        group = [(i, y_i, x0_i, x1_i)]
        for j in range(i + 1, len(dedup_rules)):
            if used[j]:
                continue
            y_j, x0_j, x1_j = dedup_rules[j]
            # Must align horizontally with the top rule and not be too far below previous rule
            if abs(x0_j - x0_i) <= 18.0 and abs(x1_j - x1_i) <= 18.0 and (y_j - group[-1][1]) <= height * 0.42:
                group.append((j, y_j, x0_j, x1_j))
        if len(group) < 3:
            continue

        top_y = group[0][1]
        bot_y = group[-1][1]
        if bot_y - top_y < 24.0 or bot_y - top_y > height * 0.72:
            continue

        tx0 = min(g[2] for g in group)
        tx1 = max(g[3] for g in group)
        bbox = (round(tx0, 2), round(top_y, 2), round(tx1, 2), round(bot_y, 2))
        if any(_bbox_overlaps(bbox, eb, pad=2.0) for eb in existing_bboxes):
            continue

        try:
            crop_box = (max(0.0, tx0 - 2.0), max(0.0, top_y + 1.0), min(width, tx1 + 2.0), min(height, bot_y - 1.0))
            raw_words = page.crop(crop_box).extract_words(keep_blank_chars=False, use_text_flow=False) or []
            words = [
                w for w in raw_words
                if ((float(w["top"]) + float(w["bottom"])) / 2.0) >= top_y + 1.5
                and ((float(w["top"]) + float(w["bottom"])) / 2.0) <= bot_y - 1.5
            ]
            if len(words) < 6:
                continue

            # Group into physical rows
            words_sorted = sorted(words, key=lambda w: ((float(w["top"]) + float(w["bottom"])) / 2.0, float(w["x0"])))
            row_bands: List[List[Dict[str, Any]]] = []
            for w in words_sorted:
                yc = (float(w["top"]) + float(w["bottom"])) / 2.0
                if not row_bands:
                    row_bands.append([w])
                    continue
                prev_yc = sum((float(x["top"]) + float(x["bottom"])) / 2.0 for x in row_bands[-1]) / len(row_bands[-1])
                if abs(yc - prev_yc) <= 4.5:
                    row_bands[-1].append(w)
                else:
                    row_bands.append([w])

            if len(row_bands) < 2:
                continue

            # Infer column split x-positions via horizontal whitespace projection across table words
            x_min = int(tx0)
            x_max = int(tx1) + 1
            if x_max - x_min < 20:
                continue
            occ = [0] * (x_max - x_min + 1)
            for w in words:
                wx0 = max(x_min, int(float(w["x0"])))
                wx1 = min(x_max, int(float(w["x1"])))
                for px in range(wx0, wx1 + 1):
                    occ[px - x_min] += 1

            # Find empty (or <=1-word noise) vertical strips of width >= 5pt
            col_splits: List[float] = [tx0 - 2.0]
            in_gap = False
            gap_start = 0
            for idx_x, cnt in enumerate(occ):
                if cnt == 0:
                    if not in_gap:
                        in_gap = True
                        gap_start = idx_x
                else:
                    if in_gap:
                        gap_len = idx_x - gap_start
                        if gap_len >= 5 and gap_start > 4 and idx_x < len(occ) - 4:
                            col_splits.append(x_min + (gap_start + idx_x) / 2.0)
                        in_gap = False
            col_splits.append(tx1 + 2.0)

            n_cols = len(col_splits) - 1
            if n_cols < 2 or n_cols > 12:
                continue

            grid: List[List[str]] = []
            for band in row_bands:
                cells: List[List[Dict[str, Any]]] = [[] for _ in range(n_cols)]
                for w in band:
                    wx = (float(w["x0"]) + float(w["x1"])) / 2.0
                    ci = 0
                    for k in range(n_cols):
                        if col_splits[k] <= wx <= col_splits[k + 1]:
                            ci = k
                            break
                        if wx > col_splits[k + 1]:
                            ci = min(k + 1, n_cols - 1)
                    cells[ci].append(w)
                row_vals = [
                    _clean_text(" ".join(w["text"] for w in sorted(cw, key=lambda z: float(z["x0"]))))
                    for cw in cells
                ]
                if any(row_vals):
                    grid.append(row_vals)

            if _is_plausible_table(grid):
                for g in group:
                    used[g[0]] = True
                extracted.append((bbox, grid))
        except Exception:
            continue

    return extracted




def _extract_shaded_tables(
    page: Any,
    existing_bboxes: List[Tuple[float, float, float, float]],
) -> List[Tuple[Tuple[float, float, float, float], List[List[str]]]]:
    """Detect and extract Nature/SpringerOpen alternating-row shaded tables without horizontal rules."""
    width = float(page.width or 0.0)
    height = float(page.height or 0.0)
    if width <= 0 or height <= 0:
        return []

    def _is_light_shaded(rc: Dict[str, Any]) -> bool:
        col = rc.get("non_stroking_color")
        if col is None:
            return False
        if isinstance(col, (int, float)):
            return 0.65 <= float(col) <= 0.98
        if isinstance(col, (list, tuple)) and len(col) == 3:
            vals = [float(v) for v in col]
            return max(vals) >= 0.72 and min(vals) <= 0.97 and not all(v >= 0.99 for v in vals)
        if isinstance(col, (list, tuple)) and len(col) == 4:
            # CMYK light tint
            return 0.02 <= sum(float(v) for v in col) <= 0.45
        return False

    bands: List[Tuple[float, float, float, float]] = []
    for rc in (page.rects or []):
        x0 = float(rc.get("x0", 0.0))
        x1 = float(rc.get("x1", 0.0))
        top = float(rc.get("top", 0.0))
        bot = float(rc.get("bottom", 0.0))
        dx = x1 - x0
        dy = bot - top
        if dx >= width * 0.25 and 8.0 <= dy <= 38.0 and height * 0.08 < top and bot < height * 0.92:
            if _is_light_shaded(rc) and not any(_bbox_overlaps((x0, top, x1, bot), eb, pad=2.0) for eb in existing_bboxes):
                bands.append((round(top, 2), round(bot, 2), round(x0, 2), round(x1, 2)))

    if len(bands) < 2:
        return []

    bands.sort()
    used = [False] * len(bands)
    extracted: List[Tuple[Tuple[float, float, float, float], List[List[str]]]] = []
    for i in range(len(bands)):
        if used[i]:
            continue
        t_i, b_i, x0_i, x1_i = bands[i]
        grp = [(i, t_i, b_i, x0_i, x1_i)]
        for j in range(i + 1, len(bands)):
            if used[j]:
                continue
            t_j, b_j, x0_j, x1_j = bands[j]
            if abs(x0_j - x0_i) <= 15.0 and abs(x1_j - x1_i) <= 15.0 and (t_j - grp[-1][2]) <= 45.0:
                grp.append((j, t_j, b_j, x0_j, x1_j))
        if len(grp) < 2:
            continue

        tx0 = min(g[3] for g in grp)
        tx1 = max(g[4] for g in grp)
        # Include 1 unshaded header row above the first shaded band and 1 unshaded row below the last band
        top_y = max(height * 0.08, grp[0][1] - 18.0)
        bot_y = min(height * 0.92, grp[-1][2] + 16.0)
        bbox = (round(tx0, 2), round(top_y, 2), round(tx1, 2), round(bot_y, 2))
        if any(_bbox_overlaps(bbox, eb, pad=2.0) for eb in existing_bboxes):
            continue

        try:
            words = page.crop(bbox).extract_words(keep_blank_chars=False, use_text_flow=False) or []
            if len(words) < 8:
                continue
            words_sorted = sorted(words, key=lambda w: ((float(w["top"]) + float(w["bottom"])) / 2.0, float(w["x0"])))
            row_bands: List[List[Dict[str, Any]]] = []
            for w in words_sorted:
                yc = (float(w["top"]) + float(w["bottom"])) / 2.0
                if not row_bands:
                    row_bands.append([w])
                    continue
                prev_yc = sum((float(x["top"]) + float(x["bottom"])) / 2.0 for x in row_bands[-1]) / len(row_bands[-1])
                if abs(yc - prev_yc) <= 4.5:
                    row_bands[-1].append(w)
                else:
                    row_bands.append([w])
            if len(row_bands) < 3:
                continue

            # Drop caption line if top row starts with 'Table N'
            if CAPTION_RE.match(" ".join(w["text"] for w in sorted(row_bands[0], key=lambda z: float(z["x0"])))):
                row_bands = row_bands[1:]
                if len(row_bands) < 2:
                    continue

            all_tbl_words = [w for rb in row_bands for w in rb]
            x_min, x_max = int(tx0), int(tx1) + 1
            if x_max - x_min < 20:
                continue
            occ = [0] * (x_max - x_min + 1)
            for w in all_tbl_words:
                for px in range(max(x_min, int(float(w["x0"]))), min(x_max, int(float(w["x1"]))) + 1):
                    occ[px - x_min] += 1

            col_splits: List[float] = [tx0 - 2.0]
            in_gap = False
            gap_start = 0
            for idx_x, cnt in enumerate(occ):
                if cnt == 0:
                    if not in_gap:
                        in_gap = True
                        gap_start = idx_x
                else:
                    if in_gap:
                        if (idx_x - gap_start) >= 5 and gap_start > 4 and idx_x < len(occ) - 4:
                            col_splits.append(x_min + (gap_start + idx_x) / 2.0)
                        in_gap = False
            col_splits.append(tx1 + 2.0)
            n_cols = len(col_splits) - 1
            if n_cols < 2 or n_cols > 12:
                continue

            grid: List[List[str]] = []
            for rb in row_bands:
                cells: List[List[Dict[str, Any]]] = [[] for _ in range(n_cols)]
                for w in rb:
                    wx = (float(w["x0"]) + float(w["x1"])) / 2.0
                    ci = 0
                    for k in range(n_cols):
                        if col_splits[k] <= wx <= col_splits[k + 1]:
                            ci = k
                            break
                        if wx > col_splits[k + 1]:
                            ci = min(k + 1, n_cols - 1)
                    cells[ci].append(w)
                row_vals = [_clean_text(" ".join(w["text"] for w in sorted(cw, key=lambda z: float(z["x0"])))) for cw in cells]
                if any(row_vals):
                    grid.append(row_vals)

            if _is_plausible_table(grid):
                for g in grp:
                    used[g[0]] = True
                extracted.append((bbox, grid))
        except Exception:
            continue

    return extracted


def _normalize_small_caps(text: str) -> str:
    """Collapse LaTeX small-caps letter-splitting artifacts like 'R O FORMER' or 'L O RA'."""
    hits = re.findall(r"\b[A-Z]\s+[A-Z]{2,}\b", text)
    if len(hits) >= 1:
        text = re.sub(r"\b([A-Z])\s+([A-Z])\s+([A-Z]{2,})\b", r"\1\2\3", text)
        text = re.sub(r"\b([A-Z])\s+([A-Z]{2,})\b", r"\1\2", text)
        text = re.sub(r"\s+([:;,\.])", r"\1", text)
        text = re.sub(r"([A-Z])\s+-\s*([A-Z])", r"\1-\2", text)
    return text


def _split_bands_two_column(
    words: List[Dict[str, Any]], gutter_x: float, page_width: float
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Split words into horizontal y-bands FIRST, then separate left/right columns across gutter_x without scrambling."""
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    y_bands: List[List[Dict[str, Any]]] = []
    for w in sorted_words:
        if not y_bands:
            y_bands.append([w])
            continue
        prev = y_bands[-1]
        prev_top = sum(x["top"] for x in prev) / len(prev)
        is_tiny_script = len(w.get("text", "").strip()) <= 3 and float(w.get("size", 10.0)) <= 7.8
        tol = 5.5 if is_tiny_script else 3.5
        if abs(w["top"] - prev_top) <= tol:
            prev.append(w)
        else:
            y_bands.append([w])

    # Merge any residual single-token superscript/subscript band into its vertically closest neighbor band
    merged_bands: List[List[Dict[str, Any]]] = []
    for band in y_bands:
        if (
            len(band) == 1
            and len(band[0].get("text", "").strip()) <= 3
            and float(band[0].get("size", 10.0)) <= 7.8
            and merged_bands
        ):
            w0 = band[0]
            prev_top = sum(x["top"] for x in merged_bands[-1]) / len(merged_bands[-1])
            if abs(w0["top"] - prev_top) <= 6.0:
                merged_bands[-1].append(w0)
                continue
        merged_bands.append(band)
    # Also handle a tiny script band that appeared just BEFORE its main line (superscripts sit higher than baseline)
    final_bands: List[List[Dict[str, Any]]] = []
    i = 0
    while i < len(merged_bands):
        band = merged_bands[i]
        if (
            len(band) == 1
            and len(band[0].get("text", "").strip()) <= 3
            and float(band[0].get("size", 10.0)) <= 7.8
            and i + 1 < len(merged_bands)
        ):
            nxt_top = sum(x["top"] for x in merged_bands[i + 1]) / len(merged_bands[i + 1])
            if abs(band[0]["top"] - nxt_top) <= 6.0:
                merged_bands[i + 1].append(band[0])
                i += 1
                continue
        final_bands.append(band)
        i += 1
    y_bands = final_bands

    segments: List[Tuple[str, List[Dict[str, Any]]]] = []
    cur_full: List[Dict[str, Any]] = []
    cur_left: List[Dict[str, Any]] = []
    cur_right: List[Dict[str, Any]] = []

    def flush_full() -> None:
        nonlocal cur_full
        if cur_full:
            segments.append(("full", cur_full))
            cur_full = []

    def flush_columns() -> None:
        nonlocal cur_left, cur_right
        if cur_left:
            segments.append(("left", cur_left))
            cur_left = []
        if cur_right:
            segments.append(("right", cur_right))
            cur_right = []

    local_gutter: float | None = None
    for band in y_bands:
        band.sort(key=lambda w: w["x0"])
        band_txt_compact = re.sub(r"\s+", "", "".join(w["text"] for w in band)).upper()
        # Detect Elsevier-style asymmetric two-column header (ARTICLE INFO | ABSTRACT)
        if "ARTICLEINFO" in band_txt_compact and "ABSTRACT" in band_txt_compact:
            best_g = None
            best_gap = 0.0
            for a, b in zip(band, band[1:]):
                g = b["x0"] - a["x1"]
                mid = (a["x1"] + b["x0"]) / 2.0
                if g >= 20.0 and page_width * 0.22 <= mid <= page_width * 0.48 and g > best_gap:
                    best_gap = g
                    best_g = mid
            if best_g is not None:
                flush_full()
                flush_columns()
                local_gutter = best_g
        elif local_gutter is not None:
            # Exit asymmetric abstract region once we hit a normal centered gutter or numbered section heading
            if re.match(r"^(?:1\.?\s+INTRODUCTION|I\.?\s+INTRODUCTION)", " ".join(w["text"] for w in band), re.I):
                flush_full()
                flush_columns()
                local_gutter = None

        active_gutter = local_gutter if local_gutter is not None else gutter_x
        word_crosses_gutter = any(w["x0"] < active_gutter - 1.5 and w["x1"] > active_gutter + 1.5 for w in band)
        if word_crosses_gutter:
            flush_columns()
            f_ln = _make_line_dict(band)
            if f_ln:
                cur_full.append(f_ln)
            continue

        left_w = [w for w in band if (w["x0"] + w["x1"]) / 2.0 < active_gutter]
        right_w = [w for w in band if (w["x0"] + w["x1"]) / 2.0 >= active_gutter]

        if left_w and right_w:
            # Check whether there is a real column gutter gap (>= 6.5pt) between left_w and right_w
            max_left_x1 = max(w["x1"] for w in left_w)
            min_right_x0 = min(w["x0"] for w in right_w)
            gap = min_right_x0 - max_left_x1
            if gap >= 6.5:
                flush_full()
                l_ln = _make_line_dict(left_w)
                r_ln = _make_line_dict(right_w)
                if l_ln:
                    cur_left.append(l_ln)
                if r_ln:
                    cur_right.append(r_ln)
            else:
                # Continuous full-width line crossing the gutter (title, abstract, figure caption)
                flush_columns()
                f_ln = _make_line_dict(band)
                if f_ln:
                    cur_full.append(f_ln)
        elif left_w:
            # Check if this is a continuation of a full-width block or a left-column line
            if cur_full and not cur_left and not cur_right:
                prev_full = cur_full[-1]
                prev_h = max(prev_full["bottom"] - prev_full["top"], 8.0)
                l_ln = _make_line_dict(left_w)
                if l_ln and (l_ln["top"] - prev_full["bottom"]) <= prev_h * 1.15 and abs(l_ln["x0"] - prev_full["x0"]) <= 20.0:
                    cur_full.append(l_ln)
                    continue
            flush_full()
            l_ln = _make_line_dict(left_w)
            if l_ln:
                cur_left.append(l_ln)
        elif right_w:
            flush_full()
            r_ln = _make_line_dict(right_w)
            if r_ln:
                cur_right.append(r_ln)

    flush_full()
    flush_columns()
    return segments


def extract_document_layout(pdf_path: str, triage_report: Dict[str, Any]) -> Dict[str, Any]:
    """Extract layout-ordered blocks, tables, marginalia, and outline with Evidence IDs."""
    if triage_report.get("document_status") in {"missing_file", "encrypted", "corrupted", "empty", "backend_missing"}:
        return {
            "pdf_path": pdf_path,
            "document_status": triage_report["document_status"],
            "error": triage_report.get("error"),
            "page_count": 0,
            "section_outline": [],
            "pages": [],
        }

    import pdfplumber

    triage_by_page = {p["page"]: p for p in triage_report.get("pages", [])}
    pages_out: List[Dict[str, Any]] = []
    section_outline: List[Dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        header_counter: Counter[str] = Counter()
        footer_counter: Counter[str] = Counter()
        page_margin_lines: Dict[int, Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = {}
        all_body_sizes: List[float] = []

        for idx, page in enumerate(pdf.pages, start=1):
            h = float(page.height or 1.0)
            words = page.extract_words(keep_blank_chars=False, extra_attrs=["size", "fontname"]) or []
            top_words = [w for w in words if w["bottom"] <= h * 0.08]
            bot_words = [w for w in words if w["top"] >= h * 0.92]
            for w in words:
                if h * 0.08 < w["top"] < h * 0.92 and "size" in w:
                    all_body_sizes.append(round(float(w["size"]), 1))
            top_lines = _group_words_to_lines(top_words)
            bot_lines = _group_words_to_lines(bot_words)
            page_margin_lines[idx] = (top_lines, bot_lines)
            for l in top_lines:
                header_counter[_normalize_marginalia(l["text"])] += 1
            for l in bot_lines:
                footer_counter[_normalize_marginalia(l["text"])] += 1

        median_body_size = (
            sorted(all_body_sizes)[len(all_body_sizes) // 2] if all_body_sizes else 10.0
        )
        min_repeat = 2 if len(pdf.pages) >= 2 else 999
        repeated_headers = {k for k, v in header_counter.items() if v >= min_repeat and k}
        repeated_footers = {k for k, v in footer_counter.items() if v >= min_repeat and k}

        prev_page_ended_with_table = False

        for idx, page in enumerate(pdf.pages, start=1):
            p_diag = triage_by_page.get(idx, {})
            width = float(page.width or 0.0)
            height = float(page.height or 0.0)
            layout_type = p_diag.get("layout_type", "single_column")
            gutter_x = p_diag.get("gutter_x")

            tables_out: List[Dict[str, Any]] = []
            table_bboxes: List[Tuple[float, float, float, float]] = []
            found_tables = page.find_tables() or []
            tbl_counter = 0
            for tbl in found_tables:
                rows = tbl.extract() or []
                non_empty_rows = _expand_collapsed_table_rows(page, tbl, rows)
                if not _is_plausible_table(non_empty_rows):
                    continue
                tbl_counter += 1
                bbox = tuple(round(float(v), 2) for v in tbl.bbox)
                table_bboxes.append(bbox)
                ev_id = f"p{idx}_tbl{tbl_counter:02d}"
                possible_cont = bool(prev_page_ended_with_table and bbox[1] < height * 0.22)
                tables_out.append(
                    {
                        "evidence_id": ev_id,
                        "page": idx,
                        "bbox": list(bbox),
                        "row_count": len(non_empty_rows),
                        "col_count": max(len(r) for r in non_empty_rows),
                        "possible_continuation": possible_cont,
                        "rows": [[_clean_text((c or "").replace("\n", " ")) for c in r] for r in non_empty_rows],
                        "markdown": _table_to_markdown(non_empty_rows),
                    }
                )
            # Fallback for pure borderless LaTeX booktabs three-line tables
            for bt_bbox, bt_rows in _extract_booktabs_tables(page, table_bboxes):
                tbl_counter += 1
                table_bboxes.append(bt_bbox)
                ev_id = f"p{idx}_tbl{tbl_counter:02d}"
                possible_cont = bool(prev_page_ended_with_table and bt_bbox[1] < height * 0.22)
                tables_out.append(
                    {
                        "evidence_id": ev_id,
                        "page": idx,
                        "bbox": list(bt_bbox),
                        "row_count": len(bt_rows),
                        "col_count": max(len(r) for r in bt_rows),
                        "possible_continuation": possible_cont,
                        "rows": bt_rows,
                        "markdown": _table_to_markdown(bt_rows),
                    }
                )
            # Fallback for Nature/SpringerOpen alternating-row shaded tables
            for sh_bbox, sh_rows in _extract_shaded_tables(page, table_bboxes):
                tbl_counter += 1
                table_bboxes.append(sh_bbox)
                ev_id = f"p{idx}_tbl{tbl_counter:02d}"
                possible_cont = bool(prev_page_ended_with_table and sh_bbox[1] < height * 0.22)
                tables_out.append(
                    {
                        "evidence_id": ev_id,
                        "page": idx,
                        "bbox": list(sh_bbox),
                        "row_count": len(sh_rows),
                        "col_count": max(len(r) for r in sh_rows),
                        "possible_continuation": possible_cont,
                        "rows": sh_rows,
                        "markdown": _table_to_markdown(sh_rows),
                    }
                )
            prev_page_ended_with_table = any(b[3] > height * 0.78 for b in table_bboxes)

            words = page.extract_words(keep_blank_chars=False, extra_attrs=["size", "fontname"]) or []

            marginalia: List[Dict[str, Any]] = []
            body_words: List[Dict[str, Any]] = []
            top_lines, bot_lines = page_margin_lines.get(idx, ([], []))
            margin_bboxes: List[Tuple[float, float, float, float]] = []
            for l in top_lines:
                norm = _normalize_marginalia(l["text"])
                if norm in repeated_headers or re.fullmatch(r"\d+(?:/\d+)?", l["text"].strip()):
                    marginalia.append({"kind": "header", "text": l["text"], "bbox": [l["x0"], l["top"], l["x1"], l["bottom"]]})
                    margin_bboxes.append((l["x0"], l["top"], l["x1"], l["bottom"]))
            for l in bot_lines:
                norm = _normalize_marginalia(l["text"])
                if norm in repeated_footers or re.fullmatch(r"\d+(?:/\d+)?", l["text"].strip()):
                    marginalia.append({"kind": "footer", "text": l["text"], "bbox": [l["x0"], l["top"], l["x1"], l["bottom"]]})
                    margin_bboxes.append((l["x0"], l["top"], l["x1"], l["bottom"]))

            for w in words:
                wb = (float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"]))
                if any(_bbox_overlaps(wb, mb, pad=1.0) for mb in margin_bboxes):
                    continue
                if any(_bbox_overlaps(wb, tb, pad=1.0) for tb in table_bboxes):
                    continue
                body_words.append(w)

            blocks_raw: List[Dict[str, Any]] = []
            if layout_type == "two_column" and gutter_x:
                segments = _split_bands_two_column(body_words, float(gutter_x), width)
                for col_label, seg_lines in segments:
                    blocks_raw.extend(_merge_lines_to_blocks(seg_lines, col_label, median_body_size))
            else:
                lines = _group_words_to_lines(body_words)
                blocks_raw.extend(_merge_lines_to_blocks(lines, "full", median_body_size))

            # Promote largest top block on page 1 to title (skip journal mastheads and author lists)
            if idx == 1 and blocks_raw:
                masthead_re = re.compile(
                    r"(?:Journal|Letters|Communications|Proceedings|Transactions|ScienceDirect|Elsevier|Springer|Wiley)",
                    re.I,
                )
                title_candidates = [
                    (i, b) for i, b in enumerate(blocks_raw[:8])
                    if b["avg_font_size"] >= median_body_size + 2.0
                    and 12 <= len(b["text"]) < 320
                    and not (b["bbox"][1] < height * 0.18 and masthead_re.search(b["text"]) and len(b["text"]) < 60)
                ]
                if not title_candidates:
                    title_candidates = [
                        (i, b) for i, b in enumerate(blocks_raw[:6])
                        if b["avg_font_size"] >= median_body_size + 1.1
                        and 15 <= len(b["text"]) < 320
                        and not (b["bbox"][1] < height * 0.18 and masthead_re.search(b["text"]) and len(b["text"]) < 60)
                    ]
                if title_candidates:
                    best_i, best = max(
                        title_candidates,
                        key=lambda pair: (
                            round(pair[1]["avg_font_size"], 1),
                            pair[1]["text"].count(",") <= 1,
                            -pair[0],
                        ),
                    )
                    best["block_type"] = "title"
                    best["text"] = _normalize_small_caps(best["text"])

            blocks_out: List[Dict[str, Any]] = []
            for b_i, b in enumerate(blocks_raw, start=1):
                col_tag = {"full": "full", "left": "col1", "right": "col2"}.get(b["column"], "b")
                ev_id = f"p{idx}_{col_tag}_b{b_i:02d}"
                rec = {
                    "evidence_id": ev_id,
                    "page": idx,
                    "column": b["column"],
                    "block_type": b["block_type"],
                    "avg_font_size": b["avg_font_size"],
                    "bbox": b["bbox"],
                    "text": b["text"],
                }
                blocks_out.append(rec)
                if b["block_type"] in {"title", "heading"}:
                    section_outline.append(
                        {
                            "evidence_id": ev_id,
                            "page": idx,
                            "block_type": b["block_type"],
                            "text": b["text"],
                        }
                    )

            pages_out.append(
                {
                    "page": idx,
                    "layout_type": layout_type,
                    "route": p_diag.get("route", "layout_text"),
                    "risk_flags": p_diag.get("risk_flags", []),
                    "marginalia": marginalia,
                    "blocks": blocks_out,
                    "tables": tables_out,
                }
            )

    return {
        "pdf_path": pdf_path,
        "document_status": triage_report.get("document_status", "ok"),
        "page_count": len(pages_out),
        "median_body_font_size": median_body_size,
        "section_outline": section_outline,
        "pages": pages_out,
    }


def render_structured_markdown(bundle: Dict[str, Any]) -> str:
    """Render a clean, evidence-anchored Markdown view for fast LLM reading."""
    lines: List[str] = [
        f"# Structured Document Extract (`{os.path.basename(bundle.get('pdf_path', ''))}`)",
        f"- Document status: `{bundle.get('document_status')}` | Pages: `{bundle.get('page_count')}`",
        "",
    ]
    for page in bundle.get("pages", []):
        pno = page["page"]
        flags = ", ".join(page.get("risk_flags", [])) or "none"
        lines.append(f"## Page {pno} (`layout={page.get('layout_type')}`, `route={page.get('route')}`, `risks={flags}`)")
        lines.append("")
        for blk in page.get("blocks", []):
            ev = blk["evidence_id"]
            btype = blk["block_type"]
            txt = blk["text"]
            if btype == "title":
                lines.append(f"### {txt} `[{ev} | p.{pno}]`")
            elif btype == "heading":
                lines.append(f"#### {txt} `[{ev} | p.{pno}]`")
            else:
                lines.append(f"- `[{ev} | p.{pno} | {btype}]` {txt}")
        for tbl in page.get("tables", []):
            cont = " (possible continuation)" if tbl.get("possible_continuation") else ""
            lines.append("")
            lines.append(f"**Table `[{tbl['evidence_id']} | p.{pno}]`{cont}**")
            lines.append("")
            lines.append(tbl["markdown"])
        ocr_blocks = page.get("ocr_blocks", [])
        if ocr_blocks:
            lines.append("")
            lines.append(f"**OCR Channel (`p.{pno}`)**")
            for ob in ocr_blocks:
                lines.append(f"- `[{ob['evidence_id']} | p.{pno} | {ob['status']}]` {ob['text']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract layout-aware structure from PDF.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--triage", required=True, help="Path to triage_report.json")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args()

    with open(args.triage, "r", encoding="utf-8") as f:
        triage_report = json.load(f)

    bundle = extract_document_layout(args.pdf, triage_report)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(render_structured_markdown(bundle))


if __name__ == "__main__":
    main()
