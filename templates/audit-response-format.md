# AUDIT_RESPONSE_FORMAT

The first non-empty lines of the response must be exactly these protocol fields, one per line:

```text
AUDIT_PROTOCOL_VERSION: 1
HANDOFF_ID: <task id>
ROUND: <integer>
AUDIT_TYPE: INITIAL | REMEDIATION_VERIFICATION
AUDIT_SUBJECT_ID: <exact audited subject id>
VERDICT: PASS | PASS_WITH_NOTES | FAIL | BLOCKED
```

Then use this Markdown structure:

```markdown
# Independent Post-Task Audit

## Executive Assessment
<concise assessment>

## Evidence Reviewed
- <bundle path / repository path and what was checked>

## Requirement Assessment
| Requirement | Relevant implementation | Test/evidence | Status |
|---|---|---|---|
| ... | ... | ... | SATISFIED / PARTIALLY_SATISFIED / NOT_SATISFIED / NOT_VERIFIABLE |

## Previous Finding Closure
<!-- Required for REMEDIATION_VERIFICATION; write "N/A — initial audit" for INITIAL. -->
| Finding | Status | Evidence |
|---|---|---|
| PTA-001 | CLOSED / OPEN / PARTIALLY_CLOSED / DISPUTED_BUT_UNRESOLVED | ... |

## Findings
<!-- Write "None." if there are no actionable findings. -->

### PTA-001 — HIGH — <title>
Status: OPEN
Requirement / expected behavior: ...
Observed evidence: ...
Root cause: ...
Impact: ...
Remediation objective: ...
Likely affected files/modules: ...
Implementation guidance: ...
Constraints to preserve: ...
Required/Recommended tests: ...
Verification expectation: ...
Closure condition: ...
Regression risks: ...
Avoid: ...

## Evidence Requests
<!-- Use N/A when no additional evidence is required. -->
### ER-001
Required evidence: ...
Evidence type: FULL_FILE | BEFORE_AND_AFTER | SEARCH_RESULTS | REFERENCES | CONFIG | SCHEMA | MIGRATION | TEST_FILE | RAW_TEST_OUTPUT | PACKAGE_MANIFEST | DIRECTORY_LISTING | OTHER
Reason: ...
Task-critical: YES | NO

## Remediation Plan
<!-- Use N/A when there are no actionable findings. -->
### PTA-001
ACTION: MODIFY | ADD_TEST | REQUEST_EVIDENCE | USER_DECISION_REQUIRED | DESIGN_DECISION_REQUIRED
PRIORITY: BLOCKER | HIGH | MEDIUM | LOW
DEPENDS_ON: ...
TARGETS: ...
OBJECTIVE: ...
STEPS: ...
VERIFY: ...
DO_NOT: ...

## Remediation Execution Pack
<!-- Required once for every actionable finding in FAIL or PASS_WITH_NOTES. Use
N/A only when there are no actionable findings. Commands are guidance for Codex and
must remain repository-local. -->
### PTA-001
EXECUTION_MODE: GUIDED | NORMAL
COMMAND_STATUS: READY | NO_SAFE_COMMAND | USER_DECISION_REQUIRED | DESIGN_DECISION_REQUIRED
LOW_CAPABILITY_EXECUTION_PROMPT:
<copy-ready, self-contained instruction telling a lower-capability Codex model what
to inspect, change, preserve, verify, and report for this finding>
REPORT_REQUIRED: YES
STOP_REASON: N/A | <required when COMMAND_STATUS is not READY>

#### CMD-001
PURPOSE: <one concrete purpose>
WORKING_DIRECTORY: <repository-relative directory; use . for repository root>
COMMAND_CLASS: SAFE_READ | SAFE_TEST | SAFE_BUILD | MODIFYING
EXECUTION_POLICY: AUTO | VERIFY_THEN_RUN
PRECONDITIONS: <facts Codex must confirm before execution>
COMMAND: <exact local command; no placeholders>
EXPECTED_RESULT: <observable success result>
ON_FAILURE: <STOP plus the evidence to record or the bounded fallback>

## Test and Verification Assessment
...

## Remediation Architecture Status
FINDINGS_FROZEN: TRUE | FALSE
CROSS_FINDING_ANALYSIS: COMPLETE | NOT_READY
ROOT_CAUSE_ANALYSIS: COMPLETE | NOT_READY
REMEDIATION_ORDER: COMPLETE | NOT_READY
REGRESSION_ANALYSIS: COMPLETE | NOT_READY
ARCHITECTURE_CONSTRAINT_ANALYSIS: COMPLETE | NOT_READY
TEST_MATRIX: COMPLETE | NOT_READY
REMEDIATION_MASTER_PROMPT_ARTIFACT: REMEDIATION_MASTER_PROMPT_<task id>_R<NN>.md | N/A
REMEDIATION_MASTER_PROMPT_READY: TRUE | FALSE

When VERDICT is FAIL or PASS_WITH_NOTES and actionable findings exist, the complete
architecture pass must finish before remediation. Emit a separate downloadable
`REMEDIATION_MASTER_PROMPT_<task id>_R<NN>.md`; the audit report's remediation prose
is not an executable substitute. The Master Prompt must bind the source response
hash and cover every frozen finding exactly once. A BLOCKED response must not emit a
code-remediation Master Prompt.

## Evidence Completeness
COMPLETE | INCOMPLETE
<explain omissions/redactions/truncation if any>

## Final Gate Decision
VERDICT: <same verdict as above>
<one short reason>
```

