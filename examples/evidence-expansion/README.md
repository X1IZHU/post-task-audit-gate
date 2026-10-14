# Real case: missing evidence → evidence expansion → PASS

This is a sanitized account of an actual two-round ChatGPT audit captured on
2026-10-13. It is historical evidence of the workflow, not a fresh audit of this
release. The prompt in the top-level Quick Start is a separate illustration.

## Task and implementation

The user asked to rerun the existing functionality and proceed to the next step.
The agreed scope covered local checks, Web/API behavior, database integration,
verification of a cited earlier audit, and preparation for subsequent work.
Live external data collection and production deployment were outside scope.
This is a translated summary, not the verbatim full task specification.

Codex ran local checks and addressed observed regressions, then assembled the
task, implementation report, test output, selected code, diff, and manifests.
The skill submitted that evidence to the independent ChatGPT auditor and captured
the response. The implementation report alone did not establish completion.

## What the auditor found

Round 1 returned **BLOCKED**. The selected bundle lacked the original browser
interaction evidence, a clean test-database migration transcript, and the original
historical audit response being cited. The auditor requested those materials rather
than inventing a code defect or accepting a narrative claim.

See the [actual Round 1 excerpt](round-01-excerpt.md).

Codex gathered the missing local evidence and prepared a second handoff. This was
evidence expansion, not production-code remediation. Round 2 returned **PASS** for
the local task after examining the additional materials. It did not authorize live
external collection, claim real-environment Windows compatibility, or approve deployment.

See the [actual Round 2 excerpt](round-02-excerpt.md).

## Evidence and limits

[PROVENANCE.json](PROVENANCE.json) records the original complete response sizes and
SHA-256 digests, plus the published excerpt digests and selection rules. The original
private repository, complete task, responses, screenshots, and bundles are not shipped.
Readers cannot independently reconstruct the original private run from these
excerpts. Hashes identify the originals for people who hold them; they are not a
substitute for independent evidence or permission to reuse a historical PASS.

The excerpts preserve selected actual fields and sentences. They are intentionally
incomplete and cannot be validated or replayed as current audit responses.

## Reproduce the local mechanisms

From the release repository root, run the existing tests:

```sh
python3 -B -m unittest discover -s tests -p test_audit_integrity.py -v
python3 -B -m unittest discover -s tests -p test_build_handoff.py -v
```

These execute local integrity, handoff, and evidence-expansion checks with temporary
fixtures. They do not call ChatGPT. They are not a recording of the original audit.

For an independent end-to-end run, install the skill and use the top-level Quick
Start with a small project you are permitted to upload. Let the agent create its own
baseline and evidence, submit through the real in-app browser, and capture the real
result. An auditor may reach a different verdict; never seed it with these excerpts
as the expected outcome.

## Short recording plan

No GIF is included; no recording toolchain was installed. A truthful short demo can
use the operating system's screen recorder:

1. In a disposable project, submit a small task with the audit opt-in.
2. Show the agent finishing its checks and the browser's actual attachment/send step.
3. Show the complete response and the agent's captured result path.
4. If evidence is requested, show the actual follow-up round and resulting verdict.

Hide account details and private project contents. Label cuts and any skipped waiting
time. Do not replace generated responses with prewritten PASS text.
