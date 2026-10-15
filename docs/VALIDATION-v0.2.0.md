# v0.2.0 local validation — 2026-10-15

Environment: macOS, Python 3.12.3, Git. This records local checks, not an external
ChatGPT audit verdict or a Windows browser compatibility claim.

## Scope

The first-stage upgrade covers Git path/file discovery, explicit missing/omitted
file states, baseline capture quality, and completion after evidence-only BLOCKED.
The candidate-fingerprint scope redesign is deferred; ignored logs and caches may
still change the worktree fingerprint. No runtime-attestation relaxation is included.

## Verification

- Full suite: `python3 -B -m unittest discover -s tests -v` — 127 tests, OK.
  The original 113 tests remain unchanged; 14 new regression tests exercise actual
  disposable Git repositories and the real handoff/completion helpers.
- New coverage includes nested untracked sources, Chinese/space/quote/arrow paths,
  staged plus unstaged content, rename lineage, confirmed deletion, a tracked
  literal wildcard filename, binary omission, required read/missing failures,
  unsupported directory evidence, and sensitive/outside symlink targets.
  POSIX-only filenames include newlines, carriage returns, backslashes and Unicode
  line separators. Symlink tests can skip when a host cannot create links; none
  skipped in this macOS run. Index bytes are checked before/after collection.
- Baseline defaults UNKNOWN even in a dirty worktree. Explicit EARLY/LATE choices
  survive initialization and appear in the handoff bundle.
- A synthetic local two-round BLOCKED → PASS flow uses real bundles, strict response
  validation, verified-download records and the final completion checker. It needs
  no remediation Master Prompt/report. FAIL/PASS_WITH_NOTES still require them.
  Synthetic responses test local protocol behavior; they are not independent audits.
- Installation smoke: fresh temporary install copied 38 resource files byte-for-byte;
  a repeated install refused overwrite and preserved every hash. Installed helpers,
  invoked by absolute path from a separate disposable project, initialized an early
  baseline and bundled a nested Chinese filename. Bundle validation passed; the
  unchanged candidate was CURRENT and a source edit was STALE. The shipped changelog
  and current validation link targets were present.
- Skill metadata passes `skill-creator/scripts/quick_validate.py`; audit protocol
  version remains 1. Helpers retain standard-library-only dependencies.

The local-worker service timed out. No advisory worker result is counted as
verification. The primary agent inspected the changes and ran the checks above.

## Boundaries

No fresh ChatGPT upload/download/browser audit was run for v0.2.0. The historical
example and v0.1.0 validation record retain their original dates and limits. The
friend-provided assessment's raw probe artifacts were unavailable; these are new
local reproductions rather than a claim to have rerun the original probe.
