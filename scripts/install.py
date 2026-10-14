#!/usr/bin/env python3
"""Install the existing skill files locally, without overwriting an installation."""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", type=pathlib.Path,
        default=pathlib.Path.home() / ".agents/skills/post-task-chatgpt-audit",
        help="Full destination skill directory (default: ~/.agents/skills/post-task-chatgpt-audit)",
    )
    args = parser.parse_args()
    source = pathlib.Path(__file__).resolve().parents[1]
    target = args.target.expanduser().absolute()
    required = ["SKILL.md", "EVIDENCE_POLICY.md", "scripts", "templates", "schema", "docs/PROTOCOL.md"]
    optional = ["README.md", "README.zh-CN.md", "LICENSE", "NOTICE", "examples"]
    selected = [source / item for item in required + optional if (source / item).exists()]
    missing = [item for item in required if not (source / item).exists()]
    if missing:
        parser.exit(2, "Missing required resources: " + ", ".join(missing) + "\n")
    # Ship the documentation linked from installed READMEs as well as the protocol.
    selected = [path for path in selected if path != source / "docs/PROTOCOL.md"]
    selected.append(source / "docs")
    if target.exists() or target.is_symlink():
        parser.exit(2, f"Destination already exists; nothing overwritten: {target}\nChoose --target for a separate installation.\n")
    if target.resolve() == source or source in target.resolve().parents:
        parser.exit(2, "Destination must be outside the source skill directory.\n")
    for path in selected:
        candidates = [path, *path.rglob("*")] if path.is_dir() else [path]
        if any(candidate.is_symlink() for candidate in candidates):
            parser.exit(2, f"Refusing symlinked installation resources: {path.name}\n")
    try:
        target.mkdir(parents=True, exist_ok=False)
        for path in selected:
            destination = target / path.relative_to(source)
            if path.is_dir():
                shutil.copytree(path, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    except OSError as error:
        # Preserve partial output for inspection; never delete user-owned paths.
        parser.exit(1, f"Install failed: {error}\nInspect any partial destination before retrying.\n")
    print(f"Installed: {target}")
    print("Select $post-task-chatgpt-audit in Codex. Restart Codex if it does not appear.")


if __name__ == "__main__":
    main()
