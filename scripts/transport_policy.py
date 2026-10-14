"""Fail-closed local policy for the Codex in-app browser transport."""
from __future__ import annotations

import json
import ast
import os
import pathlib
import re
import shlex
import zipfile

from audit_integrity import MUTABLE_CONTROL_FILES, is_runtime_capture, sha256_file

PRIMARY_BROWSER_TRANSPORT = "CODEX_IN_APP_BROWSER"
COMPUTER_USE = "COMPUTER_USE"
EXTERNAL_BROWSER = "EXTERNAL_BROWSER_FALLBACK"
STOP = "STOP"

TRANSPORT_STATES = (
    "HANDOFF_READY",
    "OPENING_IN_APP_BROWSER",
    "CHATGPT_READY",
    "OPENING_ATTACHMENT_CONTROL",
    "SELECTING_HANDOFF_FILE",
    "FILE_UPLOAD_IN_PROGRESS",
    "FILE_ATTACHED",
    "SUBMITTING_REQUEST",
    "WAITING_FOR_CHATGPT",
    "WAITING_FOR_AUDIT_RESPONSE",
    "WAITING_FOR_AUDIT_ARTIFACT",
    "RESPONSE_AND_ARTIFACT_READY",
    "DOWNLOADING_ARTIFACT",
    "TRANSPORT_FAILED",
)

_NEXT = {
    "HANDOFF_READY": {"OPENING_IN_APP_BROWSER", "TRANSPORT_FAILED"},
    "OPENING_IN_APP_BROWSER": {"CHATGPT_READY", "TRANSPORT_FAILED"},
    "CHATGPT_READY": {"OPENING_ATTACHMENT_CONTROL", "TRANSPORT_FAILED"},
    "OPENING_ATTACHMENT_CONTROL": {"SELECTING_HANDOFF_FILE", "TRANSPORT_FAILED"},
    "SELECTING_HANDOFF_FILE": {"FILE_UPLOAD_IN_PROGRESS", "TRANSPORT_FAILED"},
    "FILE_UPLOAD_IN_PROGRESS": {"FILE_ATTACHED", "TRANSPORT_FAILED"},
    "FILE_ATTACHED": {"SUBMITTING_REQUEST", "TRANSPORT_FAILED"},
    "SUBMITTING_REQUEST": {"WAITING_FOR_CHATGPT", "TRANSPORT_FAILED"},
    "WAITING_FOR_CHATGPT": {"WAITING_FOR_AUDIT_RESPONSE", "TRANSPORT_FAILED"},
    "WAITING_FOR_AUDIT_RESPONSE": {"WAITING_FOR_AUDIT_ARTIFACT", "TRANSPORT_FAILED"},
    "WAITING_FOR_AUDIT_ARTIFACT": {"RESPONSE_AND_ARTIFACT_READY", "TRANSPORT_FAILED"},
    "RESPONSE_AND_ARTIFACT_READY": {"DOWNLOADING_ARTIFACT", "TRANSPORT_FAILED"},
}


def choose_transport(
    in_app_browser_available: bool,
    external_browser_user_approved: bool = False,
) -> dict[str, object]:
    if in_app_browser_available:
        return {
            "transport": PRIMARY_BROWSER_TRANSPORT,
            "ui_automation": COMPUTER_USE,
            "external_browser_used": False,
            "user_approved": False,
            "reason": "in-app browser available",
        }
    if external_browser_user_approved:
        return {
            "transport": EXTERNAL_BROWSER,
            "ui_automation": COMPUTER_USE,
            "external_browser_used": True,
            "user_approved": True,
            "reason": "explicit user-approved fallback",
        }
    return {
        "transport": STOP,
        "ui_automation": COMPUTER_USE,
        "external_browser_used": False,
        "user_approved": False,
        "reason": "IN_APP_BROWSER_UNAVAILABLE",
    }


