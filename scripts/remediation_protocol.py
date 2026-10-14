#!/usr/bin/env python3
"""Small, persistent gates for audit-finding freeze and master-prompt execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any

import audit_integrity
import validate_audit_response as response_validator

PROTOCOL_VERSION = 1
PLANNING_GATES = (
    "FINDINGS_FROZEN",
    "CROSS_FINDING_ANALYSIS",
    "ROOT_CAUSE_ANALYSIS",
    "REMEDIATION_ORDER",
    "REGRESSION_ANALYSIS",
    "ARCHITECTURE_CONSTRAINT_ANALYSIS",
    "TEST_MATRIX",
)
MASTER_SECTIONS = (
    "0. EXECUTION ROLE",
    "1. SOURCE AUDIT IDENTITY",
    "2. CURRENT VERIFIED STATE",
    "3. GLOBAL REMEDIATION OBJECTIVE",
    "4. FINDINGS IN SCOPE",
    "5. FINDINGS OUT OF SCOPE / ALREADY CLOSED",
    "6. CROSS-FINDING RELATIONSHIP",
    "7. ARCHITECTURE / INVARIANTS TO PRESERVE",
    "8. GLOBAL IMPLEMENTATION STRATEGY",
    "9. ORDER OF OPERATIONS",
    "10. WORKSTREAMS",
    "11. CROSS-FINDING INTEGRATION",
    "12. ADVERSARIAL GENERALIZATION",
    "13. COMPLETE TEST MATRIX",
    "14. LOCAL VERIFICATION PLAN",
    "15. SAFE EXECUTION COMMANDS",
    "16. REMEDIATION REPORT REQUIREMENTS",
    "17. STOP CONDITIONS",
    "18. COMPLETION CONDITIONS",
)
PHASES = (
    "AUDIT_RESPONSE_COMPLETE",
    "AUDIT_FINDINGS_FROZEN",
    "CROSS_FINDING_ANALYSIS",
    "ROOT_CAUSE_SYNTHESIS",
    "REMEDIATION_ORDER_DESIGN",
    "REGRESSION_ANALYSIS",
    "ARCHITECTURE_CONSTRAINT_ANALYSIS",
    "TEST_MATRIX_DESIGN",
    "REMEDIATION_STRATEGY_SYNTHESIS",
    "REMEDIATION_MASTER_PROMPT",
    "MASTER_PROMPT_QUALITY_GATE",
    "LOCAL_REMEDIATION_FROM_MASTER_PROMPT",
    "LOCAL_VERIFICATION",
    "REMEDIATION_REPORT",
    "REMEDIATION_VERIFICATION",
    "COMPLETE",
)
PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}


def response_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def master_prompt_id(subject_id: str, response_hash: str) -> str:
    payload = {"protocol_version": PROTOCOL_VERSION, "subject_id": subject_id, "response_sha256": response_hash}
    return "MP-" + hashlib.sha256(canonical(payload)).hexdigest()[:32]


def _load_expected(response_path: pathlib.Path, values: dict[str, str]) -> tuple[list[str], list[str]]:
    audit_type = values.get("AUDIT_TYPE")
    if audit_type not in {"REMEDIATION_VERIFICATION", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"}:
        return [], []
    return response_validator.resolve_expected_findings(response_path, audit_type, [], None)


def actionable_ids(text: str, expected_findings: list[str] | None = None) -> list[str]:
    values, _ = response_validator.exact_header(text)
    if values.get("AUDIT_TYPE", "").startswith("XHS_"):
        ids = response_validator.xhs_finding_ids(text)
        if expected_findings:
            states, _ = response_validator.xhs_prior_closure_states(text, expected_findings)
            ids.extend(item for item, state in states.items() if state != "CLOSED")
        return sorted(set(ids))
    ids = response_validator.actionable_finding_ids(text)
    if expected_findings:
        states, _ = response_validator.prior_closure_states(text, expected_findings)
        ids.extend(item for item, state in states.items() if state != "CLOSED")
    return sorted(set(ids))


def _response_errors(path: pathlib.Path) -> tuple[dict[str, str], list[str], str]:
    text = path.read_text(encoding="utf-8")
    values, errors = response_validator.exact_header(text)
    expected, expected_errors = _load_expected(path, values)
    errors.extend(expected_errors)
    errors.extend(response_validator.semantic_errors(text, values, expected, path.parent))
    return values, errors, text


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _record_state(path: pathlib.Path, phase: str, **updates: Any) -> None:
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    supplied_history = updates.pop("phase_history", None)
    history = supplied_history if supplied_history is not None else state.get("phase_history", [])
    if not isinstance(history, list):
        raise ValueError("remediation phase history is invalid")
    if phase not in history:
        history.append(phase)
    state.update(updates)
    state.update({"remediation_phase": phase, "phase_history": history})
    _atomic_json(path, state)


def advance_phase(state_path: pathlib.Path, phase: str) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown remediation phase: {phase}")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    current = state.get("remediation_phase")
    if current in PHASE_INDEX and PHASE_INDEX[phase] < PHASE_INDEX[current]:
        raise ValueError(f"remediation phase cannot move backwards: {current} -> {phase}")
    history = state.get("phase_history", [])
    if not isinstance(history, list):
        raise ValueError("remediation phase history is invalid")
    if phase not in history:
        history.append(phase)
    state.update({"remediation_phase": phase, "phase_history": history})
    _atomic_json(state_path, state)
    return state


def freeze_findings(response_path: pathlib.Path, freeze_path: pathlib.Path, state_path: pathlib.Path | None = None) -> dict[str, Any]:
    values, errors, text = _response_errors(response_path)
    if errors:
        raise ValueError("cannot freeze invalid audit response:\n- " + "\n- ".join(errors))
    verdict = values["VERDICT"]
    finding_ids = actionable_ids(text, _load_expected(response_path, values)[0])
    record = {
        "freeze_protocol_version": PROTOCOL_VERSION,
        "state": "FROZEN",
        "handoff_id": values["HANDOFF_ID"],
        "round": int(values["ROUND"]),
        "audit_type": values["AUDIT_TYPE"],
        "audit_subject_id": values["AUDIT_SUBJECT_ID"],
        "verdict": verdict,
        "response_sha256": response_sha256(text),
        "finding_ids": finding_ids,
        "master_prompt_required": verdict in {"FAIL", "PASS_WITH_NOTES"} and bool(finding_ids),
        "planning_gates": {gate: gate == "FINDINGS_FROZEN" for gate in PLANNING_GATES},
    }
    if freeze_path.exists():
        try:
            existing = json.loads(freeze_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"existing findings freeze is unreadable: {exc}") from exc
        if existing != record:
            raise ValueError("AUDIT_FINDINGS_FROZEN is immutable; response or findings changed")
    else:
        _atomic_json(freeze_path, record)
    if state_path:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        history = state.get("phase_history", [])
        if not isinstance(history, list):
            raise ValueError("remediation phase history is invalid")
        for phase in ("AUDIT_RESPONSE_COMPLETE", "AUDIT_FINDINGS_FROZEN"):
            if phase not in history:
                history.append(phase)
        state.update({
            "remediation_phase": "AUDIT_FINDINGS_FROZEN",
            "phase_history": history,
            "findings_frozen": True,
            "frozen_response_sha256": record["response_sha256"],
            "frozen_finding_ids": finding_ids,
            "master_prompt_required": record["master_prompt_required"],
        })
        _atomic_json(state_path, state)
    return record


def _field(text: str, name: str) -> list[str]:
    return re.findall(rf"^{re.escape(name)}:\s*(.+?)\s*$", text, re.MULTILINE)


def _section_body(text: str, title: str) -> str:
    match = re.search(rf"^##\s+{re.escape(title)}\s*$", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.end():]
    boundary = re.search(r"^##\s+.+$", tail, re.MULTILINE)
    return tail[:boundary.start()] if boundary else tail


def validate_master_prompt(
    master_path: pathlib.Path,
    response_path: pathlib.Path,
    freeze_path: pathlib.Path | None = None,
    expected_subject_id: str | None = None,
    state_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    master = master_path.read_text(encoding="utf-8")
    values, errors, response = _response_errors(response_path)
    if errors:
        return {"valid": False, "errors": [f"source audit: {error}" for error in errors]}
    source_ids = actionable_ids(response, _load_expected(response_path, values)[0])
    if values.get("VERDICT") not in {"FAIL", "PASS_WITH_NOTES"} or not source_ids:
        return {"valid": False, "errors": ["source audit has no actionable remediation findings"]}
    expected_subject = expected_subject_id or values.get("AUDIT_SUBJECT_ID")
    errors = []
    required_fields = {
        "REMEDIATION_PROTOCOL_VERSION": "1",
        "HANDOFF_ID": values.get("HANDOFF_ID"),
        "SOURCE_AUDIT_ROUND": values.get("ROUND"),
        "SOURCE_AUDIT_TYPE": values.get("AUDIT_TYPE"),
        "SOURCE_AUDIT_SUBJECT_ID": expected_subject,
        "SOURCE_AUDIT_VERDICT": values.get("VERDICT"),
        "SOURCE_AUDIT_RESPONSE_HASH": response_sha256(response),
    }
    for name, expected in required_fields.items():
        matches = _field(master, name)
        if len(matches) != 1:
            errors.append(f"master prompt field {name} must occur exactly once")
        elif matches[0] != str(expected):
            errors.append(f"master prompt field {name} does not match source audit")
    ids = _field(master, "MASTER_PROMPT_ID")
    expected_master_id = master_prompt_id(expected_subject or "", response_sha256(response))
    if ids != [expected_master_id]:
        errors.append("MASTER_PROMPT_ID is missing or not bound to source audit")
    for gate in PLANNING_GATES:
        if _field(master, gate) != ["TRUE" if gate == "FINDINGS_FROZEN" else "COMPLETE"]:
            errors.append(f"planning gate {gate} is missing or incomplete")
    for title in MASTER_SECTIONS:
        if len(re.findall(rf"^##\s+{re.escape(title)}\s*$", master, re.MULTILINE)) != 1:
            errors.append(f"master prompt section is missing or duplicated: {title}")
    workstreams = _section_body(master, "10. WORKSTREAMS")
    workstream_ids = re.findall(r"^###\s+((?:PTA|XHS)-\d{3,})\b", workstreams, re.MULTILINE)
    if len(workstream_ids) != len(set(workstream_ids)):
        errors.append("master prompt has duplicate workstream finding IDs")
    if set(workstream_ids) != set(source_ids):
        errors.append(f"master prompt finding coverage mismatch: expected {source_ids}, got {sorted(set(workstream_ids))}")
    result = {
        "valid": not errors,
        "errors": errors,
        "source_audit_subject_id": expected_subject,
        "source_audit_response_hash": response_sha256(response),
        "master_prompt_id": expected_master_id,
        "finding_ids": source_ids,
    }
    if not errors:
        if not freeze_path or not freeze_path.is_file():
            result["valid"] = False
            result["errors"] = ["findings freeze is missing"]
        else:
            frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
            if frozen.get("response_sha256") != result["source_audit_response_hash"] or frozen.get("finding_ids") != source_ids:
                result["valid"] = False
                result["errors"] = ["master prompt does not match immutable findings freeze"]
    if result.get("valid") and state_path:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        history = state.get("phase_history", [])
        if not isinstance(history, list):
            history = []
        for phase in PHASES[1:PHASE_INDEX["MASTER_PROMPT_QUALITY_GATE"] + 1]:
            if phase not in history:
                history.append(phase)
        _record_state(
            state_path,
            "MASTER_PROMPT_QUALITY_GATE",
            phase_history=history,
            findings_frozen=True,
            planning_gates={gate: True for gate in PLANNING_GATES},
            master_prompt_id=result["master_prompt_id"],
            source_audit_subject_id=result["source_audit_subject_id"],
            source_audit_response_hash=result["source_audit_response_hash"],
            local_remediation_allowed=True,
        )
    return result


def local_remediation_gate(response_path: pathlib.Path, master_path: pathlib.Path | None, freeze_path: pathlib.Path | None = None, state_path: pathlib.Path | None = None) -> dict[str, Any]:
    values, errors, _ = _response_errors(response_path)
    if errors:
        return {"allowed": False, "errors": errors}
    ids = actionable_ids(response_path.read_text(encoding="utf-8"), _load_expected(response_path, values)[0])
    if values.get("VERDICT") == "PASS":
        return {"allowed": False, "master_prompt_required": False, "finding_ids": ids, "errors": []}
    if values.get("VERDICT") == "BLOCKED":
        return {"allowed": False, "master_prompt_required": False, "finding_ids": ids, "errors": ["BLOCKED enters evidence expansion, not remediation"]}
    if not master_path:
        return {"allowed": False, "master_prompt_required": True, "finding_ids": ids, "errors": ["actionable audit failure requires a remediation master prompt"]}
    result = validate_master_prompt(master_path, response_path, freeze_path, state_path=state_path)
    result["allowed"] = bool(result.get("valid"))
    result["master_prompt_required"] = True
    return result


def report_errors(report_path: pathlib.Path, response_path: pathlib.Path, master_path: pathlib.Path) -> list[str]:
    report = report_path.read_text(encoding="utf-8")
    master = validate_master_prompt(master_path, response_path, response_path.parent / "FINDINGS_FREEZE.json")
    if not master.get("valid"):
        return ["master prompt is invalid"] + list(master.get("errors", []))
    values, _, response = _response_errors(response_path)
    errors = []
    required = {
        "SOURCE_AUDIT_SUBJECT_ID": values.get("AUDIT_SUBJECT_ID"),
        "SOURCE_AUDIT_RESPONSE_HASH": response_sha256(response),
        "SOURCE_MASTER_PROMPT_ID": master["master_prompt_id"],
    }
    for name, expected in required.items():
        if _field(report, name) != [str(expected)]:
            errors.append(f"remediation report {name} is missing or mismatched")
    for finding_id in master["finding_ids"]:
        block = re.search(rf"^##\s+{re.escape(finding_id)}\s*$([\s\S]*?)(?=^##\s+|\Z)", report, re.MULTILINE)
        if not block:
            errors.append(f"remediation report is missing {finding_id}")
            continue
        body = block.group(1)
        for field in ("MASTER STEP", "IMPLEMENTATION", "FILE", "TEST", "RESULT"):
            if not re.search(rf"^{re.escape(field)}:\s*.+", body, re.MULTILINE):
                errors.append(f"remediation report {finding_id} is missing {field}")
    return errors


def check_completion(
    response_path: pathlib.Path,
    artifact_path: pathlib.Path | None,
    state_path: pathlib.Path,
    task_file: pathlib.Path | None = None,
    repo: pathlib.Path | None = None,
    expected_subject_id: str | None = None,
) -> list[str]:
    errors: list[str] = []
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    values, response_errors, _ = _response_errors(response_path)
    errors.extend(response_errors)
    if artifact_path is None or not artifact_path.is_file():
        errors.append("downloaded audit artifact is required")
    else:
        artifact = artifact_path.read_text(encoding="utf-8")
        artifact_values, artifact_errors = response_validator.exact_header(artifact)
        errors.extend(f"artifact: {error}" for error in artifact_errors)
        errors.extend(f"artifact: {error}" for error in response_validator.semantic_errors(artifact, artifact_values, _load_expected(response_path, values)[0], artifact_path.parent))
        for key in ("HANDOFF_ID", "ROUND", "AUDIT_TYPE", "AUDIT_SUBJECT_ID", "VERDICT"):
            if values.get(key) != artifact_values.get(key):
                errors.append(f"chat/artifact conflict: {key}")
    if expected_subject_id and values.get("AUDIT_SUBJECT_ID") != expected_subject_id:
        errors.append("completion subject does not match expected subject")
    try:
        subject = json.loads((state_path.parent / "AUDIT_SUBJECT.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        subject = {}
        errors.append(f"completion subject record is invalid: {exc}")
    for key in ("task_id", "round", "audit_type", "audit_subject_id"):
        response_key = "AUDIT_SUBJECT_ID" if key == "audit_subject_id" else key.upper()
        expected = values.get(response_key)
        if expected is not None and str(subject.get(key)) != str(expected):
            errors.append(f"completion {key} does not match frozen subject")
    if state.get("download_verified") is not True:
        errors.append("download transport is not verified; completion remains WAITING or UNVERIFIED")
    if state.get("task_id") and state.get("task_id") != values.get("HANDOFF_ID"):
        errors.append("completion task does not match transport state")
    if state.get("round") and str(state.get("round")) != values.get("ROUND"):
        errors.append("completion round does not match transport state")
    if values.get("VERDICT") != "PASS":
        errors.append("completion requires exact VERDICT: PASS")
    if not task_file or not repo:
        errors.append("current task and repository context are required for completion")
    round_dir = state_path.parent
    errors.extend(audit_integrity.validate_deliverable_bundle(round_dir))
    if task_file and repo:
        errors.extend(audit_integrity.check_staleness(round_dir / "BUNDLE_COMPLETE.json", repo, task_file, None))
    previous_audit = round_dir / "PREVIOUS_AUDIT_RESPONSE.md"
    if previous_audit.is_file():
        previous_master = round_dir / "PREVIOUS_REMEDIATION_MASTER_PROMPT.md"
        report = round_dir / "REMEDIATION_REPORT.md"
        if not previous_master.is_file() or not report.is_file():
            errors.append("previous remediation round is missing bound Master Prompt or report")
        else:
            errors.extend(report_errors(report, previous_audit, previous_master))
    if int(values.get("ROUND", "0") or 0) > int(state.get("max_audit_rounds", 3)):
        errors.append("audit round budget exhausted")
    return sorted(set(errors))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-findings")
    freeze.add_argument("--response", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--state")
    master = sub.add_parser("validate-master-prompt")
    master.add_argument("--master", required=True)
    master.add_argument("--response", required=True)
    master.add_argument("--freeze")
    master.add_argument("--state")
    master.add_argument("--expected-subject-id")
    gate = sub.add_parser("remediation-gate")
    gate.add_argument("--response", required=True)
    gate.add_argument("--master")
    gate.add_argument("--freeze")
    gate.add_argument("--state")
    complete = sub.add_parser("check-completion")
    complete.add_argument("--response", required=True)
    complete.add_argument("--artifact")
    complete.add_argument("--state", required=True)
    complete.add_argument("--task-file")
    complete.add_argument("--repo")
    complete.add_argument("--expected-subject-id")
    phase = sub.add_parser("advance-phase")
    phase.add_argument("--state", required=True)
    phase.add_argument("--phase", required=True, choices=PHASES)
    args = ap.parse_args()
    try:
        if args.command == "freeze-findings":
            print(json.dumps(freeze_findings(pathlib.Path(args.response), pathlib.Path(args.output), pathlib.Path(args.state) if args.state else None), ensure_ascii=False, indent=2))
            return
        if args.command == "validate-master-prompt":
            result = validate_master_prompt(pathlib.Path(args.master), pathlib.Path(args.response), pathlib.Path(args.freeze) if args.freeze else None, args.expected_subject_id, pathlib.Path(args.state) if args.state else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("valid"):
                sys.exit(2)
            return
        if args.command == "remediation-gate":
            result = local_remediation_gate(pathlib.Path(args.response), pathlib.Path(args.master) if args.master else None, pathlib.Path(args.freeze) if args.freeze else None, pathlib.Path(args.state) if args.state else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("allowed") and result.get("master_prompt_required"):
                sys.exit(2)
            return
        if args.command == "advance-phase":
            print(json.dumps(advance_phase(pathlib.Path(args.state), args.phase), ensure_ascii=False, indent=2))
            return
        errors = check_completion(
            pathlib.Path(args.response), pathlib.Path(args.artifact) if args.artifact else None,
            pathlib.Path(args.state), pathlib.Path(args.task_file) if args.task_file else None,
            pathlib.Path(args.repo).resolve() if args.repo else None, args.expected_subject_id,
        )
        if errors:
            print("NOT_COMPLETE")
            for error in errors:
                print(f"- {error}")
            sys.exit(2)
        print("COMPLETE")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"INVALID: {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
