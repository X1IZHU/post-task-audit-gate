from __future__ import annotations

import pathlib
import json
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
VALIDATOR_PATH = SCRIPT_DIR / "validate_audit_response.py"
sys.path.insert(0, str(SCRIPT_DIR))

import validate_audit_response as validator


def pack_block(
    finding_id: str = "PTA-001",
    command: str | None = None,
) -> str:
    if command is None:
        return f"""### {finding_id}
EXECUTION_MODE: GUIDED
COMMAND_STATUS: NO_SAFE_COMMAND
LOW_CAPABILITY_EXECUTION_PROMPT:
Inspect the validator and tests, implement the required change using reviewed local editing tools, preserve the safety boundary, and record verification evidence in REMEDIATION_REPORT.md. Do not execute unproven commands.
REPORT_REQUIRED: YES
STOP_REASON: Runtime executable and transitive effects have no independently verified attestation. STOP / MANUAL_REVIEW before command execution.
"""
    return f"""### {finding_id}
EXECUTION_MODE: GUIDED
COMMAND_STATUS: READY
LOW_CAPABILITY_EXECUTION_PROMPT:
Inspect the validator and its tests, add focused regression coverage, preserve existing checks, run the declared test, and record the result in the remediation report.
REPORT_REQUIRED: YES
STOP_REASON: N/A

#### CMD-001
PURPOSE: Run focused validator regression tests.
WORKING_DIRECTORY: .
COMMAND_CLASS: SAFE_TEST
EXECUTION_POLICY: AUTO
PRECONDITIONS: Confirm the test module exists in the repository.
COMMAND: {command}
EXPECTED_RESULT: The test process exits with code zero.
ON_FAILURE: STOP and record the failing output in the remediation report.
"""


def pack_section(*blocks: str) -> str:
    return "## Remediation Execution Pack\n\n" + "\n".join(blocks)


def response(execution_pack: str) -> str:
    return f"""AUDIT_PROTOCOL_VERSION: 1
HANDOFF_ID: test-task
ROUND: 1
AUDIT_TYPE: INITIAL
AUDIT_SUBJECT_ID: subject-123
VERDICT: FAIL

# Independent Post-Task Audit

## Executive Assessment
One actionable finding remains.

## Evidence Reviewed
- Validator and focused tests.

## Requirement Assessment
| Requirement | Relevant implementation | Test/evidence | Status |
|---|---|---|---|
| Validate execution packs | Validator | Focused tests | NOT_SATISFIED |

## Previous Finding Closure
N/A — initial audit

## Findings

### PTA-001 — HIGH — Missing regression coverage
Status: OPEN
Observed evidence: The validator lacks required regression coverage.

## Evidence Requests
N/A

## Remediation Plan

### PTA-001
ACTION: ADD_TEST
PRIORITY: HIGH
OBJECTIVE: Add focused regression coverage.
STEPS: Add and run the focused test.
VERIFY: Run the validator test module.
DO_NOT: Do not weaken validation.

{execution_pack}

## Test and Verification Assessment
Focused coverage is required.

## Evidence Completeness
COMPLETE

## Final Gate Decision
VERDICT: FAIL
The finding remains open.
"""


def remediation_response(closure_state: str, current_finding: bool, execution_pack: str) -> str:
    findings = "None."
    plans = "### PTA-001\nACTION: MODIFY\nPRIORITY: HIGH\nOBJECTIVE: Close the prior finding.\nSTEPS: Apply the bounded fix.\nVERIFY: Run tests.\nDO_NOT: Do not weaken validation."
    if current_finding:
        findings = """### PTA-002 — HIGH — New regression
Status: OPEN
Observed evidence: A new regression is present."""
        plans += """

### PTA-002
ACTION: MODIFY
PRIORITY: HIGH
OBJECTIVE: Fix the new regression.
STEPS: Apply the bounded fix.
VERIFY: Run tests.
DO_NOT: Do not weaken validation."""
    return f"""AUDIT_PROTOCOL_VERSION: 1
HANDOFF_ID: test-task
ROUND: 2
AUDIT_TYPE: REMEDIATION_VERIFICATION
AUDIT_SUBJECT_ID: subject-456
VERDICT: FAIL

# Independent Post-Task Audit

## Executive Assessment
Actionable work remains.

## Evidence Reviewed
- Previous audit and remediation evidence.

## Requirement Assessment
| Requirement | Relevant implementation | Test/evidence | Status |
|---|---|---|---|
| Close prior findings | Validator | Focused tests | NOT_SATISFIED |

## Previous Finding Closure
| Finding | Status | Evidence |
|---|---|---|
| PTA-001 | {closure_state} | Verified against the current files. |

## Findings
{findings}

## Evidence Requests
N/A

## Remediation Plan
{plans}

{execution_pack}

## Test and Verification Assessment
Focused coverage is required.

## Evidence Completeness
COMPLETE

## Final Gate Decision
VERDICT: FAIL
Actionable work remains.
"""


