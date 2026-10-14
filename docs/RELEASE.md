# First public release — v0.1.0

Repository: [X1IZHU/post-task-audit-gate](https://github.com/X1IZHU/post-task-audit-gate).
This file contains the repository presentation and first-release notes.

## Repository presentation

Public display name: **Post-task Audit Gate**.

The compatibility identifier remains `post-task-chatgpt-audit`. The display name
avoids using ChatGPT/GPT as the project's own product name; ChatGPT appears only as
the service used by the workflow. No OpenAI logo or endorsement is implied.
See [OpenAI brand guidelines](https://openai.com/brand/).

Repository description:

> Agent-run post-task audits with independent ChatGPT review, evidence binding, and verified finding closure.

Topics:

```text
agent-skills codex chatgpt code-review ai-agents audit evidence-verification developer-tools open-source
```

License: MIT, selected by the author, with copyright and visible author attribution
to **X1IZHU**. Commercial use is permitted under its terms.

Optional social preview: a plain original diagram, “Implement → Verify → Independent
audit”, under the project name, with “Evidence before completion” as the caption.
Avoid OpenAI logos, official-looking badges, or a fabricated audit screenshot.

## Release

Title: **v0.1.0 — Post-task Audit Gate: first public candidate**

Release notes:

---

Post-task Audit Gate helps a coding agent verify completed work through a separate,
evidence-based ChatGPT review. Once enabled for a phase, Codex handles the handoff,
browser submission, result capture, evidence expansion or remediation, and re-audit.

This first public candidate packages the existing workflow with:

- English and Chinese setup guides and a copyable first-use prompt.
- A dependency-free Python installer that refuses to overwrite existing installations.
- Task/worktree/evidence identity checks, strict result validation, and independent
  finding closure using the existing protocol.
- A sanitized real BLOCKED → evidence expansion → PASS example.
- Existing local tests, plus a documented installation and offline handoff smoke check.

Start with [README.md](../README.md), or [简体中文](../README.zh-CN.md).
Use Python 3.12 and a Codex desktop environment with the in-app browser, Computer Use,
and an authenticated ChatGPT session. This candidate was checked locally on macOS;
Windows, Linux, other agents, and other Python versions are not validated end to end.

Audit-provided shell commands remain ineligible for automatic execution without
trusted runtime attestation. Codex's independently reviewed local work uses the
original task authorization. The audit protocol and this safety boundary are preserved.

Created by **[X1IZHU](https://github.com/X1IZHU)** with assistance from Codex.
Released under the [MIT License](../LICENSE); commercial use is permitted and copies
or substantial portions must retain the copyright and permission notice. See [NOTICE](../NOTICE).
This is an independent community project with no OpenAI affiliation or endorsement.
An audit PASS is limited to the inspected scope and evidence; it is not a guarantee
of correctness or deployment approval.

---

## Publication scope

The author selected MIT after reviewing its commercial-use permission and explicitly
authorized public repository creation and publication of this first version.
Only this standalone project's publishable files belong in the repository or release
archive. Private audit state, unrelated projects, and local execution logs are excluded.

The installed source had no `.git` history, remotes, license, or release tags. This
repository starts a new local history rather than inventing earlier commits or versions.