Rules:

- Do not invent evidence.
- Do not mark a finding CLOSED based only on Codex's claim.
- If evidence necessary for correctness is missing, prefer BLOCKED.
- PASS requires `Findings: None` for actionable findings and COMPLETE evidence for material scope.
- Every actionable finding in FAIL or PASS_WITH_NOTES requires exactly one execution
  pack with the same PTA ID. A READY pack requires at least one CMD block.
- FAIL or PASS_WITH_NOTES with actionable findings also requires a separate valid
  Remediation Master Prompt after the findings-freeze and architecture-pass gates.
- `AUTO` is allowed only for SAFE_READ, SAFE_TEST, or SAFE_BUILD. `MODIFYING` requires
  `VERIFY_THEN_RUN` after Codex confirms targets and preconditions.
- A READY command must also pass the local fail-closed capability validator. Unknown
  or unproven semantics have `AUTO_EXECUTE=false` and must use `NO_SAFE_COMMAND`,
  `USER_DECISION_REQUIRED`, or `DESIGN_DECISION_REQUIRED`; changing the policy label
  cannot bypass this requirement.
- Package-manager script commands are READY only when the actual script plus implicit
  pre/post scripts resolve recursively to the same safe subset. Otherwise stop.
- Never put destructive, credential, privileged, production, or external-side-effect
  commands in a READY pack. Use a non-READY status and explain the stop reason.
- Direct or wrapped file-removal commands, unquoted output redirection, command
  substitution, destructive nested execution, remote mutations, and opaque
  shell/interpreter wrappers are not valid READY commands even when marked
  `VERIFY_THEN_RUN`.
- Put one command in each CMD block; do not use shell chaining, pipelines, or command
  separators. Use multiple ordered CMD blocks instead.
- Do not use placeholders such as `<path>`, `...`, `TODO`, or invented symbols in an
  actual execution prompt or command.
# Current execution eligibility

No trusted execution-environment attestation is implemented. All unattested shell
commands are ineligible for READY, even when their grammar is recognized as local
inspection. For actionable findings return the full low-capability remediation
prompt with COMMAND_STATUS: NO_SAFE_COMMAND and an explicit STOP / MANUAL_REVIEW
reason. This is valid and must not invalidate an otherwise useful remediation plan.
