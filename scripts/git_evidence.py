"""Read Git's NUL-delimited status without shell or display-path parsing."""
from __future__ import annotations

import json
import pathlib
import subprocess


def parse_status(raw: bytes) -> list[dict[str, str]]:
    if raw and not raw.endswith(b"\0"):
        raise ValueError("Git status is not NUL terminated")
    records = raw.split(b"\0")[:-1]
    entries = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            raise ValueError("Malformed Git porcelain status record")
        status = record[:2].decode("ascii")
        path = record[3:].decode("utf-8")
        entry = {"status": status, "path": path}
        if "R" in status or "C" in status:
            if index >= len(records):
                raise ValueError("Git rename/copy is missing its original path")
            entry["previous_path"] = records[index].decode("utf-8")
            index += 1
        for value in (path, entry.get("previous_path", path)):
            candidate = pathlib.PurePosixPath(value)
            if not value or candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("Git returned a path outside the repository")
        # Audit state is private transport/working data, never source evidence.
        if ".codex-audit" not in pathlib.PurePosixPath(path).parts:
            entries.append(entry)
    return entries


def read_status(repo: pathlib.Path) -> list[dict[str, str]] | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=repo, capture_output=True, check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode:
        # An absent repository is supported; other Git failures must be visible.
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                               capture_output=True, check=False)
        if probe.returncode:
            return None
        raise ValueError("Git status failed: " + result.stderr.decode("utf-8", errors="replace"))
    top = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=repo,
                         capture_output=True, check=True).stdout.strip()
    if top:
        raise ValueError("--repo must name the Git worktree root")
    return parse_status(result.stdout)


def format_status(entries: list[dict[str, str]]) -> str:
    # Quote for human readability only; consumers use structured entries.
    return "".join(
        entry["status"] + " " + json.dumps(entry["path"], ensure_ascii=False)
        + (" (from " + json.dumps(entry["previous_path"], ensure_ascii=False) + ")"
           if "previous_path" in entry else "") + "\n"
        for entry in entries
    )