def new_transport_state(bundle_path: str, handoff_id: str, round_no: int) -> dict[str, object]:
    bundle = pathlib.Path(bundle_path).resolve()
    return {
        "transport_state": "HANDOFF_READY",
        "browser_transport": PRIMARY_BROWSER_TRANSPORT,
        "external_browser_used": False,
        "user_approved": False,
        "transport_fallback_reason": None,
        "upload_method": "IN_APP_ATTACHMENT",
        "native_file_dialog_used": False,
        "bundle_path": str(bundle),
        "expected_bundle_filename": bundle.name,
        "handoff_id": handoff_id,
        "round": round_no,
        "uploaded_filename": None,
        "upload_verified": False,
        "downloaded_path": None,
        "download_verified": False,
        "conversation_policy": "REUSE_SAME_IN_APP_SESSION_WHEN_PROFILE_ALLOWS",
    }


def apply_transport_decision(state: dict[str, object], decision: dict[str, object]) -> dict[str, object]:
    updated = dict(state)
    updated.update({
        "browser_transport": decision["transport"],
        "external_browser_used": decision["external_browser_used"],
        "user_approved": decision["user_approved"],
        "transport_fallback_reason": decision["reason"] if decision["external_browser_used"] else None,
    })
    return updated


def validate_bundle_path(
    bundle_path: str,
    expected_bundle_path: str,
    *,
    expected_filename: str,
    bundle_complete: bool,
    hashes_valid: bool,
) -> list[str]:
    selected = pathlib.Path(bundle_path).resolve()
    expected = pathlib.Path(expected_bundle_path).resolve()
    errors: list[str] = []
    if selected != expected:
        errors.append(f"bundle path mismatch: selected={selected} expected={expected}")
    if selected.name != expected_filename:
        errors.append(f"bundle filename mismatch: {selected.name} != {expected_filename}")
    if not selected.is_file():
        errors.append(f"bundle does not exist: {selected}")
    elif selected.stat().st_size <= 0:
        errors.append(f"bundle is empty: {selected}")
    if not bundle_complete:
        errors.append("bundle is not marked complete")
    if not hashes_valid:
        errors.append("bundle hash validation failed")
    return errors


def _literal_argv(node: ast.AST) -> list[str] | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _external_launch_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    def executable_name(item: str) -> str:
        name = pathlib.PureWindowsPath(item.replace("\\", "/")).name.lower()
        return name[:-4] if name.endswith(".exe") else name

    names = [item.lower() if item.lower() in {"/c", "/k"} else executable_name(item) for item in argv]
    if names[0] == "env":
        remainder = [item for item in argv[1:] if "=" not in item or item.startswith("<")]
        return _external_launch_argv(remainder)
    if names[0] in {"open", "xdg-open", "explorer"}:
        return True
    if names[0] == "gio" and len(names) >= 2 and names[1] == "open":
        return True
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", names[0]) or names[0] == "<dynamic>":
        if len(names) >= 3 and names[1] == "-m" and names[2] == "webbrowser":
            return True
    if names[0] in {"python", "python3", "<dynamic>"} and len(names) >= 3 and names[1] == "-m" and names[2] == "webbrowser":
        return True
    if names[0] in {"powershell", "pwsh"}:
        if any(item.lower() == "start-process" for item in argv):
            return True
        for index, item in enumerate(names[1:], start=1):
            if item == "-command" and index + 1 < len(argv):
                try:
                    if any(token.lower() == "start-process" for token in shlex.split(argv[index + 1])):
                        return True
                except ValueError:
                    pass
    if names[0] in {"sh", "bash", "zsh", "dash", "fish", "pwsh", "powershell"}:
        for index, item in enumerate(names[1:], start=1):
            if (item in {"-c", "-command"} or (item.startswith("-") and "c" in item[1:].lower())) and index + 1 < len(argv):
                if argv[index + 1] == "<dynamic>":
                    return True
                try:
                    return _external_launch_argv(shlex.split(argv[index + 1]))
                except ValueError:
                    return False
    if names[0] == "cmd" and len(names) >= 3 and names[1] in {"/c", "/k"} and names[2] == "start":
        return True
    return False


