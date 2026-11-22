#!/usr/bin/env python3
"""Probe available PDF, rendering, OCR, and scientific Python backends."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from typing import Any, Dict, List


PYTHON_MODULES = [
    "pdfplumber",
    "fitz",
    "pypdf",
    "pypdfium2",
    "PIL",
    "pytesseract",
    "numpy",
]

CLI_TOOLS = [
    "pdftotext",
    "pdftoppm",
    "pdfinfo",
    "pdffonts",
    "tesseract",
]


def _get_tesseract_langs() -> List[str]:
    if not shutil.which("tesseract"):
        return []
    try:
        proc = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        lines = [line.strip() for line in (proc.stdout + "\n" + proc.stderr).splitlines()]
        langs = [
            line
            for line in lines
            if line and not line.lower().startswith("list of available") and " " not in line
        ]
        return sorted(set(langs))
    except Exception:
        return []


def probe_environment() -> Dict[str, Any]:
    """Return available Python modules, CLI tools, and OCR languages."""
    py_status: Dict[str, bool] = {}
    for mod in PYTHON_MODULES:
        try:
            importlib.import_module(mod)
            py_status[mod] = True
        except Exception:
            py_status[mod] = False

    cli_status: Dict[str, str | None] = {
        tool: shutil.which(tool) for tool in CLI_TOOLS
    }

    tesseract_langs = _get_tesseract_langs()

    return {
        "python_modules": py_status,
        "cli_tools": cli_status,
        "tesseract_langs": tesseract_langs,
        "recommended_text_backend": (
            "pdfplumber"
            if py_status["pdfplumber"]
            else "fitz"
            if py_status["fitz"]
            else "pypdf"
            if py_status["pypdf"]
            else "pdftotext"
            if cli_status["pdftotext"]
            else "none"
        ),
        "recommended_render_backend": (
            "pypdfium2"
            if py_status["pypdfium2"]
            else "fitz"
            if py_status["fitz"]
            else "pdftoppm"
            if cli_status["pdftoppm"]
            else "none"
        ),
        "ocr_available": bool(
            (py_status["pytesseract"] and py_status["PIL"] and cli_status["tesseract"])
            or cli_status["tesseract"]
        ),
    }


def main() -> None:
    print(json.dumps(probe_environment(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
