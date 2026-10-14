from __future__ import annotations

import pathlib
import json
import sys
import tempfile
import unittest
import subprocess

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import build_handoff
import validate_audit_response
import audit_integrity


PREVIOUS_FAIL = """AUDIT_PROTOCOL_VERSION: 1
HANDOFF_ID: test-task
ROUND: 1
AUDIT_TYPE: INITIAL
AUDIT_SUBJECT_ID: subject-123
VERDICT: FAIL

# Independent Post-Task Audit

## Previous Finding Closure
N/A — initial audit

## Findings
### PTA-001 — HIGH — Missing enforcement
Status: OPEN

## Evidence Requests
N/A

## Remediation Execution Pack
### PTA-001
EXECUTION_MODE: GUIDED
COMMAND_STATUS: READY
LOW_CAPABILITY_EXECUTION_PROMPT:
Apply the bounded local fix, preserve existing behavior, run the exact checks, and record every result in the remediation report.
REPORT_REQUIRED: YES
STOP_REASON: N/A

#### CMD-001
PURPOSE: Run focused tests.
WORKING_DIRECTORY: .
COMMAND_CLASS: SAFE_TEST
EXECUTION_POLICY: AUTO
PRECONDITIONS: Confirm the test file exists.
COMMAND: python3 -B tests/test_validate_audit_response.py
EXPECTED_RESULT: Tests pass.
ON_FAILURE: STOP and record the output.

## Test and Verification Assessment
Tests required.
"""

PREVIOUS_BLOCKED = PREVIOUS_FAIL.replace("VERDICT: FAIL", "VERDICT: BLOCKED", 1)

VALID_REPORT = """## PTA-001
Codex position: REMEDIATED
Execution pack status: EXECUTED
Low-capability execution prompt followed: YES
Changes made:
- Added enforcement.
Evidence:
- Validator and tests updated.
Verification:
- Focused tests pass.

### CMD-001
Command: `python3 -B tests/test_validate_audit_response.py`
Working directory: `.`
Preflight: PASS
Execution policy: AUTO
Exit code: 0
Result: Tests passed.
Changed files: tests/test_validate_audit_response.py
Failure handling: Not needed; command passed.
"""


class RemediationHandoffTests(unittest.TestCase):
    def paths(self, previous: str, report: str | None = None):
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name)
        previous_path = root / "previous.md"
        previous_path.write_text(previous, encoding="utf-8")
        report_path = None
        if report is not None:
            report_path = root / "report.md"
            report_path.write_text(report, encoding="utf-8")
        return temp, previous_path, report_path

    def test_remediation_round_without_report_fails(self) -> None:
        temp, previous, _ = self.paths(PREVIOUS_FAIL)
        with temp, self.assertRaisesRegex(ValueError, "requires --remediation-report"):
            build_handoff.validate_round_inputs(2, str(previous), None)

    def test_malformed_report_fails(self) -> None:
        malformed = VALID_REPORT.replace("Result: Tests passed.\n", "")
        temp, previous, report = self.paths(PREVIOUS_FAIL, malformed)
        with temp, self.assertRaisesRegex(ValueError, "is missing Result"):
            build_handoff.validate_round_inputs(2, str(previous), str(report))

    def test_valid_report_is_accepted_and_copied(self) -> None:
        temp, previous, report = self.paths(PREVIOUS_FAIL, VALID_REPORT)
        with temp:
            previous_text, report_text = build_handoff.validate_round_inputs(2, str(previous), str(report))
            self.assertEqual(VALID_REPORT, report_text)
            out = pathlib.Path(temp.name) / "out"
            out.mkdir()
            build_handoff.copy_round_lineage(out, str(previous), str(report), previous_text)
            self.assertEqual(VALID_REPORT, (out / "REMEDIATION_REPORT.md").read_text(encoding="utf-8"))
            self.assertTrue((out / "PREVIOUS_AUDIT_RESPONSE.md").is_file())
            self.assertTrue((out / "EXPECTED_PREVIOUS_FINDINGS.json").is_file())
            expected_ids, errors = validate_audit_response.resolve_expected_findings(
                out / "AUDIT_RESPONSE.md",
                "REMEDIATION_VERIFICATION",
                [],
                str(out / "EXPECTED_PREVIOUS_FINDINGS.json"),
            )
            self.assertEqual([], errors)
            self.assertEqual(["PTA-001"], expected_ids)

    def test_blocked_evidence_expansion_can_omit_report(self) -> None:
        temp, previous, _ = self.paths(PREVIOUS_BLOCKED)
        with temp:
            previous_text, report_text = build_handoff.validate_round_inputs(2, str(previous), None)
            self.assertIsNotNone(previous_text)
            self.assertIsNone(report_text)

    def test_builder_emits_canonical_task_spec_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "README.md").write_text("audit fixture\n", encoding="utf-8")
            audit_root = repo / ".codex-audit"
            audit_root.mkdir()
            task = {
                "protocol_version": 1,
                "task_id": "manifest-fixture",
                "original_task": "Implement the fixture task.",
                "user_task_transcript": "",
                "task_spec_hash": build_handoff.task_spec_manifest({
                    "original_task": "Implement the fixture task.",
                    "user_task_transcript": "",
                })["task_spec_hash"],
                "max_audit_rounds": 3,
            }
            (audit_root / "task.json").write_text(json.dumps(task), encoding="utf-8")
            implementation = repo / "implementation.md"
            tests = repo / "tests.md"
            implementation.write_text("Codex claims fixture implementation.\n", encoding="utf-8")
            tests.write_text("Command: python3 -B -m unittest\nExit code: 0\n", encoding="utf-8")
            previous_argv = sys.argv
            try:
                sys.argv = [
                    "build_handoff.py",
                    "--repo", str(repo),
                    "--round", "1",
                    "--implementation-report", str(implementation),
                    "--test-results", str(tests),
                ]
                build_handoff.main()
            finally:
                sys.argv = previous_argv
            round_dir = audit_root / "sessions" / "manifest-fixture" / "round-01"
            manifest_path = round_dir / "TASK_SPEC_MANIFEST.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(task["task_spec_hash"], manifest["task_spec_hash"])
            self.assertEqual([], audit_integrity.validate_bundle(round_dir))
            self.assertEqual([], audit_integrity.validate_deliverable_bundle(round_dir))
            try:
                sys.argv = [
                    "build_handoff.py",
                    "--repo", str(repo),
                    "--round", "1",
                    "--implementation-report", str(implementation),
                    "--test-results", str(tests),
                ]
                build_handoff.main()
                moved_zip = repo / "moved-bundle.zip"
                (round_dir / "AUDIT_BUNDLE_manifest-fixture_R01.zip").replace(moved_zip)
                with self.assertRaisesRegex(ValueError, "expected deliverable ZIP is missing"):
                    build_handoff.main()
            finally:
                sys.argv = previous_argv


if __name__ == "__main__":
    unittest.main()