def forbidden_launches(path: pathlib.Path) -> list[str]:
    """Find statically recognizable standalone browser/Finder launches."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return ["unreadable-or-invalid-python"]
    modules: dict[str, str] = {}
    functions: dict[str, str] = {}
    known: dict[str, str | list[str] | bool | None] = {}
    WEBBROWSER_CONTROLLER = "<webbrowser-controller>"
    WEBBROWSER_CALLABLE = "<webbrowser-callable>"
    SUBPROCESS_CALLABLE = "<subprocess-callable>"
    OS_SYSTEM_CALLABLE = "<os-system-callable>"
    WEBBROWSER_GET_CALLABLE = "<webbrowser-get-callable>"
    branch_names = set()
    for control in ast.walk(tree):
        if isinstance(control, (ast.If, ast.For, ast.While, ast.Try)):
            for nested in ast.walk(control):
                if isinstance(nested, (ast.Assign, ast.AnnAssign)):
                    targets = nested.targets if isinstance(nested, ast.Assign) else (nested.target,)
                    branch_names.update(target.id for target in targets if isinstance(target, ast.Name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"subprocess", "webbrowser", "os"}:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "webbrowser", "os"}:
            for alias in node.names:
                    functions[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    def argv_value(node: ast.AST) -> list[str] | None:
        if isinstance(node, ast.Name):
            if node.id in branch_names:
                return ["<branch-dynamic>"]
            if node.id in known and known[node.id] is None:
                return ["<dynamic>"]
            value = known.get(node.id)
            if isinstance(value, str) and not value.startswith("<"):
                try:
                    return shlex.split(value)
                except ValueError:
                    return None
            return value
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return shlex.split(node.value)
            except ValueError:
                return None
        if isinstance(node, (ast.List, ast.Tuple)):
            result = []
            for item in node.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    result.append(item.value)
                elif isinstance(item, ast.Name) and isinstance(known.get(item.id), str):
                    result.append(known[item.id])
                else:
                    result.append("<dynamic>")
            return result
        return None

    def bool_value(node: ast.AST) -> bool | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name) and isinstance(known.get(node.id), bool):
            return known[node.id]
        return None

    def call_payload(node: ast.Call) -> ast.AST | None:
        if node.args:
            return node.args[0]
        for keyword in node.keywords:
            if keyword.arg in {"args", "url", "command"}:
                return keyword.value
        return None
    hits: list[str] = []
    for node in sorted(ast.walk(tree), key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0))):
        if isinstance(node, ast.Assign):
            value = None
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and isinstance(node.value.func.value, ast.Name):
                module = modules.get(node.value.func.value.id)
                if module == "webbrowser" and node.value.func.attr == "get":
                    value = WEBBROWSER_CONTROLLER
            elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                imported = functions.get(node.value.func.id)
                if imported == "webbrowser.get" or known.get(node.value.func.id) == WEBBROWSER_GET_CALLABLE:
                    value = WEBBROWSER_CONTROLLER
            if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name):
                module = modules.get(node.value.value.id)
                if module == "webbrowser" and node.value.attr in {"open", "open_new", "open_new_tab"}:
                    value = WEBBROWSER_CALLABLE
                elif known.get(node.value.value.id) == WEBBROWSER_CONTROLLER and node.value.attr in {"open", "open_new", "open_new_tab"}:
                    value = WEBBROWSER_CALLABLE
                elif module == "webbrowser" and node.value.attr == "get":
                    value = WEBBROWSER_GET_CALLABLE
                elif module == "subprocess" and node.value.attr in {"run", "Popen", "call", "check_call", "check_output"}:
                    value = SUBPROCESS_CALLABLE
                elif module == "os" and node.value.attr in {"system", "popen"}:
                    value = OS_SYSTEM_CALLABLE
            if value is None and isinstance(node.value, ast.Name):
                imported = functions.get(node.value.id)
                value = {
                    "webbrowser.get": WEBBROWSER_GET_CALLABLE,
                    "os.system": OS_SYSTEM_CALLABLE,
                    "os.popen": OS_SYSTEM_CALLABLE,
                }.get(imported, known.get(node.value.id))
                if imported and imported.startswith("webbrowser.") and imported != "webbrowser.get":
                    value = WEBBROWSER_CALLABLE
                elif imported and imported.startswith("subprocess."):
                    value = SUBPROCESS_CALLABLE
            if value is None and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (str, bool)):
                value = node.value.value
            elif value is None:
                value = argv_value(node.value)
            if value is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        known[target.id] = value
            else:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        known[target.id] = None
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = node.value.value if node.value and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (str, bool)) else (argv_value(node.value) if node.value else None)
            known[node.target.id] = value
            continue
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        kind = None
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            module = modules.get(target.value.id)
            if module == "subprocess" and target.attr in {"run", "Popen", "call", "check_call", "check_output"}:
                kind = "subprocess"
            elif module == "os" and target.attr in {"system", "popen"}:
                kind = "os.system"
            elif module == "webbrowser" and target.attr in {"open", "open_new", "open_new_tab"}:
                kind = "webbrowser"
        elif isinstance(target, ast.Name):
            imported = functions.get(target.id)
            if imported in {"os.system", "os.popen"}:
                kind = "os.system"
            elif imported in {"webbrowser.open", "webbrowser.open_new", "webbrowser.open_new_tab"} or (imported and imported.startswith("subprocess.")):
                kind = "webbrowser" if imported == "webbrowser.open" else "subprocess"
                if imported.startswith("webbrowser."):
                    kind = "webbrowser"
            elif known.get(target.id) == WEBBROWSER_CALLABLE:
                kind = "webbrowser"
            elif known.get(target.id) == SUBPROCESS_CALLABLE:
                kind = "subprocess"
            elif known.get(target.id) == OS_SYSTEM_CALLABLE:
                kind = "os.system"
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Call):
            get_call = target.value
            if isinstance(get_call.func, ast.Attribute) and isinstance(get_call.func.value, ast.Name):
                module_get = modules.get(get_call.func.value.id) == "webbrowser" and get_call.func.attr == "get"
            else:
                module_get = isinstance(get_call.func, ast.Name) and functions.get(get_call.func.id) == "webbrowser.get"
            if module_get and target.attr in {"open", "open_new", "open_new_tab"}:
                    kind = "webbrowser"
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and known.get(target.value.id) == WEBBROWSER_CONTROLLER and target.attr in {"open", "open_new", "open_new_tab"}:
            kind = "webbrowser"
        payload = call_payload(node)
        if payload is None:
            continue
        argv = argv_value(payload) or _literal_argv(payload)
        shell_true = any(
            keyword.arg == "shell"
            and bool_value(keyword.value) is True
            for keyword in node.keywords
        )
        if kind == "webbrowser":
            hits.append(f"{path}:{getattr(node, 'lineno', 0)}")
        elif kind == "subprocess" and shell_true and (argv is None or "<dynamic>" in argv or "<branch-dynamic>" in argv or _external_launch_argv(argv)):
            hits.append(f"{path}:{getattr(node, 'lineno', 0)}")
        elif kind == "subprocess" and argv and ("<branch-dynamic>" in argv or _external_launch_argv(argv) or (
            pathlib.PureWindowsPath(argv[0].replace("\\", "/")).name.lower() == "osascript"
        )):
            hits.append(f"{path}:{getattr(node, 'lineno', 0)}")
        elif kind == "os.system" and argv is None:
            hits.append(f"{path}:{getattr(node, 'lineno', 0)}")
        elif kind == "os.system" and argv:
            command = " ".join(argv)
            if _external_launch_argv(argv) or re.search(r"(?:^|\s)(?:open|xdg-open|start|activate|reveal)(?:\s|$)|osascript[\s\S]*(?:Finder|Safari|Chrome)|Start-Process", command, re.I) or "<dynamic>" in argv:
                hits.append(f"{path}:{getattr(node, 'lineno', 0)}")
    return hits


def attachment_ready(attachments: list[object], expected_filename: str, uploading: bool = False) -> tuple[bool, str]:
    names = [item.get("filename", "") if isinstance(item, dict) else str(item) for item in attachments]
    if uploading:
        return False, "ATTACHMENT_STILL_UPLOADING"
    if len(names) != 1 or names[0] != expected_filename:
        return False, "EXPECTED_BUNDLE_NOT_EXACTLY_ATTACHED"
    return True, "ATTACHMENT_READY"


def send_allowed(state: str, prompt_ready: bool, attachments: list[object], expected_filename: str, uploading: bool = False) -> bool:
    ready, _ = attachment_ready(attachments, expected_filename, uploading)
    return state == "FILE_ATTACHED" and prompt_ready and ready


def response_and_artifact_ready(final_response_ready: bool, downloadable_artifact_ready: bool) -> bool:
    return bool(final_response_ready and downloadable_artifact_ready)


def validate_download(
    path: str,
    expected_filename: str | None = None,
    *,
    expected_handoff_id: str | None = None,
    expected_round: int | None = None,
    expected_audit_type: str | None = None,
    expected_subject_id: str | None = None,
) -> list[str]:
    candidate = pathlib.Path(path)
    errors: list[str] = []
    if expected_handoff_id is None or expected_round is None:
        errors.append("independent download identity context is required")
    if expected_audit_type is None or expected_subject_id is None:
        errors.append("complete download identity context is required")
    canonical_name = (
        f"AUDIT_RESPONSE_{expected_handoff_id}_R{expected_round:02d}.md"
        if expected_handoff_id is not None and expected_round is not None else None
    )
    if expected_filename is not None and canonical_name is not None and expected_filename != canonical_name:
        errors.append("expected download filename is not canonical")
    expected_name = canonical_name or expected_filename
    if expected_name is not None and candidate.name != expected_name:
        errors.append(f"download filename mismatch: {candidate.name} != {expected_name}")
    if not candidate.is_file():
        errors.append(f"download does not exist: {candidate}")
    elif candidate.stat().st_size <= 0:
        errors.append(f"download is empty: {candidate}")
    elif expected_handoff_id is not None or expected_round is not None or expected_audit_type is not None or expected_subject_id is not None:
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"download is not readable UTF-8 text: {exc}")
        else:
            if expected_handoff_id is not None:
                matches = re.findall(r"^HANDOFF_ID:\s*(\S+)\s*$", text, re.MULTILINE)
                if matches != [expected_handoff_id]:
                    errors.append("download handoff identity mismatch")
            if expected_round is not None:
                matches = re.findall(r"^ROUND:\s*(\d+)\s*$", text, re.MULTILINE)
                if matches != [str(expected_round)]:
                    errors.append("download round identity mismatch")
            for key, expected in (("AUDIT_TYPE", expected_audit_type), ("AUDIT_SUBJECT_ID", expected_subject_id)):
                matches = re.findall(rf"^{key}:\s*(\S+)\s*$", text, re.MULTILINE)
                if expected is None or matches != [expected]:
                    errors.append(f"download {key.lower()} identity mismatch")
    return errors


def record_verified_download(
    state_path: str,
    download_path: str,
    *,
    expected_handoff_id: str,
    expected_round: int,
    expected_audit_type: str,
    expected_subject_id: str,
) -> list[str]:
    errors = validate_download(
        download_path,
        expected_handoff_id=expected_handoff_id,
        expected_round=expected_round,
        expected_audit_type=expected_audit_type,
        expected_subject_id=expected_subject_id,
    )
    if errors:
        return errors
    state_file = pathlib.Path(state_path)
    try:
        digest = sha256_file(pathlib.Path(download_path))
    except OSError as exc:
        return [f"cannot record verified download: {exc}"]
    provenance = {
        "state_path": str(state_file.resolve()),
        "downloaded_path": str(pathlib.Path(download_path).resolve()),
        "download_sha256": digest,
        "download_verified": True,
        "download_identity": {
            "handoff_id": expected_handoff_id,
            "round": expected_round,
            "audit_type": expected_audit_type,
            "subject_id": expected_subject_id,
        },
    }
    state_file.with_name("DOWNLOAD_PROVENANCE.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state.update({
            "transport_state": "RESPONSE_AND_ARTIFACT_READY",
            "downloaded_path": str(pathlib.Path(download_path).resolve()),
            "download_verified": True,
        })
        temporary = state_file.with_name(f".{state_file.name}.tmp-{os.getpid()}")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, state_file)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return [f"cannot persist verified download state: {exc}"]
    return []


def advance_state(
    current: str,
    next_state: str,
    *,
    final_response_ready: bool = False,
    downloadable_artifact_ready: bool = False,
) -> str:
    if next_state not in _NEXT.get(current, set()):
        raise ValueError(f"invalid transport transition: {current} -> {next_state}")
    if next_state == "RESPONSE_AND_ARTIFACT_READY" and not response_and_artifact_ready(
        final_response_ready, downloadable_artifact_ready
    ):
        raise ValueError("response and downloadable artifact must both be ready")
    return next_state


def validate_transport_state(
    state_path: str,
    *,
    expected_handoff_id: str | None = None,
    expected_round: int | None = None,
    expected_bundle_path: str | None = None,
    expected_bundle_filename: str | None = None,
) -> list[str]:
    """Validate local handoff state and the exact ZIP before browser use."""
    state_file = pathlib.Path(state_path).resolve()
    errors: list[str] = []
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid transport state: {exc}"]
    required = {"browser_transport", "upload_method", "bundle_path", "expected_bundle_filename", "handoff_id", "round"}
    errors.extend(f"transport state missing {key}" for key in sorted(required - state.keys()))
    if any(value is None for value in (expected_handoff_id, expected_round, expected_bundle_path, expected_bundle_filename)):
        errors.append("independent expected transport context is required")
    transport = state.get("browser_transport")
    if transport not in {PRIMARY_BROWSER_TRANSPORT, EXTERNAL_BROWSER}:
        errors.append("transport state has an unsupported browser transport")
    fallback_reason = state.get("transport_fallback_reason")
    if transport == EXTERNAL_BROWSER:
        if not (state.get("external_browser_used") and state.get("user_approved")):
            errors.append("external browser transport requires explicit user approval")
        if not isinstance(fallback_reason, str) or not fallback_reason.strip():
            errors.append("external browser transport requires a non-empty fallback reason")
    elif state.get("external_browser_used") or state.get("user_approved") or fallback_reason not in (None, ""):
        errors.append("primary browser transport has inconsistent fallback fields")
    bundle_path = state.get("bundle_path")
    if not isinstance(bundle_path, str):
        return errors + ["transport state bundle_path is not a string"]
    handoff_id = state.get("handoff_id")
    try:
        round_no = int(state.get("round", 0))
    except (TypeError, ValueError):
        return errors + ["transport state round is not an integer"]
    if not isinstance(handoff_id, str) or not handoff_id or round_no < 1:
        return errors + ["transport state has invalid handoff_id or round"]
    expected_name = state.get("expected_bundle_filename")
    if not isinstance(expected_name, str) or not expected_name:
        return errors + ["transport state expected_bundle_filename is invalid"]
    if expected_handoff_id is not None and handoff_id != expected_handoff_id:
        errors.append(f"handoff identity mismatch: {handoff_id} != {expected_handoff_id}")
    if expected_round is not None and round_no != expected_round:
        errors.append(f"round mismatch: {round_no} != {expected_round}")
    if expected_bundle_filename is not None and expected_name != expected_bundle_filename:
        errors.append(f"expected bundle filename mismatch: {expected_name} != {expected_bundle_filename}")
    complete_path = state_file.parent / "BUNDLE_COMPLETE.json"
    complete = {}
    try:
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid BUNDLE_COMPLETE.json: {exc}")
    if expected_bundle_path is not None:
        errors.extend(validate_bundle_path(
            bundle_path,
            expected_bundle_path,
            expected_filename=expected_bundle_filename or expected_name,
            bundle_complete=complete.get("state") == "COMPLETE",
            hashes_valid=True,
        ))
    if complete:
        if complete.get("task_id") != expected_handoff_id:
            errors.append("BUNDLE_COMPLETE task identity mismatch")
        if complete.get("round") != expected_round:
            errors.append("BUNDLE_COMPLETE round mismatch")
    subject_path = state_file.parent / "AUDIT_SUBJECT.json"
    try:
        subject = json.loads(subject_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid AUDIT_SUBJECT.json: {exc}")
    else:
        if subject.get("task_id") != expected_handoff_id:
            errors.append("AUDIT_SUBJECT task identity mismatch")
        if subject.get("round") != expected_round:
            errors.append("AUDIT_SUBJECT round mismatch")
    if not state_file.parent.is_dir():
        return errors + [f"transport state directory does not exist: {state_file.parent}"]
    try:
        from audit_integrity import validate_deliverable_bundle
        errors.extend(validate_deliverable_bundle(state_file.parent))
    except ImportError as exc:
        errors.append(f"bundle validator unavailable: {exc}")
    archive = pathlib.Path(bundle_path)
    if archive.parent != state_file.parent:
        errors.append("bundle must be in the current handoff round directory")
    if archive.is_file():
        try:
            with zipfile.ZipFile(archive) as zf:
                if zf.testzip() is not None:
                    errors.append("bundle ZIP integrity check failed")
                archive_names = zf.namelist()
                if len(archive_names) != len(set(archive_names)):
                    errors.append("bundle ZIP contains duplicate members")
                expected_members = {
                    p.relative_to(state_file.parent).as_posix()
                    for p in state_file.parent.rglob("*")
                    if p.is_file() and p != archive and p.name not in MUTABLE_CONTROL_FILES and not is_runtime_capture(p)
                }
                if set(archive_names) != expected_members:
                    errors.append("bundle ZIP members do not match frozen handoff files")
        except (OSError, zipfile.BadZipFile) as exc:
            errors.append(f"invalid bundle ZIP: {exc}")
    provenance_path = state_file.with_name("DOWNLOAD_PROVENANCE.json")
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid download provenance: {exc}")
            provenance = {}
        downloaded = provenance.get("downloaded_path")
        if not isinstance(downloaded, str) or not pathlib.Path(downloaded).is_file():
            errors.append("verified download artifact is missing")
        elif provenance.get("download_sha256") != sha256_file(pathlib.Path(downloaded)):
            errors.append("verified download artifact hash mismatch")
        identity = provenance.get("download_identity")
        if not isinstance(identity, dict) or identity.get("handoff_id") != handoff_id or identity.get("round") != round_no:
            errors.append("verified download identity is not bound to this round")
    return errors


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("state")
    parser.add_argument("--expected-handoff-id", required=True)
    parser.add_argument("--expected-round", required=True, type=int)
    parser.add_argument("--expected-bundle-path", required=True)
    parser.add_argument("--expected-bundle-filename", required=True)
    parser.add_argument("--download-path")
    parser.add_argument("--expected-download-audit-type")
    parser.add_argument("--expected-download-subject-id")
    parser.add_argument("--record-download", action="store_true")
    args = parser.parse_args()
    failures = validate_transport_state(
        args.state,
        expected_handoff_id=args.expected_handoff_id,
        expected_round=args.expected_round,
        expected_bundle_path=args.expected_bundle_path,
        expected_bundle_filename=args.expected_bundle_filename,
    )
    if args.download_path:
        if not args.expected_download_audit_type or not args.expected_download_subject_id:
            print("TRANSPORT_BLOCKED")
            print("- complete download identity context is required")
            sys.exit(2)
        download_name = f"AUDIT_RESPONSE_{args.expected_handoff_id}_R{args.expected_round:02d}.md"
        failures.extend(validate_download(
            args.download_path,
            download_name,
            expected_handoff_id=args.expected_handoff_id,
            expected_round=args.expected_round,
            expected_audit_type=args.expected_download_audit_type,
            expected_subject_id=args.expected_download_subject_id,
        ))
        if args.record_download and not failures:
            failures.extend(record_verified_download(
                args.state, args.download_path,
                expected_handoff_id=args.expected_handoff_id,
                expected_round=args.expected_round,
                expected_audit_type=args.expected_download_audit_type,
                expected_subject_id=args.expected_download_subject_id,
            ))
    if failures:
        print("TRANSPORT_BLOCKED")
        for failure in failures:
            print(f"- {failure}")
        sys.exit(2)
    print("TRANSPORT_READY")
