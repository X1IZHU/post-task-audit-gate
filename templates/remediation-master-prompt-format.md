# REMEDIATION MASTER PROMPT

REMEDIATION_PROTOCOL_VERSION: 1
HANDOFF_ID: <task id>
SOURCE_AUDIT_ROUND: <round>
SOURCE_AUDIT_TYPE: INITIAL | REMEDIATION_VERIFICATION
SOURCE_AUDIT_SUBJECT_ID: <exact source subject>
SOURCE_AUDIT_VERDICT: FAIL | PASS_WITH_NOTES
SOURCE_AUDIT_RESPONSE_HASH: <sha256 of the captured source response>
MASTER_PROMPT_ID: <protocol-derived id>

FINDINGS_FROZEN: TRUE
CROSS_FINDING_ANALYSIS: COMPLETE
ROOT_CAUSE_ANALYSIS: COMPLETE
REMEDIATION_ORDER: COMPLETE
REGRESSION_ANALYSIS: COMPLETE
ARCHITECTURE_CONSTRAINT_ANALYSIS: COMPLETE
TEST_MATRIX: COMPLETE
REMEDIATION_MASTER_PROMPT_READY: TRUE

## 0. EXECUTION ROLE
You are the remediation implementer. Do not re-audit, redefine findings, or claim
findings CLOSED. Implement only the frozen findings below and report local remediation.

## 1. SOURCE AUDIT IDENTITY
Repeat the binding fields above and explain the response hash.

## 2. CURRENT VERIFIED STATE
List only CONFIRMED, LIKELY, and UNKNOWN facts supported by the source audit.

## 3. GLOBAL REMEDIATION OBJECTIVE
State the property that must be true after implementation.

## 4. FINDINGS IN SCOPE
List every actionable finding exactly once.

## 5. FINDINGS OUT OF SCOPE / ALREADY CLOSED
List findings and behaviors that must not be redefined or remediated in this pass.

## 6. CROSS-FINDING RELATIONSHIP
Describe shared causes, dependencies, ordering, and interaction risks.

## 7. ARCHITECTURE / INVARIANTS TO PRESERVE
State the current architecture and compatibility/security invariants.

## 8. GLOBAL IMPLEMENTATION STRATEGY
Explain the single root-cause-driven strategy for the frozen findings.

## 9. ORDER OF OPERATIONS
Give a numbered order from safe reproduction through final report.

## 10. WORKSTREAMS

### PTA-001
VERIFIED DEFECT: ...
ROOT CAUSE: ...
REQUIRED END STATE: ...
CONFIRMED TARGETS: ...
LIKELY TARGETS: ...
RECOMMENDED REVIEW TARGETS: ...
ORDERED IMPLEMENTATION STEPS: ...
EDGE CASES: ...
REGRESSION RISKS: ...
INVARIANTS TO PRESERVE: ...
REQUIRED TESTS: ...
LOCAL CLOSURE CONDITION: ...
DO NOT: ...

## 11. CROSS-FINDING INTEGRATION
Explain how the workstreams are applied and verified together.

## 12. ADVERSARIAL GENERALIZATION
List equivalent cases beyond the observed counterexample.

## 13. COMPLETE TEST MATRIX
For each test state the input, expected result, test level, and finding covered.

## 14. LOCAL VERIFICATION PLAN
Specify focused tests, full suite, production-path, schema, syntax, integrity, and diff checks.

## 15. SAFE EXECUTION COMMANDS
Give only exact local commands proven safe by the current policy. Otherwise write
STOP / MANUAL_REVIEW with the reason.

## 16. REMEDIATION REPORT REQUIREMENTS
Require SOURCE_AUDIT_SUBJECT_ID, SOURCE_AUDIT_RESPONSE_HASH, SOURCE_MASTER_PROMPT_ID,
and one MASTER STEP → IMPLEMENTATION → FILE → TEST → RESULT record per workstream.

## 17. STOP CONDITIONS
Stop on binding mismatch, missing target, changed finding meaning, unsafe command,
failed test, or a conflict with the remediation objective.

## 18. COMPLETION CONDITIONS
The implementer may report only REMEDIATION_IMPLEMENTED and LOCAL_VERIFICATION_PASS.
It must not report CLOSED or PASS; only the next independent audit can do that.
