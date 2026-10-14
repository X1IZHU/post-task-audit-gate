from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import audit_integrity
import remediation_protocol as protocol
import validate_audit_response as response_validator


def audit_response(verdict: str = "FAIL", finding_ids: tuple[str, ...] = ("PTA-001",)) -> str:
    finding = "\n".join(
        f"### {finding_id} — HIGH — defect\nStatus: OPEN\nObserved evidence: fixture\n"
        for finding_id in finding_ids
    )
    packs = "\n".join(
        f"### {finding_id}\nEXECUTION_MODE: GUIDED\nCOMMAND_STATUS: NO_SAFE_COMMAND\n"
        "LOW_CAPABILITY_EXECUTION_PROMPT:\nInspect the named local target, apply the bounded fix, run local checks, and report every result.\n"
        "REPORT_REQUIRED: YES\nSTOP_REASON: STOP / MANUAL_REVIEW because no trusted execution attestation exists.\n"
        for finding_id in finding_ids
    )
    plan = "\n".join(
        f"### {finding_id}\nACTION: MODIFY\nPRIORITY: HIGH\nOBJECTIVE: fix the defect\nSTEPS: inspect and verify\nVERIFY: run tests\n"
        for finding_id in finding_ids
    )
    return f"""AUDIT_PROTOCOL_VERSION: 1
HANDOFF_ID: task
ROUND: 1
AUDIT_TYPE: INITIAL
AUDIT_SUBJECT_ID: subject
VERDICT: {verdict}

# Independent Post-Task Audit

## Executive Assessment
fixture

## Evidence Reviewed
fixture

## Requirement Assessment
fixture

## Previous Finding Closure
N/A — initial audit

## Findings
{finding}

## Evidence Requests
N/A

## Remediation Plan
{plan}

## Remediation Execution Pack
{packs}

## Test and Verification Assessment
fixture

## Remediation Architecture Status
FINDINGS_FROZEN: FALSE

## Evidence Completeness
COMPLETE

## Final Gate Decision
VERDICT: {verdict}
"""


def master_prompt(response: str, covered: tuple[str, ...] = ("PTA-001",)) -> str:
    values, _ = response_validator.exact_header(response)
    response_hash = protocol.response_sha256(response)
    subject = values["AUDIT_SUBJECT_ID"]
    workstreams = "\n".join(
        f"### {finding_id}\nVERIFIED DEFECT: fixture\nROOT CAUSE: fixture\nREQUIRED END STATE: fixed\n"
        "CONFIRMED TARGETS: scripts\nLIKELY TARGETS: tests\nRECOMMENDED REVIEW TARGETS: docs\n"
        "ORDERED IMPLEMENTATION STEPS: inspect, change, verify\nEDGE CASES: equivalent inputs\n"
        "REGRESSION RISKS: compatibility\nINVARIANTS TO PRESERVE: audit gate\nREQUIRED TESTS: local regression\n"
        "LOCAL CLOSURE CONDITION: independent re-audit\nDO NOT: redefine the finding\n"
        for finding_id in covered
    )
    sections = "\n\n".join(f"## {title}\nfixture" for title in protocol.MASTER_SECTIONS if title != "10. WORKSTREAMS")
    return f"""REMEDIATION_PROTOCOL_VERSION: 1
HANDOFF_ID: task
SOURCE_AUDIT_ROUND: 1
SOURCE_AUDIT_TYPE: INITIAL
SOURCE_AUDIT_SUBJECT_ID: {subject}
SOURCE_AUDIT_VERDICT: FAIL
SOURCE_AUDIT_RESPONSE_HASH: {response_hash}
MASTER_PROMPT_ID: {protocol.master_prompt_id(subject, response_hash)}
FINDINGS_FROZEN: TRUE
CROSS_FINDING_ANALYSIS: COMPLETE
ROOT_CAUSE_ANALYSIS: COMPLETE
REMEDIATION_ORDER: COMPLETE
REGRESSION_ANALYSIS: COMPLETE
ARCHITECTURE_CONSTRAINT_ANALYSIS: COMPLETE
TEST_MATRIX: COMPLETE
REMEDIATION_MASTER_PROMPT_READY: TRUE

{sections}

## 10. WORKSTREAMS
{workstreams}
"""


