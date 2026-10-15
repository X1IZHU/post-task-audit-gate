#!/usr/bin/env python3
"""Build a conservative post-task audit bundle.

This helper intentionally avoids clever secret redaction. Sensitive candidates are omitted
and recorded in the manifest so the external auditor can BLOCK rather than be given secrets.
Codex should inspect the generated bundle before browser upload.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, pathlib, re, shutil, subprocess, uuid, zipfile

from audit_integrity import (
    audit_subject,
    bundle_entries,
    completion_integrity,
    context_identity,
    sha256_file,
    task_spec_manifest,
    validate_bundle_archive,
    validate_deliverable_bundle,
    worktree_fingerprint,
    write_bundle_hashes,
    write_census,
)
from validate_audit_response import (
    EXECUTION_POLICIES,
    actionable_finding_ids,
    exact_header,
    expected_findings_from_file,
    finding_namespace_errors,
    named_blocks,
    prior_closure_states,
    xhs_closure_ids,
    xhs_finding_ids,
    xhs_finding_id_errors,
    xhs_prior_closure_states,
    scalar,
    section,
)
from transport_policy import new_transport_state
import audit_integrity
import remediation_protocol
from git_evidence import format_status, read_status

SENSITIVE_NAMES = {
    ".env", ".npmrc", ".pypirc", "credentials", "credentials.json",
    "secrets.json", "id_rsa", "id_ed25519"
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
SKIP_DIRS = {".git", ".codex-audit", "node_modules", ".venv", "venv", "dist", "build", ".next", ".cache"}
MAX_TEXT = 1024 * 1024
REMEDIATION_VERDICTS = {"FAIL", "PASS_WITH_NOTES"}
XHS_PROFILE = "XHS_USER_PANEL"
XHS_ALLOWED_SOURCE_CLASSES = {"OWNED_MARKETING_ASSET", "PUBLIC_USER_ASSET", "GENERATED_ASSET", "LICENSED_ASSET"}
UNRESOLVED_STATES = {"OPEN", "PARTIALLY_CLOSED", "DISPUTED_BUT_UNRESOLVED"}
REPORT_POSITIONS = {"REMEDIATED", "DISPUTED", "NOT_APPLICABLE_WITH_EVIDENCE"}
REPORT_EXECUTION_STATUSES = {"EXECUTED", "ADAPTED_AFTER_PREFLIGHT", "NOT_EXECUTED"}
REPORT_PROMPT_STATUSES = {"YES", "ADAPTED", "NO"}


def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    return p.returncode, p.stdout, p.stderr


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sensitive(path: pathlib.Path):
    low = path.name.lower()
    if low in SENSITIVE_NAMES or low.startswith(".env."):
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    return False


def text_bytes(path):
    try:
        b = path.read_bytes()
        b.decode("utf-8")
        return b
    except Exception:
        return None


def snapshot_entry(repo, out, rel, role, *, deleted=False, previous=None):
    """Classify omissions separately from unexpected required-file failures."""
    entry = {"repository_path": rel, "role": role}
    if previous is not None:
        entry["previous_repository_path"] = previous
    candidate = pathlib.Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        entry.update(status="DENIED_PATH", reason="outside repository boundary")
        return entry, None
    try:
        source = (repo / candidate).resolve()
        source_rel = source.relative_to(repo)
    except (OSError, RuntimeError) as exc:
        entry.update(status="READ_ERROR", reason=type(exc).__name__)
        return entry, None
    except ValueError:
        entry.update(status="DENIED_PATH", reason="outside repository boundary")
        return entry, None
    if sensitive(candidate) or sensitive(source_rel) or (previous and sensitive(pathlib.Path(previous))):
        entry.update(status="OMITTED_SENSITIVE", reason="sensitive path policy")
        return entry, None
    try:
        if not source.exists():
            entry.update(status="DELETED" if deleted else "READ_ERROR",
                         reason="Git records deletion" if deleted else "expected file is missing")
            return entry, None
        if not source.is_file():
            entry.update(status="UNSUPPORTED", reason="expected path is not a regular file")
            return entry, None
        content = source.read_bytes()
    except OSError as exc:
        entry.update(status="READ_ERROR", reason=type(exc).__name__)
        return entry, None
    entry.update(size=len(content), sha256=sha256_bytes(content))
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        entry.update(status="OMITTED_BINARY", reason="not UTF-8 text")
        return entry, None
    if len(content) > MAX_TEXT:
        entry.update(status="OMITTED_TOO_LARGE", reason="text exceeds snapshot limit")
        return entry, content
    directory = "CHANGED_FILES" if role == "changed" else "REQUESTED_EVIDENCE"
    dst = out / directory / candidate
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(content)
    entry.update(status="COMPLETE", bundle_path=dst.relative_to(out).as_posix())
    return entry, content


def test_provenance_errors(path: pathlib.Path, repo: pathlib.Path, expected_fingerprint: str) -> list[str]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"test provenance is invalid: {exc}"]
    errors: list[str] = []
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return ["test provenance schema_version must be 1"]
    if record.get("candidate_worktree_fingerprint") != expected_fingerprint:
        errors.append("test provenance candidate fingerprint does not match current worktree")
    runs = record.get("runs")
    if not isinstance(runs, list) or not runs:
        return errors + ["test provenance must contain at least one run"]
    names = set()
    for index, run_record in enumerate(runs):
        if not isinstance(run_record, dict):
            errors.append(f"test provenance run {index} is not an object")
            continue
        name = run_record.get("name")
        if not isinstance(name, str) or not name or name in names:
            errors.append(f"test provenance run {index} has missing or duplicate name")
        names.add(name)
        command = run_record.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            errors.append(f"test provenance run {index} command is invalid")
        try:
            cwd = pathlib.Path(run_record.get("cwd", "")).resolve()
        except (TypeError, OSError):
            cwd = pathlib.Path()
        if cwd != repo.resolve():
            errors.append(f"test provenance run {index} cwd is not the candidate repository")
        if run_record.get("exit_code") != 0:
            errors.append(f"test provenance run {index} did not succeed")
        for field in ("pre_run_worktree_fingerprint", "post_run_worktree_fingerprint"):
            if run_record.get(field) != expected_fingerprint:
                errors.append(f"test provenance run {index} {field} does not match current worktree")
        output_path = run_record.get("output_path")
        if not isinstance(output_path, str) or not output_path:
            errors.append(f"test provenance run {index} output_path is missing")
        else:
            output = (repo / output_path).resolve()
            try:
                output.relative_to(repo.resolve())
            except ValueError:
                errors.append(f"test provenance run {index} output_path is outside repository")
            else:
                if not output.is_file():
                    errors.append(f"test provenance run {index} output is missing: {output_path}")
    return errors


def write_split_evidence(out: pathlib.Path, candidate: pathlib.Path, content: bytes):
    """Keep explicitly requested large text evidence reviewable without lifting MAX_TEXT."""
    destination = out / "REQUESTED_EVIDENCE" / candidate
    destination.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, start in enumerate(range(0, len(content), MAX_TEXT), start=1):
        chunk = content[start:start + MAX_TEXT]
        part = destination.with_name(f"{destination.name}.part-{index:03d}")
        part.write_bytes(chunk)
        parts.append({"bundle_path": str(part.relative_to(out)), "size": len(chunk), "sha256": sha256_bytes(chunk)})
    index_path = destination.with_name(f"{destination.name}.parts.json")
    index_path.write_text(json.dumps({"source": candidate.as_posix(), "sha256": sha256_bytes(content), "parts": parts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(index_path.relative_to(out)), parts


def prior_actionable_ids(previous_text: str, authoritative_ids: list[str] | None = None) -> tuple[list[str], list[str]]:
    values, header_errors = exact_header(previous_text)
    audit_type = values.get("AUDIT_TYPE")
    errors = list(header_errors)
    if audit_type in {"XHS_USER_PANEL_REVIEW", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"}:
        errors.extend(xhs_finding_id_errors(previous_text))
        errors.extend(finding_namespace_errors(previous_text, True))
        ids = set(xhs_finding_ids(previous_text))
        closure_ids = authoritative_ids if authoritative_ids is not None else xhs_closure_ids(previous_text)
        closure_states, closure_errors = xhs_prior_closure_states(previous_text, closure_ids)
        errors.extend(closure_errors)
    else:
        errors.extend(finding_namespace_errors(previous_text, False))
        ids = set(actionable_finding_ids(previous_text))
        closure_states, closure_errors = prior_closure_states(previous_text, authoritative_ids)
        errors.extend(closure_errors)
    ids.update(
        finding_id for finding_id, state in closure_states.items()
        if state in UNRESOLVED_STATES
    )
    return sorted(ids), errors


def previous_round_errors(
    round_dir: pathlib.Path,
    task_id: str,
    round_no: int,
    audit_type: str,
    previous_audit: pathlib.Path | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        subject = json.loads((round_dir / "AUDIT_SUBJECT.json").read_text(encoding="utf-8"))
        complete = json.loads((round_dir / "BUNDLE_COMPLETE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"previous round control metadata is invalid: {exc}"]
    expected_profile = "XHS_USER_PANEL" if audit_type.startswith("XHS_") else "IMPLEMENTATION"
    expected_subject = audit_subject(
        subject.get("task_spec_hash", ""), subject.get("worktree_fingerprint", ""),
        subject.get("project_context_version", ""), subject.get("project_context_fingerprint", ""),
        subject.get("bundle_manifest_hash", ""), subject.get("round", 0), subject.get("audit_profile", ""),
    )
    checks = {"task_id": task_id, "round": round_no, "audit_type": audit_type, "audit_profile": expected_profile}
    for key, expected in checks.items():
        if subject.get(key) != expected:
            errors.append(f"previous round subject {key} mismatch")
        if complete.get(key) != expected:
            errors.append(f"previous round completion {key} mismatch")
    if subject.get("audit_subject_id") != expected_subject:
        errors.append("previous round subject hash is invalid")
    if complete.get("audit_subject_id") != subject.get("audit_subject_id"):
        errors.append("previous round completion subject mismatch")
    shared_fields = (
        "audit_protocol_version", "audit_subject_id", "task_id", "round", "audit_type", "audit_profile",
        "task_spec_hash", "worktree_fingerprint", "project_context_version", "project_context_fingerprint",
        "project_context_state", "bundle_manifest_hash",
    )
    for key in shared_fields:
        if complete.get(key) != subject.get(key):
            errors.append(f"previous round completion {key} is not bound to subject")
    if complete.get("state") != "COMPLETE":
        errors.append("previous round completion state is not COMPLETE")
    if complete.get("completion_content_hash") != completion_integrity(complete):
        errors.append("previous round completion integrity hash is invalid")
    state_path = round_dir / "DOWNLOAD_PROVENANCE.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        state = {}
        errors.append(f"previous round transport state is invalid: {exc}")
    if state.get("download_verified") is not True:
        errors.append("previous round has no verified downloaded response artifact")
    downloaded_path = state.get("downloaded_path")
    if not isinstance(downloaded_path, str) or not pathlib.Path(downloaded_path).is_file():
        errors.append("previous round downloaded response artifact is missing")
    else:
        if state.get("download_sha256") != sha256_file(pathlib.Path(downloaded_path)):
            errors.append("previous round downloaded response artifact hash mismatch")
        if previous_audit is not None and pathlib.Path(downloaded_path).resolve() != previous_audit.resolve():
            errors.append("previous audit is not the exact verified downloaded response artifact")
    manifest = round_dir / "BUNDLE_MANIFEST.json"
    actual_manifest_hash = sha256_file(manifest) if manifest.is_file() else None
    if not actual_manifest_hash or subject.get("bundle_manifest_hash") != actual_manifest_hash or complete.get("bundle_manifest_hash") != actual_manifest_hash:
        errors.append("previous round manifest hash is invalid")
    hashes = round_dir / "HASHES.sha256"
    if not hashes.is_file():
        return errors + ["previous round is missing HASHES.sha256"]
    declared = set()
    for line in hashes.read_text(encoding="utf-8").split("\n"):
        if not line or line.startswith("#"):
            continue
        try:
            digest, name = audit_integrity.parse_checksum_record(line)
        except ValueError as exc:
            errors.append(f"previous round: {exc}")
            continue
        declared.add(name)
        candidate = round_dir / name
        if not candidate.is_file() or sha256_file(candidate) != digest:
            errors.append(f"previous round hash mismatch: {name}")
    if "AUDIT_SUBJECT.json" not in declared:
        errors.append("previous round hash list does not cover AUDIT_SUBJECT.json")
    try:
        manifest_entries_list = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
        manifest_entries = {item.get("path"): item for item in manifest_entries_list}
        if not isinstance(complete.get("file_count"), int) or complete["file_count"] != len(manifest_entries_list):
            errors.append("previous round completion file_count does not match manifest")
    except (OSError, json.JSONDecodeError, AttributeError):
        manifest_entries = {}
        errors.append("previous round manifest is invalid")
    expected_file = round_dir / "EXPECTED_PREVIOUS_FINDINGS.json"
    if expected_file.is_file():
        entry = manifest_entries.get("EXPECTED_PREVIOUS_FINDINGS.json")
        if not entry or entry.get("sha256") != sha256_file(expected_file) or entry.get("size") != expected_file.stat().st_size:
            errors.append("previous round authoritative findings are not frozen by the manifest")
    return errors


def previous_ready_commands(previous_text: str) -> dict[str, list[str]]:
    pack_section = section(previous_text, "Remediation Execution Pack", "Test and Verification Assessment")
    result: dict[str, list[str]] = {}
    for finding_id, block in named_blocks(pack_section, "PTA"):
        if scalar(block, "COMMAND_STATUS") == "READY":
            result[finding_id] = [command_id for command_id, _ in named_blocks(block, "CMD", heading_level=4)]
    return result


def report_field(block: str, name: str, field_names: tuple[str, ...]) -> str | None:
    match = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", block, re.MULTILINE)
    if not match:
        return None
    inline = match.group(1).strip()
    tail = block[match.end():]
    boundary = re.search(
        rf"^(?:{'|'.join(re.escape(field) for field in field_names)}):|^###\s+CMD-",
        tail,
        re.MULTILINE,
    )
    continuation = tail[:boundary.start() if boundary else len(tail)].strip()
    return "\n".join(part for part in (inline, continuation) if part).strip()


def remediation_report_errors(previous_text: str, report_text: str) -> list[str]:
    errors: list[str] = []
    actionable_ids, closure_errors = prior_actionable_ids(previous_text)
    errors.extend(closure_errors)
    report_blocks = named_blocks(report_text, "PTA", heading_level=2)
    block_map = {finding_id: block for finding_id, block in report_blocks}
    if len(block_map) != len(report_blocks):
        errors.append("REMEDIATION_REPORT.md contains duplicate finding sections")

    top_fields = (
        "Codex position", "Execution pack status", "Low-capability execution prompt followed",
        "Changes made", "Evidence", "Verification",
    )
    ready_commands = previous_ready_commands(previous_text)
    for finding_id in actionable_ids:
        block = block_map.get(finding_id)
        if block is None:
            errors.append(f"REMEDIATION_REPORT.md is missing section {finding_id}")
            continue
        values = {name: report_field(block, name, top_fields) for name in top_fields}
        for name, value in values.items():
            if not value:
                errors.append(f"REMEDIATION_REPORT.md {finding_id} is missing {name}")
        if values["Codex position"] not in REPORT_POSITIONS:
            errors.append(f"REMEDIATION_REPORT.md {finding_id} has invalid Codex position")
        if values["Execution pack status"] not in REPORT_EXECUTION_STATUSES:
            errors.append(f"REMEDIATION_REPORT.md {finding_id} has invalid Execution pack status")
        if values["Low-capability execution prompt followed"] not in REPORT_PROMPT_STATUSES:
            errors.append(f"REMEDIATION_REPORT.md {finding_id} has invalid prompt-followed status")

        command_blocks = named_blocks(block, "CMD", heading_level=3)
        command_ids = [command_id for command_id, _ in command_blocks]
        for command_id in ready_commands.get(finding_id, []):
            if command_ids.count(command_id) != 1:
                errors.append(
                    f"REMEDIATION_REPORT.md {finding_id} command record count for {command_id} "
                    f"is {command_ids.count(command_id)}, expected 1"
                )
        for command_id, command_block in command_blocks:
            required = (
                "Command", "Working directory", "Preflight", "Execution policy", "Exit code",
                "Result", "Changed files", "Failure handling",
            )
            fields = {name: scalar(command_block, name) for name in required}
            for name, value in fields.items():
                if not value:
                    errors.append(f"REMEDIATION_REPORT.md {finding_id}/{command_id} is missing {name}")
            if fields["Preflight"] not in {"PASS", "FAIL"}:
                errors.append(f"REMEDIATION_REPORT.md {finding_id}/{command_id} has invalid Preflight")
            if fields["Execution policy"] not in EXECUTION_POLICIES | {"DO_NOT_RUN"}:
                errors.append(f"REMEDIATION_REPORT.md {finding_id}/{command_id} has invalid Execution policy")
            if fields["Exit code"] and not re.fullmatch(r"-?\d+|NOT_RUN", fields["Exit code"]):
                errors.append(f"REMEDIATION_REPORT.md {finding_id}/{command_id} has invalid Exit code")
    return errors


def validate_round_inputs(
    round_no: int,
    previous_audit: str | None,
    remediation_report: str | None,
    current_is_xhs: bool = False,
    expected_handoff_id: str | None = None,
) -> tuple[str | None, str | None]:
    if round_no == 1:
        return None, None
    if not previous_audit:
        raise ValueError("remediation-verification rounds require --previous-audit")
    previous_path = pathlib.Path(previous_audit)
    if not previous_path.is_file():
        raise ValueError(f"previous audit does not exist: {previous_audit}")
    previous_text = previous_path.read_text(encoding="utf-8")
    values, header_errors = exact_header(previous_text)
    if header_errors:
        raise ValueError("previous audit has invalid header:\n- " + "\n- ".join(header_errors))
    verdict = values.get("VERDICT")
    if not verdict:
        raise ValueError("previous audit is missing the leading VERDICT field")

    report_text: str | None = None
    is_xhs = values.get("AUDIT_TYPE") in {"XHS_USER_PANEL_REVIEW", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"}
    if is_xhs != current_is_xhs:
        raise ValueError("previous audit profile does not match current build profile")
    if expected_handoff_id is not None and values.get("HANDOFF_ID") != expected_handoff_id:
        raise ValueError("previous audit task does not match current handoff")
    if values.get("ROUND") != str(round_no - 1):
        raise ValueError(f"previous audit round must be {round_no - 1}")
    expected_type = (
        "XHS_USER_PANEL_REVIEW" if round_no == 2 else "XHS_USER_PANEL_REMEDIATION_VERIFICATION"
    ) if current_is_xhs else ("INITIAL" if round_no == 2 else "REMEDIATION_VERIFICATION")
    if values.get("AUDIT_TYPE") != expected_type:
        raise ValueError(f"previous audit type must be {expected_type}")
    if expected_handoff_id is not None or (previous_path.parent / "AUDIT_SUBJECT.json").exists():
        identity_errors = previous_round_errors(
            previous_path.parent,
            expected_handoff_id or values["HANDOFF_ID"],
            round_no - 1,
            values["AUDIT_TYPE"],
            previous_path,
        )
        try:
            prior_subject = json.loads((previous_path.parent / "AUDIT_SUBJECT.json").read_text(encoding="utf-8"))
            if values.get("AUDIT_SUBJECT_ID") != prior_subject.get("audit_subject_id"):
                identity_errors.append("previous audit response subject does not match frozen subject")
        except (OSError, json.JSONDecodeError):
            pass
        if identity_errors:
            raise ValueError("invalid previous round identity:\n- " + "\n- ".join(identity_errors))
    authoritative_ids = None
    expected_file = previous_path.parent / "EXPECTED_PREVIOUS_FINDINGS.json"
    if round_no > 2:
        if not expected_file.is_file():
            raise ValueError(f"previous round is missing authoritative findings: {expected_file}")
        authoritative_ids, expected_errors = expected_findings_from_file(expected_file, "XHS" if current_is_xhs else "PTA")
        if expected_errors:
            raise ValueError("invalid authoritative previous findings:\n- " + "\n- ".join(expected_errors))
    if verdict in REMEDIATION_VERDICTS:
        if not remediation_report:
            raise ValueError(f"previous verdict {verdict} requires --remediation-report")
        report_path = pathlib.Path(remediation_report)
        if not report_path.is_file():
            raise ValueError(f"remediation report does not exist: {remediation_report}")
        report_text = report_path.read_text(encoding="utf-8")
        prior_ids, lineage_errors = prior_actionable_ids(previous_text, authoritative_ids)
        errors = list(lineage_errors)
        if not is_xhs and not prior_ids:
            errors.append("previous remediation verdict has no canonical actionable findings")
        if not is_xhs:
            errors.extend(remediation_report_errors(previous_text, report_text))
        if is_xhs:
            for finding_id in prior_ids:
                block = re.search(rf"^##\s+{re.escape(finding_id)}\b(.*?)(?=^##\s+|\Z)", report_text, re.MULTILINE | re.DOTALL)
                if not block:
                    errors.append(f"XHS REMEDIATION_REPORT.md is missing section {finding_id}")
                    continue
                for field in ("Decision", "Before", "Change", "Why", "Evidence"):
                    if not re.search(rf"^{field}:\s*.+", block.group(1), re.MULTILINE):
                        errors.append(f"XHS REMEDIATION_REPORT.md {finding_id} is missing {field}")
                if re.search(r"^Decision:\s*REJECT\s*$", block.group(1), re.MULTILINE) and not re.search(r"^REJECTION_REASON:\s*.+", block.group(1), re.MULTILINE):
                    errors.append(f"XHS REMEDIATION_REPORT.md {finding_id} REJECT requires REJECTION_REASON")
        if errors:
            raise ValueError("invalid remediation report:\n- " + "\n- ".join(errors))
    return previous_text, report_text


def copy_round_lineage(
    out: pathlib.Path,
    previous_audit: str | None,
    remediation_report: str | None,
    previous_text: str | None,
    remediation_master_prompt: str | None = None,
) -> None:
    if previous_audit:
        shutil.copy2(previous_audit, out / "PREVIOUS_AUDIT_RESPONSE.md")
        authoritative_ids = None
        if previous_audit:
            expected_file = pathlib.Path(previous_audit).parent / "EXPECTED_PREVIOUS_FINDINGS.json"
            if expected_file.is_file():
                authoritative_ids, expected_errors = expected_findings_from_file(
                    expected_file,
                    "XHS" if (previous_text and exact_header(previous_text)[0].get("AUDIT_TYPE", "").startswith("XHS_")) else "PTA",
                )
                if expected_errors:
                    raise ValueError("invalid authoritative previous findings:\n- " + "\n- ".join(expected_errors))
        prior_ids, lineage_errors = prior_actionable_ids(previous_text or "", authoritative_ids)
        if lineage_errors:
            raise ValueError("previous audit lineage is invalid:\n- " + "\n- ".join(lineage_errors))
        (out / "EXPECTED_PREVIOUS_FINDINGS.json").write_text(
            json.dumps({"finding_ids": prior_ids}, indent=2), encoding="utf-8"
        )
    if remediation_report:
        shutil.copy2(remediation_report, out / "REMEDIATION_REPORT.md")
    if remediation_master_prompt:
        shutil.copy2(remediation_master_prompt, out / "PREVIOUS_REMEDIATION_MASTER_PROMPT.md")
        freeze = pathlib.Path(remediation_master_prompt).resolve().parent / "FINDINGS_FREEZE.json"
        if freeze.is_file():
            shutil.copy2(freeze, out / "PREVIOUS_FINDINGS_FREEZE.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--implementation-report", required=True)
    ap.add_argument("--test-results", required=True)
    ap.add_argument("--previous-audit")
    ap.add_argument("--remediation-report")
    ap.add_argument("--remediation-master-prompt", help="Validated master prompt that drove the previous remediation")
    ap.add_argument("--profile", default="implementation", choices=["implementation", "xhs-user-panel"])
    ap.add_argument("--content-file", action="append", help="XHS: safe repository-relative final post evidence")
    ap.add_argument("--asset", action="append", help="XHS: asset_id:path:role:source_class:publication_allowed")
    ap.add_argument("--evidence-path", action="append", help="Additional safe repository-relative evidence path")
    ap.add_argument("--test-provenance", help="JSON execution attestation for final local test runs")
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    audit_root = repo / ".codex-audit"
    task = json.loads((audit_root / "task.json").read_text(encoding="utf-8"))
    task_id = task["task_id"]
    task_spec = task_spec_manifest(task)
    task_hash = task_spec["task_spec_hash"]
    stored_task_hash = task.get("task_spec_hash")
    if stored_task_hash and stored_task_hash != task_hash:
        raise ValueError(
            "task.json task_spec_hash disagrees with the canonical TASK_SPEC_MANIFEST: "
            f"stored={stored_task_hash} computed={task_hash}"
        )
    transcript = task.get("user_task_transcript", "")
    context_version, context_hash, context_state = context_identity(repo)
    candidate_worktree_hash = worktree_fingerprint(repo)
    test_provenance_path = pathlib.Path(args.test_provenance).resolve() if args.test_provenance else None
    if test_provenance_path:
        try:
            test_provenance_path.relative_to(repo)
        except ValueError:
            raise ValueError("--test-provenance must be inside the repository")
        if not test_provenance_path.is_file():
            raise ValueError(f"test provenance does not exist: {args.test_provenance}")
        provenance_errors = test_provenance_errors(test_provenance_path, repo, candidate_worktree_hash)
        if provenance_errors:
            raise ValueError("invalid test provenance:\n- " + "\n- ".join(provenance_errors))
    round_no = args.round
    is_xhs = args.profile == "xhs-user-panel"
    audit_profile = XHS_PROFILE if is_xhs else "IMPLEMENTATION"
    audit_type = ("XHS_USER_PANEL_REVIEW" if round_no == 1 else "XHS_USER_PANEL_REMEDIATION_VERIFICATION") if is_xhs else ("INITIAL" if round_no == 1 else "REMEDIATION_VERIFICATION")
    if is_xhs and not (args.content_file or args.asset):
        raise ValueError("XHS profile requires --content-file or --asset; do not default to repository evidence")
    if is_xhs and round_no > task.get("max_goal_iterations", 4):
        raise ValueError("XHS profile exceeds max_goal_iterations")
    previous_text, _ = validate_round_inputs(round_no, args.previous_audit, args.remediation_report, is_xhs, task_id)
    previous_master_path = pathlib.Path(args.remediation_master_prompt).resolve() if args.remediation_master_prompt else None
    if round_no > 1 and not is_xhs and previous_text:
        previous_verdict = exact_header(previous_text)[0].get("VERDICT")
        if previous_verdict in REMEDIATION_VERDICTS:
            if previous_master_path is None or not previous_master_path.is_file():
                raise ValueError("remediation-verification rounds require --remediation-master-prompt after FAIL/PASS_WITH_NOTES")
            previous_freeze = pathlib.Path(args.previous_audit).resolve().parent / "FINDINGS_FREEZE.json"
            master_result = remediation_protocol.validate_master_prompt(
                previous_master_path, pathlib.Path(args.previous_audit).resolve(), previous_freeze,
                exact_header(previous_text)[0].get("AUDIT_SUBJECT_ID"),
            )
            if not master_result.get("valid"):
                raise ValueError("invalid remediation master prompt:\n- " + "\n- ".join(master_result.get("errors", [])))
            report_errors = remediation_protocol.report_errors(
                pathlib.Path(args.remediation_report).resolve(), pathlib.Path(args.previous_audit).resolve(), previous_master_path,
            )
            if report_errors:
                raise ValueError("invalid master-driven remediation report:\n- " + "\n- ".join(report_errors))
    final_out = audit_root / "sessions" / task_id / f"round-{round_no:02d}"
    if final_out.exists():
        expected_zip = final_out / f"AUDIT_BUNDLE_{task_id}_R{round_no:02d}.zip"
        existing_errors = validate_deliverable_bundle(final_out) if expected_zip.is_file() else ["expected deliverable ZIP is missing"]
        if not existing_errors:
            print(expected_zip)
            return
        raise ValueError("audit round already exists and is incomplete or conflicting; use a new round: " + "; ".join(existing_errors))
    out = final_out.with_name(f".{final_out.name}.staging-{uuid.uuid4().hex}")
    out.parent.mkdir(parents=True, exist_ok=True)
    (out / "CHANGED_FILES").mkdir(parents=True, exist_ok=True)

    (out / "ORIGINAL_TASK.md").write_text(task["original_task"], encoding="utf-8")
    if transcript:
        (out / "USER_TASK_TRANSCRIPT.md").write_text(transcript, encoding="utf-8")
    (out / "TASK_SPEC_MANIFEST.json").write_text(
        json.dumps(task_spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copy2(args.implementation_report, out / "IMPLEMENTATION_REPORT.md")
    shutil.copy2(args.test_results, out / "TEST_RESULTS.md")
    if test_provenance_path:
        shutil.copy2(test_provenance_path, out / "TEST_PROVENANCE.json")

    base = audit_root / "baseline"
    if (base / "STATUS.txt").exists(): shutil.copy2(base / "STATUS.txt", out / "BASELINE_STATUS.txt")

    entries = read_status(repo)
    (out / "WORKTREE_STATUS.txt").write_text(
        format_status(entries) if entries is not None else "Git unavailable\n", encoding="utf-8")
    baseline_quality = task.get("baseline_quality", "UNKNOWN")
    if baseline_quality not in {"UNKNOWN", "EARLY_INITIALIZATION", "LATE_INITIALIZATION"}:
        baseline_quality = "UNKNOWN"
    (out / "BASELINE_QUALITY.txt").write_text(
        f"BASELINE_QUALITY: {baseline_quality}\n"
        "This is a declared capture time, not inferred from worktree dirtiness. "
        "UNKNOWN/LATE cannot establish a clean pre-implementation baseline.\n", encoding="utf-8")
    manifest = []
    safe_changed = set()
    for item in entries or []:
        rel = item["path"]
        entry, _ = snapshot_entry(repo, out, rel, "changed",
                                  deleted="D" in item["status"], previous=item.get("previous_path"))
        entry["git_status"] = item["status"]
        manifest.append(entry)
        if entry["status"] in {"COMPLETE", "DELETED", "OMITTED_BINARY", "OMITTED_TOO_LARGE"}:
            safe_changed.add(rel)
            if item.get("previous_path"):
                safe_changed.add(item["previous_path"])
    if entries is not None and safe_changed:
        rc2, final_diff, _ = run(["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--binary", "HEAD", "--", *sorted(safe_changed)], repo)
        (out / "FINAL_DIFF.patch").write_text(final_diff if rc2 == 0 else "Git diff unavailable (for example, unborn HEAD). See file snapshots.\n", encoding="utf-8")
    else:
        (out / "FINAL_DIFF.patch").write_text("No safe tracked diff available. See FILE_MANIFEST.json and CHANGED_FILES.\n", encoding="utf-8")

    # Baseline diff is stored locally by init_task.py for attribution, but is deliberately not copied
    # wholesale into the upload bundle because pre-existing changes may contain secrets.
    (out / "BASELINE_DIFF_NOTICE.txt").write_text(
        "Raw baseline diff retained locally under .codex-audit/baseline/DIFF.patch and not uploaded. "
        "Use BASELINE_STATUS.txt plus current safe evidence; if precise pre-existing content is material, "
        "prepare a separately redacted baseline excerpt.\n", encoding="utf-8")

    copy_round_lineage(out, args.previous_audit, args.remediation_report, previous_text, args.remediation_master_prompt)

    # XHS reviews the publishable post, not source code.  Asset metadata is explicit
    # so image-review references have a stable key and private customer images stay out.
    xhs_assets = []
    if is_xhs:
        for requested in args.content_file or []:
            candidate = pathlib.Path(requested)
            source = (repo / candidate).resolve()
            if candidate.is_absolute() or ".." in candidate.parts or not source.is_file() or repo not in source.parents:
                raise ValueError(f"invalid XHS content file: {requested}")
            content = text_bytes(source)
            if content is None or len(content) > MAX_TEXT:
                raise ValueError(f"XHS content must be UTF-8 text within limit: {requested}")
            dst = out / "POST_CONTENT" / candidate
            dst.parent.mkdir(parents=True, exist_ok=True); dst.write_bytes(content)
        for raw in args.asset or []:
            try:
                asset_id, path_text, role, source_class, publication_allowed = raw.split(":", 4)
            except ValueError:
                raise ValueError("--asset requires asset_id:path:role:source_class:publication_allowed")
            candidate = pathlib.Path(path_text); source = (repo / candidate).resolve()
            if (not re.fullmatch(r"[A-Za-z0-9_-]+", asset_id) or candidate.is_absolute() or ".." in candidate.parts or not source.is_file() or repo not in source.parents):
                raise ValueError(f"invalid XHS asset: {raw}")
            if source_class not in XHS_ALLOWED_SOURCE_CLASSES or publication_allowed.upper() != "TRUE":
                raise ValueError(f"XHS asset is not upload-authorized: {asset_id}")
            dst = out / "POST_ASSETS" / asset_id / source.name
            dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, dst)
            xhs_assets.append({"asset_id": asset_id, "filename": source.name, "sequence": len(xhs_assets) + 1, "role": role, "content_type": source.suffix.lower(), "source_class": source_class, "publication_allowed": True, "bundle_path": str(dst.relative_to(out))})
        (out / "ASSET_MANIFEST.json").write_text(json.dumps(xhs_assets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Evidence expansion can add selected unchanged files without broadening the
    # raw bundle or allowing paths outside the repository.
    for requested in args.evidence_path or []:
        entry, content = snapshot_entry(repo, out, requested, "evidence_request")
        candidate = pathlib.Path(requested)
        if entry["status"] == "OMITTED_TOO_LARGE":
            split_manifest, parts = write_split_evidence(out, candidate, content)
            entry.update(status="SPLIT_COMPLETE", split_manifest=split_manifest, split_parts=parts)
            for changed_entry in manifest:
                if changed_entry.get("repository_path") == candidate.as_posix() and changed_entry.get("status") == "OMITTED_TOO_LARGE":
                    changed_entry.update(status="SPLIT_COMPLETE", split_manifest=split_manifest, split_parts=parts)
        manifest.append(entry)

    (out / "FILE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [e for e in manifest if e["status"] in {"READ_ERROR", "UNSUPPORTED"}]
    if failures:
        raise ValueError("Required file evidence failed: " + "; ".join(
            f"{e['status']}: {e['repository_path']} ({e['reason']})" for e in failures))

    # A census records repository visibility without uploading the repository source.
    write_census(repo, out / "REPOSITORY_CENSUS.jsonl")

    # Copy the response format into every bundle.
    skill_root = pathlib.Path(__file__).resolve().parents[1]
    shutil.copy2(skill_root / "templates" / ("xhs-audit-response-format.md" if is_xhs else "audit-response-format.md"), out / "AUDIT_RESPONSE_FORMAT.md")
    shutil.copy2(skill_root / "EVIDENCE_POLICY.md", out / "EVIDENCE_POLICY.md")
    if is_xhs:
        shutil.copy2(skill_root / "templates" / "xhs-user-panel-prompt.md", out / "XHS_USER_PANEL_PROMPT.md")
        template_name = "xhs-initial-audit.md" if round_no == 1 else "xhs-remediation-verification.md"
    else:
        template_name = "initial-audit.md" if round_no == 1 else "remediation-verification.md"
    req = (skill_root / "templates" / template_name).read_text(encoding="utf-8")
    warning = [e for e in manifest if e.get("status") not in {"COMPLETE", "SPLIT_COMPLETE", "DELETED"}]
    if warning:
        req += "\n\n## Evidence completeness warning\n\nSome changed-file snapshots were omitted. Review FILE_MANIFEST.json and return BLOCKED if the omitted evidence is material to correctness.\n"
    (out / "AUDIT_REQUEST.md").write_text(req, encoding="utf-8")
    (out / "FILE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_path = out / f"AUDIT_BUNDLE_{task_id}_R{round_no:02d}.zip"
    state = {
        "audit_protocol_version": 1,
        "task_id": task_id,
        "round": round_no,
        "audit_type": audit_type,
        "audit_profile": audit_profile,
        "state": "HANDOFF_READY",
        "strict_pass_only": task.get("strict_pass_only", True),
        "max_audit_rounds": task.get("max_audit_rounds", 3),
        "max_goal_iterations": task.get("max_goal_iterations", 4),
        "mode_family": "GOAL_LOOP" if is_xhs else "STRICT_AUDIT",
        "max_evidence_expansion_rounds": task.get("max_evidence_expansion_rounds", 2),
        "remediation_execution_mode": task.get("remediation_execution_mode", "GUIDED"),
        "remediation_phase": "HANDOFF_BUILDING",
        "findings_frozen": False,
        "master_prompt_required": False,
    }
    final_zip_path = final_out / f"AUDIT_BUNDLE_{task_id}_R{round_no:02d}.zip"
    state.update(new_transport_state(str(final_zip_path), task_id, round_no))
    (out / "STATE.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    # BUNDLE_MANIFEST is created before its own hash and before the final integrity
    # markers, avoiding a circular hash while keeping the evidence list deterministic.
    bundle_manifest = {
        "manifest_schema_version": 1,
        "files": bundle_entries(out) + [
            {
                "path": entry["repository_path"],
                "sha256": entry.get("sha256"),
                "size": entry.get("size"),
                "evidence_class": "changed",
                "included": False,
                "exclusion_reason": entry.get("reason", entry.get("status")),
                "source_repository_path": entry["repository_path"],
            }
            for entry in manifest if entry.get("status") != "COMPLETE"
        ],
    }
    (out / "BUNDLE_MANIFEST.json").write_text(json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_hash = sha256_file(out / "BUNDLE_MANIFEST.json")
    subject_id = audit_subject(task_hash, candidate_worktree_hash, context_version, context_hash, manifest_hash, round_no, audit_profile)
    subject = {
        "audit_protocol_version": 1,
        "audit_subject_id": subject_id,
        "task_id": task_id,
        "round": round_no,
        "audit_type": audit_type,
        "audit_profile": audit_profile,
        "task_spec_hash": task_hash,
        "worktree_fingerprint": candidate_worktree_hash,
        "project_context_version": context_version,
        "project_context_fingerprint": context_hash,
        "project_context_state": context_state,
        "bundle_manifest_hash": manifest_hash,
    }
    (out / "AUDIT_SUBJECT.json").write_text(json.dumps(subject, indent=2) + "\n", encoding="utf-8")
    write_bundle_hashes(out, manifest_hash)
    subject["file_count"] = len(bundle_manifest["files"])
    subject["state"] = "COMPLETE"
    subject["completion_content_hash"] = completion_integrity(subject)
    (out / "BUNDLE_COMPLETE.json").write_text(json.dumps(subject, indent=2) + "\n", encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in out.rglob("*"):
            if p == zip_path or not p.is_file() or p.name in audit_integrity.MUTABLE_CONTROL_FILES or audit_integrity.is_runtime_capture(p): continue
            z.write(p, p.relative_to(out))
    bundle_errors = audit_integrity.validate_bundle(out) + validate_bundle_archive(zip_path, out)
    if bundle_errors:
        raise ValueError("built handoff failed final validation:\n- " + "\n- ".join(bundle_errors))
    os.replace(out, final_out)
    print(final_zip_path)

if __name__ == "__main__":
    main()
