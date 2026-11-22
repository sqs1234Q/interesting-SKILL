#!/usr/bin/env python3
"""Validate /app/output/result.json against /input/case.json output_contract and evidence_bundle.json."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Set


def _collect_valid_evidence_ids(bundle: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for page in bundle.get("pages", []):
        for b in page.get("blocks", []):
            if b.get("evidence_id"):
                ids.add(str(b["evidence_id"]))
        for t in page.get("tables", []):
            if t.get("evidence_id"):
                ids.add(str(t["evidence_id"]))
        for ob in page.get("ocr_blocks", []):
            if ob.get("evidence_id"):
                ids.add(str(ob["evidence_id"]))
    return ids


def _collect_cited_evidence_ids(obj: Any) -> Set[str]:
    cited: Set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"evidence_id", "source_id"} and isinstance(v, str) and v.strip():
                cited.add(v.strip())
            elif k in {"evidence_ids", "source_ids"} and isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        cited.add(item.strip())
            else:
                cited |= _collect_cited_evidence_ids(v)
    elif isinstance(obj, list):
        for item in obj:
            cited |= _collect_cited_evidence_ids(item)
    return cited


def _extract_required_keys(case_data: Dict[str, Any]) -> List[str]:
    contract = case_data.get("output_contract")
    if isinstance(contract, dict):
        if isinstance(contract.get("required_fields"), list):
            return [str(k) for k in contract["required_fields"]]
        if isinstance(contract.get("required"), list):
            return [str(k) for k in contract["required"]]
        if isinstance(contract.get("properties"), dict):
            return [str(k) for k in contract["properties"].keys()]
        return [str(k) for k in contract.keys()]
    if isinstance(contract, list):
        return [str(k) for k in contract]
    return []


def validate(case_path: str, result_path: str, evidence_path: str | None = None) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    try:
        with open(case_path, "r", encoding="utf-8") as f:
            case_data = json.load(f)
    except Exception as exc:
        return {"valid": False, "errors": [f"Failed to read case.json: {exc}"], "warnings": []}

    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result_data = json.load(f)
    except Exception as exc:
        return {"valid": False, "errors": [f"Failed to read result.json: {exc}"], "warnings": []}

    if not isinstance(result_data, dict):
        return {"valid": False, "errors": ["result.json root must be a JSON object."], "warnings": []}

    expected_case_id = case_data.get("case_id")
    if expected_case_id is not None:
        if result_data.get("case_id") != expected_case_id:
            errors.append(
                f"case_id mismatch: expected {expected_case_id!r}, got {result_data.get('case_id')!r}"
            )

    required_keys = _extract_required_keys(case_data)
    for key in required_keys:
        if key not in result_data:
            errors.append(f"Missing required output_contract field: {key!r}")
        elif result_data[key] is None:
            warnings.append(f"Required field {key!r} is null; verify if intentional.")

    cited_ids = _collect_cited_evidence_ids(result_data)
    if evidence_path:
        try:
            with open(evidence_path, "r", encoding="utf-8") as f:
                bundle = json.load(f)
            valid_ids = _collect_valid_evidence_ids(bundle)
            invalid_ids = sorted(cited_ids - valid_ids)
            if invalid_ids:
                errors.append(f"Cited evidence_id(s) not found in evidence_bundle.json: {invalid_ids}")
            if not cited_ids and valid_ids:
                warnings.append("No evidence_id citations found in result.json; add page/block provenance.")
        except Exception as exc:
            warnings.append(f"Could not verify evidence_bundle.json: {exc}")

    return {
        "valid": len(errors) == 0,
        "checked_required_fields": required_keys,
        "cited_evidence_count": len(cited_ids),
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate result.json against Harbor output_contract.")
    parser.add_argument("--case", required=True, help="Path to /input/case.json")
    parser.add_argument("--result", required=True, help="Path to /app/output/result.json")
    parser.add_argument("--evidence", help="Optional path to evidence_bundle.json")
    args = parser.parse_args()

    report = validate(args.case, args.result, args.evidence)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
