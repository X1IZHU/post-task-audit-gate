#!/usr/bin/env python3
from __future__ import annotations
import argparse, pathlib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--audit-type", required=True, choices=["INITIAL", "REMEDIATION_VERIFICATION", "XHS_USER_PANEL_REVIEW", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"])
    ap.add_argument("--profile", default="implementation", choices=["implementation", "xhs-user-panel"])
    ap.add_argument("--subject-id", default="{{AUDIT_SUBJECT_ID}}")
    ap.add_argument("--bundle-filename")
    ap.add_argument("--output")
    args = ap.parse_args()
    skill_root = pathlib.Path(__file__).resolve().parents[1]
    name = "xhs-browser-submit-prompt.md" if args.profile == "xhs-user-panel" else "browser-submit-prompt.md"
    bundle_filename = args.bundle_filename or f"AUDIT_BUNDLE_{args.task_id}_R{args.round:02d}.zip"
    text = (skill_root / "templates" / name).read_text(encoding="utf-8")
    text = (text.replace("{{TASK_ID}}", args.task_id)
                .replace("{{ROUND}}", str(args.round))
                .replace("{{ROUND_PADDED}}", f"{args.round:02d}")
                .replace("{{AUDIT_TYPE}}", args.audit_type)
                .replace("{{AUDIT_SUBJECT_ID}}", args.subject_id)
                .replace("{{BUNDLE_FILENAME}}", bundle_filename))
    if args.output:
        pathlib.Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)

if __name__ == "__main__":
    main()
