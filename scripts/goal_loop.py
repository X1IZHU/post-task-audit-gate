#!/usr/bin/env python3
"""Parse creative review state without changing strict-audit semantics."""
from __future__ import annotations
import argparse, json, pathlib, re

import validate_audit_response

GOAL_TYPES = {"XHS_USER_PANEL_REVIEW", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"}
GOAL_STATUSES = {"GOAL_REACHED", "ITERATION_REQUIRED", "MORE_EVIDENCE_REQUIRED", "USER_DECISION_REQUIRED", "BLOCKED"}
REMEDIATION_FIELDS = ("CURRENT_PROBLEM", "KEEP", "DELETE", "REWRITE", "CORE_MESSAGE", "FIRST_SCREEN_GOAL", "BODY_STRUCTURE", "IMAGE_PLAN", "CTA_DIRECTION", "DO_NOT", "ACCEPTANCE_CRITERIA")


def header(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def section(text: str, title: str) -> str:
    match = re.search(rf"^##\s+{re.escape(title)}\s*$([\s\S]*?)(?=^##\s+|\Z)", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def remediation(text: str) -> dict[str, str]:
    body = section(text, "CODEX_REMEDIATION_PROMPT")
    return {field: value for field in REMEDIATION_FIELDS if (value := header(body, field))}


def _validation_errors(text: str, expected_findings: list[str] | None) -> tuple[str | None, list[str]]:
    values, errors = validate_audit_response.exact_header(text)
    audit_type = values.get("AUDIT_TYPE")
    if audit_type in {"XHS_USER_PANEL_REMEDIATION_VERIFICATION"} and expected_findings is None:
        errors.append("goal response is missing authoritative expected findings context")
    errors.extend(validate_audit_response.semantic_errors(text, values, expected_findings or []))
    return audit_type, errors


def plan(
    text: str,
    iteration: int,
    max_iterations: int = 4,
    expected_findings: list[str] | None = None,
) -> dict[str, object]:
    audit_type, validation_errors = _validation_errors(text, expected_findings)
    if validation_errors:
        return {
            "mode_family": "GOAL_LOOP" if audit_type in GOAL_TYPES else "STRICT_AUDIT",
            "terminal": audit_type not in GOAL_TYPES,
            "goal_status": "INVALID_RESPONSE",
            "next_step": "STOP_INVALID_RESPONSE",
            "validation_errors": validation_errors,
            "conversation": "REUSE_SAME_GOAL_CHAT",
            "blind_review_required": True,
            "publication_triggered": False,
        }
    if audit_type not in GOAL_TYPES:
        return {"mode_family": "STRICT_AUDIT", "terminal": True, "reason": "strict audit verdict remains authoritative"}
    verdict, declared = header(text, "VERDICT"), header(text, "GOAL_STATUS")
    guidance = remediation(text)
    status = declared if declared in GOAL_STATUSES else None
    if status is None:
        if verdict == "PASS": status = "GOAL_REACHED"
        elif verdict == "BLOCKED": status = "BLOCKED"
        elif verdict == "FAIL" and guidance and not header(text, "USER_DECISION_REQUIRED"):
            status = "ITERATION_REQUIRED"
        else: status = "BLOCKED"
    if iteration >= max_iterations and status == "ITERATION_REQUIRED": status = "MAX_ITERATIONS_REACHED"
    evidence = header(text, "EVIDENCE_FEASIBILITY")
    next_step = {"GOAL_REACHED": "STOP_READY_FOR_USER_REVIEW", "ITERATION_REQUIRED": "APPLY_REMEDIATION", "MORE_EVIDENCE_REQUIRED": "CREATE_OR_COLLECT_EVIDENCE" if evidence == "LOCAL_GENERATABLE" else "USER_ACTION_REQUIRED", "USER_DECISION_REQUIRED": "ASK_USER", "BLOCKED": "STOP_BLOCKED", "MAX_ITERATIONS_REACHED": "STOP_WITH_BEST_VERSION"}[status]
    return {"mode_family": "GOAL_LOOP", "goal_status": status, "next_step": next_step, "remediation": guidance, "conversation": "REUSE_SAME_GOAL_CHAT", "blind_review_required": True, "publication_triggered": False}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("response")
    ap.add_argument("--iteration", type=int, required=True)
    ap.add_argument("--max-goal-iterations", type=int, default=4)
    args = ap.parse_args()
    response_path = pathlib.Path(args.response)
    text = response_path.read_text(encoding="utf-8")
    values, _ = validate_audit_response.exact_header(text)
    expected_findings = None
    if values.get("AUDIT_TYPE") == "XHS_USER_PANEL_REMEDIATION_VERIFICATION":
        expected_path = response_path.parent / "EXPECTED_PREVIOUS_FINDINGS.json"
        if expected_path.is_file():
            expected_findings, file_errors = validate_audit_response.expected_findings_from_file(expected_path, "XHS")
            if file_errors:
                print(json.dumps({"goal_status": "INVALID_RESPONSE", "next_step": "STOP_INVALID_RESPONSE", "validation_errors": file_errors}, ensure_ascii=False, indent=2))
                return
        else:
            expected_findings = None
    print(json.dumps(plan(text, args.iteration, args.max_goal_iterations, expected_findings), ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
