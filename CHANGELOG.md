# Changelog

## v0.2.0 — 2026-10-15

- Collect untracked nested files individually with NUL-delimited Git status; preserve
  Unicode, spaces, quotes, arrows, newlines, and rename source/destination paths.
  Keep index state unchanged and use literal pathspecs for filtered diffs.
- Distinguish confirmed deletions, deliberate omissions and unexpected read failures.
  Required read failures/unsupported non-file candidates prevent a completed bundle.
  Keep private audit state and links to outside/sensitive targets out of snapshots.
- Default baseline quality to `UNKNOWN`; accept explicit early/late capture declarations
  and include the declaration in every handoff.
- Allow evidence-only `BLOCKED` → `PASS` completion without code-remediation documents;
  preserve the requirements for prior `FAIL` / `PASS_WITH_NOTES` and all identity,
  transport, lineage and staleness checks.
- Preserve plain checksum records; escape unusual filenames in `HASHES.sha256`.
- Candidate fingerprint scope, runtime attestation, and Windows browser validation
  are not redesigned or newly established by this release.

## v0.1.0 — 2026-10-14

- Package the existing installed skill, helpers, templates, schemas, and tests in
  a standalone repository. No prior release tags or source Git history were found
  in the installed copy; this is not a reconstruction of development history.
- Add English and Chinese onboarding, an installation helper that refuses to
  overwrite existing files, a sanitized historical example, and a bug-report template.
- Clarify installed-resource paths and runtime prerequisites in `SKILL.md`.
- Apply the author's selected MIT license and public attribution to X1IZHU.
- Preserve the existing audit protocol, validators, browser transport constraints,
  execution-attestation boundary, and XHS review profile.

First public candidate; earlier development versions are not reconstructed here.