class ExecutionPackValidationTests(unittest.TestCase):
    def test_closure_prose_does_not_create_foreign_finding(self) -> None:
        text = """## Previous Finding Closure
        The evidence includes a SHA-256 digest and the prior finding remains closed.
        SHA-256: deadbeef
| Finding | Status |
| PTA-001 | CLOSED |
## Findings
"""
        states, errors = validator.prior_closure_states(text, ["PTA-001"])
        self.assertEqual({"PTA-001": "CLOSED"}, states)
        self.assertEqual([], errors)

    def test_strict_authoritative_closure_is_exact_and_unique(self) -> None:
        base = """## Previous Finding Closure
| Finding | Status |
| PTA-001 | CLOSED |
| PTA-002 | CLOSED |
| PTA-003 | CLOSED |
## Findings
"""
        for mutation, expected in (
            ("| PTA-001 | CLOSED |\n", "duplicate"),
            ("| PTA-999 | CLOSED |\n", "unexpected"),
            ("| PTA-001 | CLOSED |\n", "missing"),
        ):
            text = base.replace("## Findings", mutation + "## Findings") if expected != "missing" else base.replace("| PTA-001 | CLOSED |\n", "")
            states, errors = validator.prior_closure_states(text, ["PTA-001", "PTA-002", "PTA-003"])
            if expected == "duplicate":
                self.assertTrue(any("duplicate" in item for item in errors))
            elif expected == "unexpected":
                self.assertTrue(any("unexpected" in item for item in errors))
            else:
                self.assertTrue(any("missing or ambiguous" in item for item in errors))

    def errors(self, text: str, expected_findings: list[str] | None = None) -> list[str]:
        values, header_errors = validator.exact_header(text)
        return header_errors + validator.semantic_errors(text, values, expected_findings or [])

    def test_canonical_exact_format_with_stopped_pack_is_valid(self) -> None:
        self.assertEqual([], self.errors(response(pack_section(pack_block()))))

    def test_duplicate_critical_field_is_rejected(self) -> None:
        canonical = response(pack_section(pack_block()))
        duplicated = canonical.replace(
            "# Independent Post-Task Audit\n",
            "# Independent Post-Task Audit\n\nHANDOFF_ID: test-task\n",
            1,
        )
        self.assertTrue(any("duplicate critical field: HANDOFF_ID" in error for error in self.errors(duplicated)))

    def test_missing_pack_is_rejected(self) -> None:
        errors = self.errors(response(""))
        self.assertTrue(any("execution pack count" in error for error in errors))

    def test_plain_file_removal_is_rejected(self) -> None:
        errors = self.errors(response(pack_section(pack_block(command="rm README.md"))))
        self.assertTrue(any("destructive file command" in error for error in errors))

    def test_output_redirection_is_rejected(self) -> None:
        errors = self.errors(response(pack_section(pack_block(command="printf x > README.md"))))
        self.assertTrue(any("cannot be READY" in error for error in errors))

    def test_attached_output_redirection_is_rejected(self) -> None:
        errors = self.errors(response(pack_section(pack_block(command="printf x>README.md"))))
        self.assertTrue(any("output redirection" in error for error in errors))

    def test_quoted_redirection_literal_is_accepted(self) -> None:
        text = response(pack_section(pack_block(command="printf 'x>README.md'")))
        self.assertEqual([], validator.shell_syntax_errors("printf 'x>README.md'"))
        self.assertTrue(any("AUTO_EXECUTE=false" in error for error in self.errors(text)))

    def test_transparent_destructive_wrapper_is_rejected(self) -> None:
        commands = (
            "command rm README.md",
            "busybox rm README.md",
            "env -u FOO rm README.md",
            "env --unset FOO rm README.md",
            "nohup env -u FOO rm README.md",
        )
        for command in commands:
            with self.subTest(command=command):
                errors = self.errors(response(pack_section(pack_block(command=command))))
                self.assertTrue(any("destructive file command" in error for error in errors))

    def test_destructive_find_execution_is_rejected(self) -> None:
        commands = (
            "find . -exec rm {} +",
            "find . -exec sh -c 'rm README.md' {} +",
            "find . -exec env -u FOO rm {} +",
        )
        for command in commands:
            with self.subTest(command=command):
                errors = self.errors(response(pack_section(pack_block(command=command))))
                self.assertTrue(any("find execution" in error for error in errors))

    def test_remote_mutation_is_rejected(self) -> None:
        commands = (
            "gh issue create --title test --body test",
            "curl -X POST https://example.invalid/resource",
            "curl -dpayload https://example.invalid/resource",
            "curl -XPOST https://example.invalid/resource",
            "curl --request=POST https://example.invalid/resource",
            "gh api -XPOST repos/o/r/issues",
        )
        for command in commands:
            with self.subTest(command=command):
                errors = self.errors(response(pack_section(pack_block(command=command))))
                self.assertTrue(any("remote mutation" in error for error in errors))

    def test_remote_read_grammar_does_not_grant_execution_authority(self) -> None:
        commands = (
            "gh issue view 123",
            "curl -f https://example.invalid/resource",
            "curl -XGET https://example.invalid/resource",
            "curl --request=GET https://example.invalid/resource",
            "gh api -XGET repos/o/r/issues/1",
            "env -u FOO python3 -m unittest",
        )
        for command in commands:
            with self.subTest(command=command):
                text = response(pack_section(pack_block(command=command)))
                self.assertTrue(any("AUTO_EXECUTE=false" in error for error in self.errors(text)))

    def test_command_substitution_is_rejected(self) -> None:
        text = response(pack_section(pack_block(command="printf '%s' $(git status --short)")))
        self.assertTrue(any("command substitution" in error for error in self.errors(text)))

    def test_process_substitution_is_rejected(self) -> None:
        text = response(pack_section(pack_block(command="cat <(rm README.md)")))
        self.assertTrue(any("process substitution" in error for error in self.errors(text)))

    def test_combined_shell_command_flags_are_rejected(self) -> None:
        for command in ("bash -lc 'rm README.md'", "bash -xc 'rm README.md'", "sh -fc 'rm README.md'"):
            with self.subTest(command=command):
                text = response(pack_section(pack_block(command=command)))
                self.assertTrue(any("opaque shell wrapper" in error for error in self.errors(text)))

    def test_shell_chaining_is_rejected(self) -> None:
        chained = pack_block(command="python3 -m unittest && python3 -m compileall scripts")
        errors = self.errors(response(pack_section(chained)))
        self.assertTrue(any("without shell chaining" in error for error in errors))

    def test_non_ready_pack_requires_and_accepts_stop_reason(self) -> None:
        stopped = """## Remediation Execution Pack

### PTA-001
EXECUTION_MODE: GUIDED
COMMAND_STATUS: NO_SAFE_COMMAND
LOW_CAPABILITY_EXECUTION_PROMPT:
Inspect the named evidence, do not change repository state, record why no safe exact command can be formed, and request the missing implementation detail.
REPORT_REQUIRED: YES
STOP_REASON: Current evidence does not identify a repository-local target that can be changed safely.
"""
        self.assertEqual([], self.errors(response(stopped)))

    def test_each_unresolved_prior_state_requires_a_pack(self) -> None:
        only_new_pack = pack_section(pack_block("PTA-002"))
        for state in ("OPEN", "PARTIALLY_CLOSED", "DISPUTED_BUT_UNRESOLVED"):
            with self.subTest(state=state):
                errors = self.errors(remediation_response(state, True, only_new_pack), ["PTA-001"])
                self.assertTrue(any("execution pack count for PTA-001 is 0" in error for error in errors))

    def test_closed_prior_finding_does_not_require_a_pack(self) -> None:
        text = remediation_response("CLOSED", True, pack_section(pack_block("PTA-002")))
        self.assertEqual([], self.errors(text, ["PTA-001"]))

    def test_unresolved_prior_and_new_finding_each_have_one_pack(self) -> None:
        packs = pack_section(pack_block("PTA-001"), pack_block("PTA-002"))
        text = remediation_response("OPEN", True, packs)
        self.assertEqual([], self.errors(text, ["PTA-001"]))

    def run_standard_cli(self, text: str, expected_payload: object | None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            response_path = root / "AUDIT_RESPONSE.md"
            response_path.write_text(text, encoding="utf-8")
            if expected_payload is not None:
                (root / "EXPECTED_PREVIOUS_FINDINGS.json").write_text(
                    json.dumps(expected_payload), encoding="utf-8"
                )
            return subprocess.run(
                [sys.executable, "-B", str(VALIDATOR_PATH), str(response_path)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_standard_cli_loads_expected_findings_and_rejects_omission(self) -> None:
        text = remediation_response("CLOSED", True, pack_section(pack_block("PTA-002")))
        omitted = text.replace(
            "| PTA-001 | CLOSED | Verified against the current files. |",
            "None.",
        )
        result = self.run_standard_cli(omitted, {"finding_ids": ["PTA-001"]})
        self.assertEqual(2, result.returncode)
        self.assertIn("PTA-001", result.stdout)

    def test_standard_cli_accepts_complete_authoritative_closure(self) -> None:
        text = remediation_response("CLOSED", True, pack_section(pack_block("PTA-002")))
        result = self.run_standard_cli(text, {"finding_ids": ["PTA-001"]})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("VERDICT=FAIL", result.stdout)

    def test_standard_cli_fails_closed_without_expected_findings_file(self) -> None:
        text = remediation_response("CLOSED", True, pack_section(pack_block("PTA-002")))
        result = self.run_standard_cli(text, None)
        self.assertEqual(2, result.returncode)
        self.assertIn("requires authoritative prior findings", result.stdout)

    def test_standard_cli_fails_closed_for_malformed_expected_findings_file(self) -> None:
        text = remediation_response("CLOSED", True, pack_section(pack_block("PTA-002")))
        result = self.run_standard_cli(text, {"finding_ids": "PTA-001"})
        self.assertEqual(2, result.returncode)
        self.assertIn("invalid shape", result.stdout)

    def test_confirmed_safety_bypasses_fail_full_cli(self) -> None:
        commands = (
            "bash -lc 'rm README.md'",
            "bash -xc 'rm README.md'",
            "env -u FOO rm README.md",
            "find . -exec env -u FOO rm {} +",
            "cat <(rm README.md)",
            "curl -XPOST https://example.invalid/resource",
            "curl --request=POST https://example.invalid/resource",
            "gh api -XPOST repos/o/r/issues",
        )
        for command in commands:
            with self.subTest(command=command):
                text = response(pack_section(pack_block(command=command)))
                result = self.run_standard_cli(text, None)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)

    def test_fail_closed_command_equivalence_suite(self) -> None:
        commands = (
            "rm README.md",
            "'rm' README.md",
            r"\r\m README.md",
            "timeout 1 rm README.md",
            "nice rm README.md",
            "stdbuf -oL rm README.md",
            "setsid rm README.md",
            "/usr/bin/time rm README.md",
            "exec -a fake rm README.md",
            "env FOO=bar rm README.md",
            "FOO=bar rm README.md",
            "git diff --check\nrm README.md",
            "find . -exec timeout 1 rm {} +",
            "find . -exec find . -exec rm {} + {} +",
            "xargs python3 -c 'import os; os.remove(\"README.md\")'",
            "bash -c 'rm README.md'",
            "eval 'rm README.md'",
            "printf '%s' $(rm README.md)",
            "printf '%s' `rm README.md`",
            "cat <(rm README.md)",
            "gh issue create --title test --body test",
            "gh api repos/o/r/issues -ftitle=test",
            "gh api repos/o/r/issues --field=title=test",
            "gh api repos/o/r/issues --input=payload.json",
            "curl --json '{}' https://example.invalid/resource",
            "curl --data-ascii x=1 https://example.invalid/resource",
            "wget --method=POST https://example.invalid/resource",
            "wget -S https://example.invalid/resource",
            "wget --spider -O result.txt https://example.invalid/resource",
            "npm publish",
            "npm version patch",
            "pnpm publish",
            "yarn release",
            "npm run verify",
            "./wrapper.sh",
            "base64 -d payload.txt",
            "git push",
            "git remote add origin https://example.invalid/repo.git",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = validator.classify_command_semantics(command)
                self.assertFalse(decision.auto_execute)
                self.assertTrue(validator.command_safety_errors(command))
                if "\n" not in command:
                    result = self.run_standard_cli(response(pack_section(pack_block(command=command))), None)
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)

    def test_argument_level_output_and_credential_bypasses_fail_full_cli(self) -> None:
        commands = (
            "sort README.md -o SKILL.md",
            "sort -o SKILL.md README.md",
            "sort --output SKILL.md README.md",
            "sort --output=SKILL.md README.md",
            "uniq README.md SKILL.md",
            "git log --output=SKILL.md",
            "git log --output SKILL.md",
            "git show --output=SKILL.md HEAD",
            "git show --output SKILL.md HEAD",
            "curl -u user:secret https://example.invalid/resource",
            "curl --user user:secret https://example.invalid/resource",
            "curl -uuser:secret https://example.invalid/resource",
            "curl --user=user:secret https://example.invalid/resource",
            "curl --config credentials.conf https://example.invalid/resource",
            "curl -K credentials.conf https://example.invalid/resource",
            "curl --netrc-file credentials.conf https://example.invalid/resource",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.run_standard_cli(response(pack_section(pack_block(command=command))), None)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("AUTO_EXECUTE=false", result.stdout)

    def test_static_controls_require_execution_attestation_full_cli(self) -> None:
        commands = (
            "sort README.md",
            "sort",
            "uniq README.md",
            "uniq",
            "git log",
            "git show HEAD",
            "git status --short",
            "curl --url https://example.invalid/resource",
            "curl --url=https://example.invalid/resource",
            "curl -I https://example.invalid/resource",
            "curl -XGET https://example.invalid/resource",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.run_standard_cli(response(pack_section(pack_block(command=command))), None)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("AUTO_EXECUTE=false", result.stdout)

    def test_allowlisted_read_families_reject_unproven_argument_forms(self) -> None:
        commands = (
            "cat --help",
            "head -n 1 README.md",
            "ls -la",
            "wc -l README.md",
            "true unexpected",
            "git log --exec=cat",
            "git show --config=config.ini HEAD",
            "find . -name README.md",
            "pytest --junitxml=results.xml",
            "python3 tests/test_validate_audit_response.py --unsafe",
            "python3 -m unittest --buffer",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = validator.classify_command_semantics(command)
                self.assertFalse(decision.auto_execute)
                self.assertTrue(validator.command_safety_errors(command))

    def test_safe_capabilities_are_narrow_and_explicit(self) -> None:
        commands = (
            "git diff --check",
            "git status --short",
            "python3 -B -m unittest discover -s tests -p 'test_*.py'",
            "python3 -m json.tool schema/audit-response.schema.json",
            "timeout 1 nice stdbuf -oL setsid /usr/bin/time git diff --check",
            "env -u FOO python3 -m unittest",
            "printf 'x>README.md'",
            "curl -I https://example.invalid/resource",
            "curl -XGET https://example.invalid/resource",
            "gh api -XGET repos/o/r/issues/1",
            "gh issue view 123",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(validator.auto_execute_allowed(command), command)

    def test_package_scripts_are_resolved_before_being_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            (repo / "package.json").write_text(json.dumps({
                "scripts": {
                    "verify": "git diff --check",
                    "nested": "npm run verify",
                    "unsafe": "rm README.md",
                    "prewith-hook": "git diff --check",
                    "with-hook": "git status --short",
                    "postwith-hook": "rm README.md",
                }
            }), encoding="utf-8")
            self.assertFalse(validator.auto_execute_allowed("npm run verify", repo))
            self.assertFalse(validator.auto_execute_allowed("pnpm nested", repo))
            self.assertFalse(validator.auto_execute_allowed("yarn run with-hook", repo))
            self.assertFalse(validator.auto_execute_allowed("npm run unsafe", repo))
            self.assertFalse(validator.auto_execute_allowed("npm run missing", repo))
            self.assertFalse(validator.auto_execute_allowed("npm run verify -- --unsafe", repo))


    def test_unattested_runtime_equivalences_reject_in_production(self) -> None:
        bases = [
            "sort README.md $ARGS", 'sort README.md "${ARGS}"',
            "git log $@", "curl $* https://example.invalid/", "cat *",
            "cat ~/.ssh/id_rsa", "cat ../outside", "cat /etc/passwd",
            "cat link-to-outside", "python3 tests/test_nonexistent.py",
            "python3 -m pytest tests", "pytest tests", "python3 -m unittest",
            "python3 -B -m py_compile x.py", "python3 -m json.tool data.json",
            "git diff --check", "./git diff --check", "true", "printf 'literal $X >'",
        ]
        wrappers = ["{}", "env PATH=bin {}", "timeout 1 nice {}", "command {}"]
        for base in bases:
            for wrapper in wrappers:
                command = wrapper.format(base)
                with self.subTest(command=command):
                    result = self.run_standard_cli(response(pack_section(pack_block(command=command))), None)
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("AUTO_EXECUTE=false", result.stdout)
                    guided = response(pack_section(pack_block(command=command))).replace(
                        "EXECUTION_POLICY: AUTO", "EXECUTION_POLICY: VERIFY_THEN_RUN")
                    self.assertTrue(any("AUTO_EXECUTE=false" in error for error in self.errors(guided)))


if __name__ == "__main__":
    unittest.main()
