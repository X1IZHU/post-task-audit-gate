#!/usr/bin/env python3
"""Deterministic integrity helpers for the post-task audit protocol.

This module deliberately uses only the Python standard library. It fingerprints
the candidate locally; it does not upload repository contents or interpret them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import zipfile
from typing import Any, Iterable

PROTOCOL_VERSION = 1
TASK_SPEC_CANONICALIZATION_VERSION = 1
TASK_SPEC_AUTHORITATIVE_FIELDS = ("original_task", "user_task_transcript")
TASK_SPEC_EXCLUDED_METADATA = (
    "task_id",
    "created_at",
    "repo_root",
    "branch",
    "head",
    "worktree_was_dirty",
    "baseline_quality",
    "max_audit_rounds",
    "max_evidence_expansion_rounds",
    "remediation_execution_mode",
    "strict_pass_only",
    "project_context_version",
    "project_context_fingerprint",
    "project_context_state",
    "round",
    "audit_type",
    "audit_subject_id",
    "bundle_manifest_hash",
    "browser_prompt",
    "artifact_filename",
    "handoff_metadata",
)
SKIP_DIRS = {".git", ".codex-audit", "node_modules", ".venv", "venv", "dist", "build", "coverage", ".cache"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
MAX_CENSUS_HASH_BYTES = 50 * 1024 * 1024
MUTABLE_CONTROL_FILES = {"STATE.json", "DOWNLOAD_PROVENANCE.json", "FINDINGS_FREEZE.json"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(repo: pathlib.Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    except OSError:
        return 127, ""
    return result.returncode, result.stdout.decode("utf-8", "replace")


def is_sensitive(path: pathlib.Path) -> bool:
    name = path.name.lower()
    return name in SENSITIVE_NAMES or name.startswith(".env.") or path.suffix.lower() in SENSITIVE_SUFFIXES


def is_skipped(path: pathlib.Path) -> str | None:
    for part in path.parts:
        if part in SKIP_DIRS:
            return part
    return None


def relpath(repo: pathlib.Path, path: pathlib.Path) -> str:
    return path.relative_to(repo).as_posix()


def tracked_paths(repo: pathlib.Path) -> set[str]:
    rc, output = run_git(repo, "ls-files", "-co", "--exclude-standard", "-z")
    if rc != 0:
        return set()
    return {item for item in output.split("\0") if item}


def iter_visible(repo: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    files: list[pathlib.Path] = []
    excluded: list[str] = []
    for root, dirs, names in os.walk(repo):
        root_path = pathlib.Path(root)
        kept: list[str] = []
        for name in sorted(dirs):
            candidate = root_path / name
            rel = relpath(repo, candidate)
            if name in SKIP_DIRS:
                excluded.append(rel)
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            candidate = root_path / name
            if candidate.is_file() or candidate.is_symlink():
                files.append(candidate)
    return files, sorted(set(excluded))


def file_metadata(repo: pathlib.Path, path: pathlib.Path, tracked: set[str], *, hash_sensitive: bool) -> dict[str, Any]:
    rel = relpath(repo, path)
    sensitive = is_sensitive(path)
    skipped = is_skipped(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    digest = None
    hash_reason = None
    if path.is_symlink():
        digest = sha256_bytes(os.readlink(path).encode("utf-8"))
    elif size is not None and (hash_sensitive or not sensitive) and size <= MAX_CENSUS_HASH_BYTES:
        try:
            digest = sha256_file(path)
        except OSError as exc:
            hash_reason = str(exc)
    elif size is not None and size > MAX_CENSUS_HASH_BYTES:
        hash_reason = "too_large_to_hash"
    elif sensitive:
        hash_reason = "sensitive_path"
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = None
    status = "EXCLUDED" if skipped else "VISIBLE"
    if not path.exists() and not path.is_symlink():
        status = "DELETED"
    return {
        "path": rel,
        "kind": "symlink" if path.is_symlink() else "file",
        "tracked": rel in tracked,
        "size": size,
        "sha256": digest,
        "classification": "SENSITIVE" if sensitive else status,
        "included_in_raw_evidence": False,
        "exclusion_reason": hash_reason or ("sensitive_file_policy" if sensitive else None),
        "sensitive": sensitive,
        "generated": any(part in {"generated", "dist", "build"} for part in path.parts),
        "binary": False,
        "mode": mode,
    }


def repository_census(repo: pathlib.Path) -> list[dict[str, Any]]:
    repo = repo.resolve()
    tracked = tracked_paths(repo)
    files, excluded = iter_visible(repo)
    entries = [file_metadata(repo, path, tracked, hash_sensitive=False) for path in files]
    for path in excluded:
        entries.append({
            "path": path,
            "kind": "directory",
            "tracked": False,
            "size": None,
            "sha256": None,
            "classification": "EXCLUDED_REGION",
            "included_in_raw_evidence": False,
            "exclusion_reason": "raw_evidence_skip_directory",
            "sensitive": False,
            "generated": path.split("/")[-1] in {"dist", "build"},
            "binary": None,
        })
    known = {entry["path"] for entry in entries}
    for path in sorted(tracked - known):
        entries.append({
            "path": path,
            "kind": "file",
            "tracked": True,
            "size": None,
            "sha256": None,
            "classification": "DELETED",
            "included_in_raw_evidence": False,
            "exclusion_reason": "tracked_path_missing_from_worktree",
            "sensitive": is_sensitive(pathlib.Path(path)),
            "generated": False,
            "binary": None,
        })
    return sorted(entries, key=lambda entry: (entry["path"], entry["kind"]))


def write_census(repo: pathlib.Path, output: pathlib.Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for entry in repository_census(repo):
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def worktree_manifest(repo: pathlib.Path) -> list[dict[str, Any]]:
    repo = repo.resolve()
    tracked = tracked_paths(repo)
    files, excluded = iter_visible(repo)
    entries = [file_metadata(repo, path, tracked, hash_sensitive=True) for path in files]
    for path in excluded:
        entries.append({"path": path, "kind": "directory", "status": "EXCLUDED_REGION"})
    known = {entry["path"] for entry in entries}
    for path in sorted(tracked - known):
        entries.append({"path": path, "kind": "file", "status": "DELETED", "tracked": True, "sha256": None})
    return sorted(entries, key=lambda entry: (entry["path"], entry["kind"]))


def worktree_fingerprint(repo: pathlib.Path) -> str:
    return sha256_bytes(canonical(worktree_manifest(repo)))


def task_spec_hash(task_text: str, transcript_text: str = "") -> str:
    return sha256_bytes(canonical(task_spec_payload(task_text, transcript_text)))


def task_spec_payload(task_text: str, transcript_text: str = "") -> dict[str, str]:
    """Return the only semantic inputs that define the implementation task.

    This intentionally excludes transport, audit-control, and repository identity
    metadata.  Both bundle construction and staleness checking must use this
    function (or the manifest wrapper below), never raw task.json bytes.
    """
    if not isinstance(task_text, str) or not isinstance(transcript_text, str):
        raise TypeError("task specification inputs must be UTF-8 strings")
    return {
        "original_task": task_text,
        "user_task_transcript": transcript_text,
    }


def task_spec_manifest(task: dict[str, Any], *, transcript_override: str | None = None) -> dict[str, Any]:
    """Build a deterministic, auditable manifest for the semantic task spec.

    The manifest records logical sources and individual value hashes without
    copying the task text into another evidence file.  The final hash is the
    hash of the canonical semantic payload only.
    """
    if not isinstance(task, dict):
        raise TypeError("task metadata must be a JSON object")
    task_text = task.get("original_task")
    if not isinstance(task_text, str):
        raise ValueError("task metadata is missing string original_task")
    stored_transcript = task.get("user_task_transcript", "")
    if not isinstance(stored_transcript, str):
        raise ValueError("task metadata user_task_transcript must be a string")
    transcript_text = stored_transcript if transcript_override is None else transcript_override
    if not isinstance(transcript_text, str):
        raise TypeError("task transcript override must be a UTF-8 string")

    payload = task_spec_payload(task_text, transcript_text)
    sources = []
    for logical_id in TASK_SPEC_AUTHORITATIVE_FIELDS:
        value = payload[logical_id]
        encoded = value.encode("utf-8")
        sources.append({
            "logical_id": logical_id,
            "source": f"task.json:{logical_id}",
            "sha256": sha256_bytes(encoded),
            "utf8_bytes": len(encoded),
        })
    return {
        "canonicalization_version": TASK_SPEC_CANONICALIZATION_VERSION,
        "authoritative_input_sources": sources,
        "excluded_metadata": list(TASK_SPEC_EXCLUDED_METADATA),
        "task_spec_hash": sha256_bytes(canonical(payload)),
    }


def task_spec_manifest_from_file(task_file: pathlib.Path, transcript_file: pathlib.Path | None = None) -> dict[str, Any]:
    """Load task metadata and apply an optional explicit transcript override."""
    try:
        task = json.loads(task_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load task metadata: {task_file}: {exc}") from exc
    transcript = transcript_file.read_text(encoding="utf-8") if transcript_file else None
    return task_spec_manifest(task, transcript_override=transcript)


def context_identity(repo: pathlib.Path) -> tuple[str, str, str]:
    candidates = [repo / ".codex" / "project-context.json", repo / ".codex-audit" / "project-context.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {"state": "UNREADABLE", "path": relpath(repo, path)}
        version = str(value.get("version", value.get("context_version", 1))) if isinstance(value, dict) else "1"
        fingerprint = sha256_bytes(canonical(value))
        return version, fingerprint, "PRESENT" if not context_bootstrap_errors(value) else "PRESENT_WITH_UNTRUSTED_SEMANTIC_ENTRIES"
    absent = {"state": "ABSENT", "version": 0, "entries": []}
    return "0", sha256_bytes(canonical(absent)), "ABSENT"


def context_bootstrap_errors(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["context must be a JSON object"]
    errors = []
    for index, entry in enumerate(value.get("entries", [])):
        if not isinstance(entry, dict):
            errors.append(f"entry {index} is not an object")
            continue
        if entry.get("kind") == "semantic" and entry.get("state") == "ACTIVE" and not entry.get("provenance"):
            errors.append(f"semantic entry {index} is ACTIVE without accepted provenance")
    return errors


def audit_subject(task_hash: str, worktree_hash: str, context_version: str, context_hash: str, manifest_hash: str, round_no: int, audit_profile: str = "IMPLEMENTATION") -> str:
    return sha256_bytes(canonical({
        "task_spec_hash": task_hash,
        "worktree_fingerprint": worktree_hash,
        "project_context_version": context_version,
        "project_context_fingerprint": context_hash,
        "bundle_manifest_hash": manifest_hash,
        "audit_protocol_version": PROTOCOL_VERSION,
        "round": round_no,
        "audit_profile": audit_profile,
    }))


def completion_integrity(record: dict[str, Any]) -> str:
    """Hash the completion record without a self-referential field."""
    return sha256_bytes(canonical({key: value for key, value in record.items() if key != "completion_content_hash"}))


def is_runtime_capture(path: pathlib.Path) -> bool:
    return path.name == "AUDIT_RESPONSE.md" or bool(
        re.fullmatch(r"(?:AUDIT_RESPONSE|REMEDIATION_MASTER_PROMPT)_.+_R\d{2}\.md", path.name)
    )


def bundle_entries(bundle: pathlib.Path) -> list[dict[str, Any]]:
    excluded = {"BUNDLE_MANIFEST.json", "FILE_MANIFEST.json", "BUNDLE_COMPLETE.json", "HASHES.sha256", "AUDIT_SUBJECT.json"} | MUTABLE_CONTROL_FILES
    result = []
    for path in sorted(p for p in bundle.rglob("*") if p.is_file() and p.name not in excluded and not is_runtime_capture(p)):
        rel = path.relative_to(bundle).as_posix()
        result.append({
            "path": rel,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "evidence_class": "control" if path.name in {"AUDIT_REQUEST.md", "AUDIT_RESPONSE_FORMAT.md"} else "evidence",
            "included": True,
            "exclusion_reason": None,
            "source_repository_path": rel.removeprefix("CHANGED_FILES/"),
        })
    return result


def write_bundle_hashes(bundle: pathlib.Path, manifest_hash: str) -> None:
    lines = []
    for path in sorted(p for p in bundle.rglob("*") if p.is_file() and p.name not in {"HASHES.sha256", "BUNDLE_COMPLETE.json"} | MUTABLE_CONTROL_FILES and not is_runtime_capture(p)):
        lines.append(f"{sha256_file(path)}  {path.relative_to(bundle).as_posix()}")
    lines.append(f"# BUNDLE_MANIFEST_HASH {manifest_hash}")
    (bundle / "HASHES.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_bundle(bundle: pathlib.Path) -> list[str]:
    errors: list[str] = []
    manifest_path = bundle / "BUNDLE_MANIFEST.json"
    complete_path = bundle / "BUNDLE_COMPLETE.json"
    if not manifest_path.is_file():
        return ["missing BUNDLE_MANIFEST.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid BUNDLE_MANIFEST.json: {exc}"]
    listed = {entry.get("path"): entry for entry in manifest.get("files", [])}
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file() and p.suffix.lower() != ".zip" and p.name not in {"BUNDLE_MANIFEST.json", "FILE_MANIFEST.json", "BUNDLE_COMPLETE.json", "HASHES.sha256", "AUDIT_SUBJECT.json"} and p.name not in MUTABLE_CONTROL_FILES and not is_runtime_capture(p)}
    included_paths = {path for path, entry in listed.items() if entry.get("included", True)}
    if included_paths != actual:
        errors.append("bundle files do not match BUNDLE_MANIFEST.json")
    for path, entry in listed.items():
        if not entry.get("included", True):
            continue
        candidate = bundle / path
        if candidate.is_file() and (entry.get("sha256") != sha256_file(candidate) or entry.get("size") != candidate.stat().st_size):
            errors.append(f"hash or size mismatch: {path}")
    if complete_path.is_file():
        try:
            complete = json.loads(complete_path.read_text(encoding="utf-8"))
            expected_manifest_hash = sha256_file(manifest_path)
            if complete.get("bundle_manifest_hash") != expected_manifest_hash:
                errors.append("BUNDLE_COMPLETE bundle_manifest_hash mismatch")
            if complete.get("state") != "COMPLETE":
                errors.append("BUNDLE_COMPLETE state is not COMPLETE")
            if "completion_content_hash" in complete and complete.get("completion_content_hash") != completion_integrity(complete):
                errors.append("BUNDLE_COMPLETE completion integrity mismatch")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid BUNDLE_COMPLETE.json: {exc}")
    else:
        errors.append("missing BUNDLE_COMPLETE.json")
    hashes_path = bundle / "HASHES.sha256"
    if hashes_path.is_file():
        for line in hashes_path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            digest, _, name = line.partition("  ")
            candidate = bundle / name
            if not candidate.is_file() or sha256_file(candidate) != digest:
                errors.append(f"HASHES.sha256 mismatch: {name}")
    else:
        errors.append("missing HASHES.sha256")
    return errors


def validate_bundle_archive(archive: pathlib.Path, bundle: pathlib.Path) -> list[str]:
    """Verify the deliverable ZIP is the same evidence that was validated on disk."""
    errors: list[str] = []
    if not archive.is_file() or archive.stat().st_size == 0:
        return ["missing or empty deliverable ZIP"]
    try:
        with zipfile.ZipFile(archive) as handle:
            names = set(handle.namelist())
            expected = {
                path.relative_to(bundle).as_posix()
                for path in bundle.rglob("*")
                if path.is_file() and path != archive and path.name not in MUTABLE_CONTROL_FILES and not is_runtime_capture(path)
            }
            if names != expected:
                errors.append("deliverable ZIP contents do not match validated bundle")
            for name in sorted(expected & names):
                with handle.open(name) as member:
                    digest = hashlib.sha256(member.read()).hexdigest()
                if digest != sha256_file(bundle / name):
                    errors.append(f"deliverable ZIP content hash mismatch: {name}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"invalid deliverable ZIP: {exc}")
    return errors


def validate_deliverable_bundle(bundle: pathlib.Path) -> list[str]:
    """Validate both the handoff directory and its expected, content-matching ZIP."""
    errors = validate_bundle(bundle)
    try:
        complete = json.loads((bundle / "BUNDLE_COMPLETE.json").read_text(encoding="utf-8"))
        task_id = complete["task_id"]
        round_no = int(complete["round"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return errors + [f"cannot determine expected deliverable ZIP: {exc}"]
    expected = bundle / f"AUDIT_BUNDLE_{task_id}_R{round_no:02d}.zip"
    errors.extend(validate_bundle_archive(expected, bundle))
    return errors


def check_staleness(bundle_complete: pathlib.Path, repo: pathlib.Path, task_file: pathlib.Path, transcript: pathlib.Path | None) -> list[str]:
    expected = json.loads(bundle_complete.read_text(encoding="utf-8"))
    # The builder hashes the semantic fields in task.json.  Reusing the same
    # manifest loader here prevents JSON formatting and transport metadata from
    # creating false task-spec staleness.
    task_hash = task_spec_manifest_from_file(task_file, transcript).get("task_spec_hash")
    context_version, context_hash, _ = context_identity(repo)
    current = {
        "task_spec_hash": task_hash,
        "worktree_fingerprint": worktree_fingerprint(repo),
        "project_context_version": context_version,
        "project_context_fingerprint": context_hash,
    }
    return [key for key, value in current.items() if expected.get(key) != value]


def parse_blocks(text: str, prefix: str) -> list[dict[str, Any]]:
    pattern = rf"^###\s+({prefix}-\d{{3,}})(?:\s+.*)?$"
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        item: dict[str, Any] = {"id": match.group(1)}
        for key, value in re.findall(r"^([A-Z][A-Z_ /-]+):\s*(.+)$", block, re.MULTILINE):
            item[key.lower().replace(" ", "_").replace("/", "_").replace("-", "_")] = value.strip()
        result.append(item)
    return result


def parse_evidence_requests(text: str) -> list[dict[str, Any]]:
    return parse_blocks(text, "ER")


def parse_remediation_plan(text: str) -> list[dict[str, Any]]:
    start = text.find("## Remediation Plan")
    if start < 0:
        return []
    return parse_blocks(text[start:], "PTA")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    census = sub.add_parser("census")
    census.add_argument("--repo", required=True)
    census.add_argument("--output", required=True)
    validate = sub.add_parser("validate-bundle")
    validate.add_argument("bundle")
    stale = sub.add_parser("check-staleness")
    stale.add_argument("--bundle-complete", required=True)
    stale.add_argument("--repo", required=True)
    stale.add_argument("--task-file", required=True)
    stale.add_argument("--task-transcript")
    evidence = sub.add_parser("parse-evidence-requests")
    evidence.add_argument("response")
    plan = sub.add_parser("parse-remediation-plan")
    plan.add_argument("response")
    context = sub.add_parser("validate-context")
    context.add_argument("context")
    args = ap.parse_args()
    if args.command == "census":
        write_census(pathlib.Path(args.repo).resolve(), pathlib.Path(args.output))
        print(args.output)
    elif args.command == "validate-bundle":
        errors = validate_bundle(pathlib.Path(args.bundle).resolve())
        if errors:
            print("BUNDLE_INVALID")
            for error in errors:
                print(f"- {error}")
            sys.exit(2)
        print("BUNDLE_VALID")
    elif args.command == "check-staleness":
        mismatches = check_staleness(pathlib.Path(args.bundle_complete), pathlib.Path(args.repo).resolve(), pathlib.Path(args.task_file), pathlib.Path(args.task_transcript) if args.task_transcript else None)
        if mismatches:
            print("AUDIT_RESULT=STALE")
            for key in mismatches:
                print(f"- {key}")
            sys.exit(2)
        print("AUDIT_RESULT=CURRENT")
    elif args.command == "parse-evidence-requests":
        print(json.dumps(parse_evidence_requests(pathlib.Path(args.response).read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    elif args.command == "parse-remediation-plan":
        print(json.dumps(parse_remediation_plan(pathlib.Path(args.response).read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    else:
        errors = context_bootstrap_errors(json.loads(pathlib.Path(args.context).read_text(encoding="utf-8")))
        if errors:
            print("CONTEXT_BOOTSTRAP_INVALID")
            for error in errors:
                print(f"- {error}")
            sys.exit(2)
        print("CONTEXT_BOOTSTRAP_VALID")


if __name__ == "__main__":
    main()
