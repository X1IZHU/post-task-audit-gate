#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, os, pathlib, subprocess, uuid

from audit_integrity import context_identity, task_spec_manifest


def run(cmd, cwd):
    try:
        p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 127, "", str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--task-file", required=True, help="UTF-8 file containing the original task")
    ap.add_argument("--task-transcript", help="Optional authoritative task transcript")
    ap.add_argument("--task-id", default=None)
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    root = repo / ".codex-audit"
    baseline = root / "baseline"
    baseline.mkdir(parents=True, exist_ok=True)

    task_id = args.task_id or f"pta-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    task_text = pathlib.Path(args.task_file).read_text(encoding="utf-8")
    transcript_text = pathlib.Path(args.task_transcript).read_text(encoding="utf-8") if args.task_transcript else ""
    context_version, context_fingerprint, context_state = context_identity(repo)

    rc, branch, _ = run(["git", "branch", "--show-current"], repo)
    rc2, head, _ = run(["git", "rev-parse", "HEAD"], repo)
    git_ok = rc == 0 and rc2 == 0

    # Keep audit transport state out of normal worktree status without changing committed .gitignore.
    if git_ok:
        info_exclude = repo / ".git" / "info" / "exclude"
        try:
            existing = info_exclude.read_text(encoding="utf-8") if info_exclude.exists() else ""
            pattern = ".codex-audit/"
            if pattern not in {line.strip() for line in existing.splitlines()}:
                info_exclude.parent.mkdir(parents=True, exist_ok=True)
                with info_exclude.open("a", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(pattern + "\n")
        except Exception:
            pass

    status = diff = staged = untracked = ""
    if git_ok:
        _, status, _ = run(["git", "status", "--porcelain=v1"], repo)
        _, diff, _ = run(["git", "diff", "--no-ext-diff", "--binary", "HEAD"], repo)
        _, staged, _ = run(["git", "diff", "--cached", "--no-ext-diff", "--binary", "HEAD"], repo)
        _, untracked, _ = run(["git", "ls-files", "--others", "--exclude-standard"], repo)

    (baseline / "STATUS.txt").write_text(status, encoding="utf-8")
    (baseline / "DIFF.patch").write_text(diff, encoding="utf-8")
    (baseline / "STAGED_DIFF.patch").write_text(staged, encoding="utf-8")
    (baseline / "UNTRACKED.txt").write_text(untracked, encoding="utf-8")

    data = {
        "protocol_version": 1,
        "task_id": task_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repo_root": str(repo),
        "original_task": task_text,
        "user_task_transcript": transcript_text,
        "task_spec_hash": task_spec_manifest({
            "original_task": task_text,
            "user_task_transcript": transcript_text,
        })["task_spec_hash"],
        "git_available": git_ok,
        "branch": branch.strip() if git_ok else None,
        "head": head.strip() if git_ok else None,
        "worktree_was_dirty": bool(status.strip()) if git_ok else None,
        "baseline_quality": "EARLY_INITIALIZATION",
        "max_audit_rounds": 3,
        "max_goal_iterations": 4,
        "max_evidence_expansion_rounds": 2,
        "remediation_execution_mode": "GUIDED",
        "strict_pass_only": True,
        "project_context_version": context_version,
        "project_context_fingerprint": context_fingerprint,
        "project_context_state": context_state,
        "bootstrap_policy": "mechanical facts may be AUTO_ACTIVE; semantic entries start BOOTSTRAP_PROPOSED until independently accepted"
    }
    (root / "task.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(task_id)

if __name__ == "__main__":
    main()