class RemediationProtocolTests(unittest.TestCase):
    def test_fail_requires_master_and_pass_skips_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fail = root / "fail.md"
            fail.write_text(audit_response(), encoding="utf-8")
            blocked = protocol.local_remediation_gate(fail, None)
            self.assertFalse(blocked["allowed"])
            self.assertTrue(blocked["master_prompt_required"])
            passed = root / "pass.md"
            passed.write_text(audit_response("PASS", ()), encoding="utf-8")
            self.assertFalse(protocol.local_remediation_gate(passed, None)["master_prompt_required"])

    def test_freeze_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            response = root / "response.md"
            freeze = root / "FINDINGS_FREEZE.json"
            response.write_text(audit_response(), encoding="utf-8")
            record = protocol.freeze_findings(response, freeze)
            self.assertEqual(["PTA-001"], record["finding_ids"])
            response.write_text(audit_response("FAIL", ("PTA-002",)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "immutable"):
                protocol.freeze_findings(response, freeze)

    def test_master_prompt_requires_all_planning_gates_and_exact_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            response = root / "response.md"
            response.write_text(audit_response(), encoding="utf-8")
            freeze = root / "FINDINGS_FREEZE.json"
            protocol.freeze_findings(response, freeze)
            master = root / "master.md"
            master.write_text(master_prompt(response.read_text(encoding="utf-8")), encoding="utf-8")
            self.assertTrue(protocol.validate_master_prompt(master, response, freeze)["valid"])
            incomplete = master.read_text(encoding="utf-8").replace("TEST_MATRIX: COMPLETE", "TEST_MATRIX: NOT_READY")
            master.write_text(incomplete, encoding="utf-8")
            self.assertFalse(protocol.validate_master_prompt(master, response, freeze)["valid"])
            master.write_text(master_prompt(response.read_text(encoding="utf-8"), ()), encoding="utf-8")
            result = protocol.validate_master_prompt(master, response, freeze)
            self.assertTrue(any("coverage mismatch" in error for error in result["errors"]))

    def test_master_prompt_rejects_wrong_subject_and_report_needs_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            response = root / "response.md"
            response.write_text(audit_response(), encoding="utf-8")
            freeze = root / "FINDINGS_FREEZE.json"
            protocol.freeze_findings(response, freeze)
            master = root / "master.md"
            master.write_text(master_prompt(response.read_text(encoding="utf-8")), encoding="utf-8")
            wrong = protocol.validate_master_prompt(master, response, expected_subject_id="other")
            self.assertFalse(wrong["valid"])
            report = root / "report.md"
            report.write_text(
                "SOURCE_AUDIT_SUBJECT_ID: subject\nSOURCE_AUDIT_RESPONSE_HASH: "
                + protocol.response_sha256(response.read_text(encoding="utf-8"))
                + "\nSOURCE_MASTER_PROMPT_ID: "
                + protocol.master_prompt_id("subject", protocol.response_sha256(response.read_text(encoding="utf-8")))
                + "\n## PTA-001\nMASTER STEP: MP-01\nIMPLEMENTATION: changed\nFILE: scripts/x.py\nTEST: test\nRESULT: pass\n",
                encoding="utf-8",
            )
            self.assertEqual([], protocol.report_errors(report, response, master))

    def test_cli_gate_does_not_accept_report_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response = pathlib.Path(directory) / "response.md"
            response.write_text(audit_response(), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "remediation_protocol.py"), "remediation-gate", "--response", str(response)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("requires a remediation master prompt", result.stdout)

    def test_completion_requires_download_and_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            response = root / "response.md"
            artifact = root / "artifact.md"
            response.write_text(audit_response("PASS", ()), encoding="utf-8")
            artifact.write_text(response.read_text(encoding="utf-8"), encoding="utf-8")
            state = root / "STATE.json"
            state.write_text(json.dumps({"task_id": "task", "round": 1, "download_verified": False, "max_audit_rounds": 3}), encoding="utf-8")
            errors = protocol.check_completion(response, artifact, state, expected_subject_id="subject")
            self.assertTrue(any("download transport is not verified" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
