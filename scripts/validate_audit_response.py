#!/usr/bin/env python3
"""Syntactic and conservative semantic validation for audit responses."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import sys
from dataclasses import dataclass

FIELDS = {
    "AUDIT_PROTOCOL_VERSION": r"AUDIT_PROTOCOL_VERSION:\s*(\d+)",
    "HANDOFF_ID": r"HANDOFF_ID:\s*(\S+)",
    "ROUND": r"ROUND:\s*(\d+)",
    "AUDIT_TYPE": r"AUDIT_TYPE:\s*(INITIAL|REMEDIATION_VERIFICATION|XHS_USER_PANEL_REVIEW|XHS_USER_PANEL_REMEDIATION_VERIFICATION)",
    "AUDIT_SUBJECT_ID": r"AUDIT_SUBJECT_ID:\s*(\S+)",
    "VERDICT": r"VERDICT:\s*(PASS|PASS_WITH_NOTES|FAIL|BLOCKED)",
}
HEADER_ORDER = tuple(FIELDS)
CLOSURE_STATES = {"CLOSED", "OPEN", "PARTIALLY_CLOSED", "DISPUTED_BUT_UNRESOLVED"}
COMMAND_STATUSES = {"READY", "NO_SAFE_COMMAND", "USER_DECISION_REQUIRED", "DESIGN_DECISION_REQUIRED"}
COMMAND_CLASSES = {"SAFE_READ", "SAFE_TEST", "SAFE_BUILD", "MODIFYING"}
EXECUTION_POLICIES = {"AUTO", "VERIFY_THEN_RUN"}
XHS_CANONICAL_SECTIONS = (
    "INFERRED_CONTENT_INTENT", "CURRENT_VERSION_BLIND_REVIEW", "SELF_SELECTED_PERSONAS",
    "RAW_USER_REACTIONS", "NATURAL_USER_VOICE_PANEL", "AUDIENCE_CLARITY", "WHAT_WORKS",
    "WHAT_DOES_NOT_WORK", "GOOD_POINTS", "BAD_POINTS", "BIGGEST_STRENGTH", "BIGGEST_FAILURE",
    "AFTER_READING_TAKEAWAY", "IMAGE_REVIEW", "LIKELY_USER_ACTIONS", "OVERALL_EVALUATION",
    "PREVIOUS_FINDING_CLOSURE_CHECK", "CODEX_REMEDIATION_PROMPT", "Final Gate Decision",
)
PLACEHOLDER_RE = re.compile(r"<[^>\n]+>|\b(?:TODO|TBD)\b|\.\.\.|/path/to|your/path", re.IGNORECASE)
DESTRUCTIVE_EXECUTABLES = {
    "rm", "rmdir", "unlink", "shred", "del", "erase", "remove-item",
    "mv", "cp", "install", "touch", "truncate", "tee", "chmod", "chown",
}
SHELL_EXECUTABLES = {"sh", "bash", "zsh", "fish", "dash", "ksh", "pwsh", "powershell"}
INTERPRETER_EXECUTABLES = {"python", "python3", "node", "ruby", "perl", "php", "lua"}
FORWARDING_WRAPPERS = {"command", "timeout", "nice", "stdbuf", "setsid", "time", "exec", "nohup", "busybox"}
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn"}
SAFE_READ_EXECUTABLES = {
    "cat", "cut", "echo", "head", "ls", "pwd", "printf", "tail", "tr", "true", "false", "wc",
}
SAFE_TEST_MODULES = {"unittest", "pytest", "json.tool", "py_compile"}
SAFE_GIT_COMMANDS = {"diff", "log", "ls-files", "rev-parse", "show", "status"}
GH_READ_COMMANDS = {
    ("issue", "list"), ("issue", "status"), ("issue", "view"),
    ("pr", "checks"), ("pr", "diff"), ("pr", "list"), ("pr", "status"), ("pr", "view"),
    ("repo", "list"), ("repo", "view"),
    ("release", "list"), ("release", "view"),
    ("run", "list"), ("run", "view"),
}
REMOTE_MUTATION_PAIRS = {
    ("issue", "create"), ("issue", "delete"), ("issue", "close"), ("issue", "reopen"), ("issue", "edit"),
    ("issue", "comment"), ("pr", "create"), ("pr", "merge"), ("pr", "close"), ("pr", "reopen"),
    ("pr", "edit"), ("pr", "comment"), ("pr", "review"), ("repo", "create"), ("repo", "delete"),
    ("repo", "edit"), ("repo", "fork"), ("release", "create"), ("release", "delete"),
    ("release", "edit"), ("release", "upload"), ("workflow", "run"), ("workflow", "enable"),
    ("workflow", "disable"), ("gist", "create"), ("gist", "delete"), ("gist", "edit"),
    ("secret", "set"), ("secret", "delete"), ("variable", "set"), ("variable", "delete"),
}


@dataclass(frozen=True)
class CommandCapability:
    """A conservative semantic decision for a command in a READY pack."""

    auto_execute: bool
    capability: str
    reason: str


def _safe(capability: str = "SAFE_LOCAL_VERIFICATION") -> CommandCapability:
    return CommandCapability(True, capability, "command belongs to the explicitly allowed local verification subset")


def _unknown(reason: str) -> CommandCapability:
    return CommandCapability(False, "UNKNOWN/UNPROVEN", reason)


def _unsafe(reason: str) -> CommandCapability:
    return CommandCapability(False, "UNSAFE", reason)


def _name(token: str) -> str:
    return pathlib.PurePosixPath(token).name.lower()


def exact_header(text: str) -> tuple[dict[str, str], list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors: list[str] = []
    values: dict[str, str] = {}
    if len(lines) < len(HEADER_ORDER):
        errors.append("response does not contain the complete protocol header")
        return values, errors
    for key, line in zip(HEADER_ORDER, lines[: len(HEADER_ORDER)]):
        match = re.fullmatch(FIELDS[key], line)
        if not match:
            errors.append(f"header field {key} is missing or out of order")
        else:
            values[key] = match.group(1)
    for key, pattern in FIELDS.items():
        count = len(re.findall(rf"^\s*{pattern}\s*$", text, re.MULTILINE))
        if count > 1 and key != "VERDICT":
            errors.append(f"duplicate critical field: {key}")
    return values, errors


def all_verdicts(text: str) -> list[str]:
    return re.findall(r"^\s*VERDICT:\s*(PASS|PASS_WITH_NOTES|FAIL|BLOCKED)\s*$", text, re.MULTILINE)


def section(text: str, title: str, next_title: str | None = None) -> str:
    match = re.search(rf"^##[ \t]+{re.escape(title)}[ \t]*$", text, re.MULTILINE)
    if not match:
        return ""
    body = text[match.end():]
    if next_title:
        next_match = re.search(r"^##[ \t]+.+?[ \t]*$", body, re.MULTILINE)
        if next_match:
            body = body[:next_match.start()]
    return body


def named_blocks(text: str, prefix: str, heading_level: int = 3) -> list[tuple[str, str]]:
    hashes = "#" * heading_level
    matches = list(re.finditer(rf"^{hashes}[ \t]+({prefix}-\d{{3,}})(?:[ \t]+.*)?$", text, re.MULTILINE))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append((match.group(1), text[match.end():end]))
    return result


def scalar(block: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}:[ \t]*(.+?)[ \t]*$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def actionable_finding_ids(text: str) -> list[str]:
    findings = section(text, "Findings", "Evidence Requests")
    return [finding_id for finding_id, block in named_blocks(findings, "PTA")
            if scalar(block, "Status") == "OPEN"]


def finding_namespace_errors(text: str, is_xhs: bool) -> list[str]:
    title = "WHAT_DOES_NOT_WORK" if is_xhs else "Findings"
    boundary = "GOOD_POINTS" if is_xhs else "Evidence Requests"
    findings = section(text, title, boundary)
    ids = re.findall(r"^###\s+([A-Z]+-\d{3,})(?:\s+.*)?$", findings, re.MULTILINE)
    expected = "XHS" if is_xhs else "PTA"
    return [f"foreign finding namespace: {finding_id} (expected {expected}-###)"
            for finding_id in ids if not finding_id.startswith(expected + "-")]


def xhs_finding_ids(text: str) -> list[str]:
    findings = section(text, "WHAT_DOES_NOT_WORK", "GOOD_POINTS")
    raw = re.findall(r"^FINDING_ID:\s*(\S+)\s*$", findings, re.MULTILINE)
    return sorted(set(item for item in raw if re.fullmatch(r"XHS-\d{3,}", item)))


def xhs_finding_id_errors(text: str) -> list[str]:
    findings = section(text, "WHAT_DOES_NOT_WORK", "GOOD_POINTS")
    raw = re.findall(r"^FINDING_ID:\s*(\S+)\s*$", findings, re.MULTILINE)
    errors: list[str] = []
    errors.extend(f"invalid XHS finding ID: {item}" for item in raw if not re.fullmatch(r"XHS-\d{3,}", item))
    errors.extend(f"duplicate XHS finding ID: {item}" for item in sorted(set(raw)) if raw.count(item) > 1)
    return errors


def xhs_closure_ids(text: str) -> list[str]:
    closure = section(text, "PREVIOUS_FINDING_CLOSURE_CHECK")
    return sorted(set(re.findall(r"^\s*(XHS-\d{3,})\s*[:|]\s*(?:CLOSED|OPEN|PARTIALLY_CLOSED|DISPUTED_BUT_UNRESOLVED)\s*$", closure, re.MULTILINE)))


def xhs_prior_closure_states(text: str, finding_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    closure = section(text, "PREVIOUS_FINDING_CLOSURE_CHECK")
    errors: list[str] = []
    closure_ids = re.findall(r"^\s*\|?\s*((?:PTA|XHS)-\d{3,})\s*(?:\||:)", closure, re.MULTILINE)
    states: dict[str, str] = {}
    errors.extend(
        f"XHS prior finding closure contains foreign finding: {finding_id}"
        for finding_id in sorted(set(closure_ids))
        if not finding_id.startswith("XHS-")
    )
    for finding_id in finding_ids:
        matches = re.findall(
            rf"^\s*{re.escape(finding_id)}\s*[:|]\s*(CLOSED|OPEN|PARTIALLY_CLOSED|DISPUTED_BUT_UNRESOLVED)\s*$",
            closure,
            re.MULTILINE,
        )
        if len(matches) != 1:
            errors.append(f"XHS prior finding closure state is missing or ambiguous: {finding_id}")
        else:
            states[finding_id] = matches[0]
    listed = set(re.findall(
        r"^\s*(XHS-\d{3,})\s*[:|]\s*(?:CLOSED|OPEN|PARTIALLY_CLOSED|DISPUTED_BUT_UNRESOLVED)\s*$",
        closure,
        re.MULTILINE,
    ))
    errors.extend(
        f"XHS prior finding closure contains unexpected finding: {finding_id}"
        for finding_id in sorted(listed - set(finding_ids))
    )
    return states, errors


def prior_closure_states(text: str, finding_ids: list[str] | None = None) -> tuple[dict[str, str], list[str]]:
    closure = section(text, "Previous Finding Closure", "Findings")
    errors: list[str] = []
    closure_ids = re.findall(r"^\s*\|?\s*((?:PTA|XHS)-\d{3,})\s*(?:\||:)", closure, re.MULTILINE)
    states: dict[str, str] = {}
    ids = sorted(set(item for item in closure_ids if item.startswith("PTA-"))) if finding_ids is None else list(finding_ids)
    errors.extend(
        f"previous finding closure contains foreign finding: {finding_id}"
        for finding_id in sorted(set(closure_ids))
        if not finding_id.startswith("PTA-")
    )
    if finding_ids is not None:
        rows = re.findall(
            rf"^\s*\|?\s*(PTA-\d{{3,}})\s*(?:\||:)\s*({'|'.join(sorted(CLOSURE_STATES))})\b",
            closure,
            re.MULTILINE,
        )
        row_ids = [finding_id for finding_id, _ in rows]
        for finding_id in sorted(set(row_ids) - set(ids)):
            errors.append(f"previous finding closure contains unexpected finding: {finding_id}")
        for finding_id in ids:
            count = row_ids.count(finding_id)
            if count > 1:
                errors.append(f"previous finding closure contains duplicate finding: {finding_id}")
    for finding_id in ids:
        lines = [line for line in closure.splitlines() if re.search(rf"\b{re.escape(finding_id)}\b", line)]
        found: set[str] = set()
        for line in lines:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if finding_id in cells:
                index = cells.index(finding_id)
                if index + 1 < len(cells) and cells[index + 1] in CLOSURE_STATES:
                    found.add(cells[index + 1])
                    continue
            match = re.search(
                rf"\b{re.escape(finding_id)}\b[^\n]*?\b(?:Status:\s*)?({'|'.join(sorted(CLOSURE_STATES))})\b",
                line,
            )
            if match:
                found.add(match.group(1))
        if len(found) != 1:
            errors.append(f"previous finding closure state is missing or ambiguous: {finding_id}")
        else:
            states[finding_id] = found.pop()
    return states, errors


def shell_syntax_errors(command: str) -> list[str]:
    errors: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "`" or (char == "$" and command[index:index + 2] == "$("):
                errors.append("command substitution cannot be READY")
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in {"<", ">"} and command[index:index + 2] in {"<(", ">("}:
            errors.append("process substitution cannot be READY")
        elif char == ">":
            errors.append("output redirection cannot be READY")
        elif char in {";", "|", "&", "\n", "\r"}:
            errors.append("each CMD block must contain one command without shell chaining")
        elif char == "`" or (char == "$" and command[index:index + 2] == "$("):
            errors.append("command substitution cannot be READY")
        elif char == "$":
            errors.append("shell parameter expansion cannot be READY")
        elif char in {"*", "?", "[", "~"}:
            errors.append("shell pathname or home expansion cannot be READY")
        index += 1
    if quote is not None or escaped:
        errors.append("command quoting is incomplete")
    return list(dict.fromkeys(errors))


def _forwarding_operands(executable: str, tokens: list[str]) -> tuple[list[str] | None, str | None]:
    """Parse known transparent wrappers without treating every option as safe."""
    index = 1
    if executable == "env":
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                index += 1
                continue
            if token in {"-i", "--ignore-environment", "-0", "--null"}:
                index += 1
                continue
            if token in {"-u", "--unset", "-C", "--chdir"}:
                if index + 1 >= len(tokens):
                    return None, f"env option {token} is missing its operand"
                index += 2
                continue
            if token.startswith("--unset=") or token.startswith("--chdir="):
                index += 1
                continue
            if (token.startswith("-u") or token.startswith("-C")) and len(token) > 2:
                index += 1
                continue
            if token in {"-S", "--split-string"} or token.startswith("--split-string="):
                return None, "env split-string form is opaque"
            if token.startswith("-"):
                return None, f"unsupported env option is opaque: {token}"
            break
        return tokens[index:], None

    if executable == "timeout":
        options_with_values = {"-k", "--kill-after", "-s", "--signal"}
        while index < len(tokens) and tokens[index].startswith("-"):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in options_with_values:
                index += 2
            elif any(token.startswith(option + "=") for option in options_with_values):
                index += 1
            elif token in {"--foreground", "--preserve-status"}:
                index += 1
            else:
                return None, f"opaque timeout option: {token}"
        if index >= len(tokens):
            return None, "timeout does not expose a command"
        index += 1  # duration
        return tokens[index:], None

    if executable == "nice":
        while index < len(tokens) and tokens[index].startswith("-"):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token == "-n":
                index += 2
            elif token.startswith("-n") and len(token) > 2:
                index += 1
            else:
                return None, f"opaque nice option: {token}"
        return tokens[index:], None

    if executable == "stdbuf":
        while index < len(tokens) and tokens[index].startswith("-"):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in {"-i", "-o", "-e"}:
                index += 2
            elif len(token) > 2 and token[:2] in {"-i", "-o", "-e"}:
                index += 1
            else:
                return None, f"opaque stdbuf option: {token}"
        return tokens[index:], None

    if executable == "setsid":
        while index < len(tokens) and tokens[index].startswith("-"):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in {"-f", "-w", "-c", "--fork", "--wait", "--ctty"}:
                index += 1
            elif token in {"-C", "--wait-child"}:
                index += 2
            else:
                return None, f"opaque setsid option: {token}"
        return tokens[index:], None

    if executable == "time":
        while index < len(tokens) and tokens[index].startswith("-"):
            token = tokens[index]
            if token in {"-p", "--portability", "--verbose"}:
                index += 1
            elif token in {"-f", "--format"}:
                index += 2
            elif token.startswith("--format="):
                index += 1
            elif token in {"-o", "--output", "-a", "--append"} or token.startswith("--output="):
                return None, "time output file is a local mutation"
            else:
                return None, f"opaque time option: {token}"
        return tokens[index:], None

    if executable == "exec":
        while index < len(tokens) and tokens[index].startswith("-"):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token == "-a":
                index += 2
            elif token == "-l":
                index += 1
            else:
                return None, f"opaque exec option: {token}"
        return tokens[index:], None

    if executable == "nohup":
        if index < len(tokens) and tokens[index] == "--":
            index += 1
        return tokens[index:], None

    if executable == "busybox":
        if index >= len(tokens):
            return None, "busybox does not expose an applet"
        return tokens[index:], None

    if executable == "command":
        while index < len(tokens) and tokens[index] in {"-p"}:
            index += 1
        return tokens[index:], None

    return None, f"unsupported wrapper: {executable}"


def requested_method(args: list[str], long_option: str) -> str | None:
    for index, arg in enumerate(args):
        if arg == "-X":
            return args[index + 1].upper() if index + 1 < len(args) else ""
        if arg.startswith("-") and not arg.startswith("--") and "X" in arg[1:]:
            suffix = arg.split("X", 1)[1]
            return (suffix or (args[index + 1] if index + 1 < len(args) else "")).upper()
        if arg.lower() == long_option:
            return args[index + 1].upper() if index + 1 < len(args) else ""
        prefix = long_option + "="
        if arg.lower().startswith(prefix):
            return arg[len(prefix):].upper()
    return None


def _classify_curl(args: list[str]) -> CommandCapability:
    allowed_flags = {
        "-f", "--fail", "--fail-with-body", "-s", "--silent", "-S", "--show-error",
        "-L", "--location", "-I", "--head", "--compressed", "--globoff",
    }
    mutation_flags = {
        "-d", "-F", "-T", "--data", "--data-raw", "--data-binary", "--data-urlencode",
        "--form", "--form-string", "--upload-file", "--json", "--post-data", "--post-file",
    }
    credential_or_config_flags = {
        "-u", "--user", "--config", "-k", "--netrc", "--netrc-file", "--netrc-optional",
        "--cert", "--key", "--cacert", "--capath", "--proxy-cert", "--proxy-key",
        "--proxy-cacert", "--proxy-capath", "--proxy-user", "--delegation", "--negotiate",
        "--ntlm", "--ntlm-wb", "--digest", "--anyauth", "--basic", "--oauth2-bearer",
        "-b", "--cookie", "-c", "--cookie-jar",
    }
    method: str | None = None
    index = 0
    urls = 0
    while index < len(args):
        token = args[index]
        lower = token.lower()
        if lower in credential_or_config_flags or any(
            lower == flag or lower.startswith(flag + "=")
            for flag in credential_or_config_flags
        ):
            return _unsafe("curl credential, config, or delegated-auth option cannot be READY")
        if lower.startswith(("-u", "-k", "-b", "-c")) and not lower.startswith("--"):
            return _unsafe("curl credential, config, or delegated-auth option cannot be READY")
        if lower in mutation_flags or any(lower.startswith(flag + "=") for flag in mutation_flags):
            return _unsafe("remote mutation command cannot be READY")
        if lower.startswith("-d") and not lower.startswith("--"):
            return _unsafe("remote mutation command cannot be READY")
        if lower.startswith("-f") and not lower.startswith("--") and lower != "-f":
            return _unsafe("remote mutation command cannot be READY")
        if lower.startswith("-t") and not lower.startswith("--"):
            return _unsafe("remote mutation command cannot be READY")
        if token.startswith("-X") and token != "-X":
            method = token[2:].upper()
            index += 1
            continue
        if token == "-X" or lower == "--request":
            if index + 1 >= len(args):
                return _unknown("curl request method is missing")
            method = args[index + 1].upper()
            index += 2
            continue
        if token.startswith("-x") and token != "-x":
            return _unknown("curl proxy option is not in the safe subset")
        if lower.startswith("--request="):
            method = token.split("=", 1)[1].upper()
            index += 1
            continue
        if lower == "--url":
            if index + 1 >= len(args):
                return _unknown("curl URL option is missing its operand")
            urls += 1
            index += 2
            continue
        if lower.startswith("--url="):
            if not token.split("=", 1)[1]:
                return _unknown("curl URL option is missing its operand")
            urls += 1
            index += 1
            continue
        if token.startswith("-"):
            if token not in allowed_flags:
                return _unknown(f"unsupported curl option is not proven read-only: {token}")
        else:
            urls += 1
        index += 1
    if method is not None and method not in {"GET", "HEAD"}:
        return _unsafe("remote mutation command cannot be READY")
    if urls == 0:
        return _unknown("curl command has no URL")
    return _safe("SAFE_REMOTE_READ")


def _classify_wget(args: list[str]) -> CommandCapability:
    # A normal GET writes a local file.  Only the no-download spider mode is
    # bounded enough for automatic execution.
    if "--spider" not in args:
        method = requested_method(args, "--method")
        if method and method not in {"GET", "HEAD"}:
            return _unsafe("remote mutation command cannot be READY")
        return _unknown("wget GET may write a local file; only --spider is proven read-only")
    index = 0
    while index < len(args):
        token = args[index]
        lower = token.lower()
        if lower in {"--spider", "-q", "--quiet", "-nv", "--no-verbose", "-s", "--server-response"}:
            index += 1
            continue
        if lower == "--method":
            if index + 1 >= len(args):
                return _unknown("wget method is missing its operand")
            if args[index + 1].upper() not in {"GET", "HEAD"}:
                return _unsafe("remote mutation command cannot be READY")
            index += 2
            continue
        if lower.startswith("--method="):
            if token.split("=", 1)[1].upper() not in {"GET", "HEAD"}:
                return _unsafe("remote mutation command cannot be READY")
            index += 1
            continue
        if lower in {"--post-data", "--post-file", "--body-data", "--body-file", "--upload-file", "-o", "-O", "--output-document"}:
            return _unsafe("remote mutation or local output command cannot be READY")
        if token.startswith("-"):
            return _unknown(f"unsupported wget option is not proven read-only: {token}")
        index += 1
    return _safe("SAFE_REMOTE_READ")


def _classify_gh(args: list[str]) -> CommandCapability:
    index = 0
    while index < len(args):
        token = args[index].lower()
        if token in {"-r", "--repo", "--hostname"}:
            index += 2
            continue
        if token.startswith("--repo=") or token.startswith("--hostname="):
            index += 1
            continue
        break
    if index >= len(args):
        return _unknown("GitHub CLI command is not proven read-only")
    command = args[index].lower()
    if command == "api":
        api_args = args[index + 1:]
        method = requested_method(api_args, "--method")
        if method is not None and method not in {"GET", "HEAD"}:
            return _unsafe("remote mutation command cannot be READY")
        mutation_markers = {"-f", "--raw-field", "--field", "--input", "--method=post", "--method=put", "--method=patch", "--method=delete"}
        if any(
            arg.lower() in mutation_markers
            or arg.lower().startswith(("--field=", "--raw-field=", "--input="))
            or (arg.lower().startswith("-f") and arg.lower() != "-f")
            for arg in api_args
        ):
            return _unsafe("remote mutation command cannot be READY")
        if any(arg.lower() in {"-f", "--raw-field", "--field", "--input", "--method"} for arg in api_args):
            return _unknown("GitHub API input/method form is not proven read-only")
        return _safe("SAFE_REMOTE_READ")
    if len(args) > index + 1 and (command, args[index + 1].lower()) in REMOTE_MUTATION_PAIRS:
        return _unsafe("remote mutation command cannot be READY")
    if (command, args[index + 1].lower() if len(args) > index + 1 else "") in GH_READ_COMMANDS:
        return _safe("SAFE_REMOTE_READ")
    return _unknown("GitHub CLI subcommand is not proven read-only")


def _classify_git(args: list[str]) -> CommandCapability:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] in {"-C", "--git-dir", "--work-tree"}:
            index += 2
        elif args[index] in {"--no-pager", "--no-replace-objects"}:
            index += 1
        else:
            return _unknown(f"unsupported git global option is not proven read-only: {args[index]}")
    if index >= len(args):
        return _unknown("git command is not proven read-only")
    command = args[index].lower()
    rest = [arg.lower() for arg in args[index + 1:]]
    if command in {"push", "fetch", "pull", "commit", "merge", "rebase", "reset", "checkout", "switch", "clean", "rm", "config", "add", "mv", "tag", "submodule", "worktree"}:
        return _unsafe("destructive or external git command cannot be READY")
    if command == "remote":
        if rest and rest[0] in {"add", "remove", "rm", "rename", "set-url", "set-branches"}:
            return _unsafe("git remote mutation command cannot be READY")
        if rest and rest[0] not in {"-v", "--verbose", "get-url", "get-branches", "show"}:
            return _unknown("git remote form is not proven read-only")
        return _safe("SAFE_LOCAL_INSPECTION")
    if command not in SAFE_GIT_COMMANDS:
        return _unknown(f"git subcommand is not proven read-only: {command}")
    if any(
        arg in {"-o", "--output"}
        or arg.startswith("--output=")
        or (arg.startswith("-o") and len(arg) > 2 and not arg.startswith("--"))
        for arg in rest
    ):
        return _unknown("git output file is a local mutation")

    allowed_args = {
        "diff": {"--check"},
        "log": set(),
        "ls-files": set(),
        "rev-parse": set(),
        "show": set(),
        "status": {"--short", "--porcelain", "--branch"},
    }
    if any(arg.startswith("-") and arg not in allowed_args[command] for arg in rest):
        return _unknown(f"git {command} option is not proven read-only")
    return _safe("SAFE_LOCAL_INSPECTION")


def _classify_safe_read(executable: str, args: list[str]) -> CommandCapability:
    """Allow only the small, option-bounded local inspection subset."""
    if executable in {"true", "false", "pwd"}:
        if args:
            return _unknown(f"{executable} arguments are not proven read-only")
        return _safe("SAFE_LOCAL_INSPECTION")
    if executable in {"printf", "echo"}:
        return _safe("SAFE_LOCAL_INSPECTION") if not any(arg.startswith("-") for arg in args) else _unknown(
            f"{executable} option form is not proven read-only"
        )
    if executable == "sort":
        if any(arg.startswith("-") for arg in args):
            return _unknown("sort options, including output-file forms, are not proven read-only")
        return _safe("SAFE_LOCAL_INSPECTION")
    if executable == "uniq":
        if len(args) > 1 or any(arg.startswith("-") for arg in args):
            return _unknown("uniq multiple-input or option forms are not proven read-only")
        return _safe("SAFE_LOCAL_INSPECTION")
    if executable in {"cat", "cut", "head", "ls", "tail", "tr", "wc"}:
        if any(arg.startswith("-") for arg in args):
            return _unknown(f"{executable} option form is not proven read-only")
        for arg in args:
            path = pathlib.PurePosixPath(arg)
            if arg.startswith("/") or ".." in path.parts or any(part.startswith(".") for part in path.parts):
                return _unknown("local inspection operand is not a bounded repository-relative path")
            if any(part.lower() in {".ssh", ".aws", ".gnupg", ".env"} or part.lower().endswith((".pem", ".key", ".p12", ".pfx")) for part in path.parts):
                return _unknown("local inspection operand may be sensitive")
        return _safe("SAFE_LOCAL_INSPECTION")
    return _unknown(f"{executable} is not in the bounded local inspection subset")


def _classify_python(executable: str, tokens: list[str]) -> CommandCapability:
    index = 1
    module: str | None = None
    while index < len(tokens):
        token = tokens[index]
        if token in {"-B", "-I", "-s", "-u"}:
            index += 1
            continue
        if token == "-m":
            if index + 1 >= len(tokens):
                return _unknown("Python -m option is missing its module")
            module = tokens[index + 1]
            index += 2
            break
        if token in {"-c", "-e", "-"} or token.startswith("-c") or token.startswith("-e"):
            return _unknown("opaque interpreter wrapper cannot be READY")
        if token.startswith("-"):
            return _unknown(f"unsupported Python option is not proven safe: {token}")
        return _unknown("repository-controlled Python script execution cannot be proven safe")
    if module not in {"json.tool"}:
        return _unknown("Python module execution is not proven safe")
    module_args = tokens[index:]
    if module == "json.tool":
        if len(module_args) > 1 or any(arg.startswith("-") for arg in module_args):
            return _unknown("json.tool argument form is not proven safe")
        return _safe("SAFE_TEST")
    if any(arg.startswith("-") or ".." in pathlib.PurePosixPath(arg).parts for arg in module_args):
        return _unknown("Python module arguments are not proven safe")
    return _safe("SAFE_TEST")


def _classify_package(tokens: list[str], repo_root: pathlib.Path | None, stack: tuple[str, ...]) -> CommandCapability:
    executable = _name(tokens[0])
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index].lower()
        if token in {"--silent", "--quiet", "--json", "--global=false", "--foreground-scripts"}:
            index += 1
        else:
            return _unknown(f"package-manager option is not proven safe: {tokens[index]}")
    if index >= len(tokens):
        return _unknown("package-manager command is not proven safe")
    subcommand = tokens[index].lower()
    dangerous = {
        "publish", "pack", "version", "release", "deploy", "install", "i", "add", "remove", "rm", "uninstall",
        "update", "upgrade", "link", "unlink", "login", "logout", "deprecate", "dist-tag", "owner", "access",
        "init", "config", "exec", "dlx", "set", "unpublish",
    }
    if subcommand in dangerous:
        return _unsafe("package publish/release/install or other mutation command cannot be READY")
    if executable == "npm" and subcommand not in {"run", "run-script", "test"}:
        return _unknown(f"npm subcommand is not proven safe: {subcommand}")
    if executable == "pnpm" and subcommand not in {"run", "test"} and subcommand.startswith("-"):
        return _unknown("pnpm command is not proven safe")
    if executable == "yarn" and subcommand not in {"run", "test"}:
        # Yarn's bare script form is handled below; built-in commands remain
        # unknown unless explicitly classified as a read-only script.
        if subcommand in {"help", "--version", "version"}:
            return _safe("SAFE_LOCAL_INSPECTION") if subcommand != "version" else _unsafe("package version command cannot be READY")

    if subcommand in {"run", "run-script"}:
        index += 1
        if index >= len(tokens) or tokens[index].startswith("-"):
            return _unknown("package script name is missing")
        script_name = tokens[index]
        index += 1
    elif subcommand == "test":
        script_name = "test"
        index += 1
    else:
        script_name = subcommand
        index += 1
    if index < len(tokens):
        return _unknown("package script arguments are not proven safe")
    if repo_root is None:
        return _unknown("package script cannot be resolved without a repository root")
    package_file = repo_root / "package.json"
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _unknown("package script cannot be resolved from package.json")
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict):
        return _unknown("package script cannot be resolved from package.json")
    names = [f"pre{script_name}", script_name, f"post{script_name}"]
    decisions: list[CommandCapability] = []
    for name in names:
        if name not in scripts:
            continue
        script = scripts[name]
        if not isinstance(script, str) or not script.strip():
            return _unknown(f"package script {name} is not a concrete command")
        key = f"{package_file}:{name}"
        if key in stack or len(stack) >= 8:
            return _unknown("recursive package script chain cannot be proven safe")
        decisions.append(_classify_command(script, repo_root, stack + (key,)))
    if not decisions:
        return _unknown(f"package script {script_name} is not defined")
    for decision in decisions:
        if not decision.auto_execute:
            return _unknown(f"package script {script_name} is not proven safe: {decision.reason}")
    return _safe("SAFE_PACKAGE_VERIFICATION")


def _classify_tokens(tokens: list[str], repo_root: pathlib.Path | None, stack: tuple[str, ...] = ()) -> CommandCapability:
    if not tokens:
        return _unknown("command is empty")
    executable = _name(tokens[0])
    lowered = [token.lower() for token in tokens[1:]]

    if executable == "env" or executable in FORWARDING_WRAPPERS:
        if executable == "command" and len(tokens) >= 3 and tokens[1] in {"-v", "-V"}:
            return _safe("SAFE_LOCAL_INSPECTION")
        downstream, wrapper_error = _forwarding_operands(executable, tokens)
        if wrapper_error:
            return _unknown(f"opaque {executable} wrapper cannot be READY: {wrapper_error}")
        if not downstream:
            return _unknown(f"opaque {executable} wrapper cannot be READY: wrapper does not expose an executable")
        return _classify_tokens(downstream, repo_root, stack)
    if executable == "command":
        return _unknown("opaque command wrapper cannot be READY")
    if executable in SHELL_EXECUTABLES:
        return _unknown("opaque shell wrapper cannot be READY")
    if executable in {"eval", "source", ".", "xargs"}:
        label = "opaque xargs execution" if executable == "xargs" else "opaque indirect execution"
        return _unknown(f"{label} cannot be READY")
    if executable in DESTRUCTIVE_EXECUTABLES:
        return _unsafe("destructive file command cannot be READY")
    if executable in INTERPRETER_EXECUTABLES:
        return _classify_python(executable, tokens) if executable in {"python", "python3"} else _unknown("opaque interpreter wrapper cannot be READY")
    if executable in PACKAGE_MANAGERS:
        return _classify_package(tokens, repo_root, stack)
    if executable == "git":
        return _classify_git(tokens[1:])
    if executable == "curl":
        return _classify_curl(tokens[1:])
    if executable == "wget":
        return _classify_wget(tokens[1:])
    if executable == "gh":
        return _classify_gh(tokens[1:])
    if executable == "find":
        if any(token in {"-exec", "-execdir", "-ok", "-okdir"} for token in lowered):
            return _unknown("indirect find execution cannot be proven safe")
        if any(token.startswith("-") for token in lowered):
            return _unknown("find option form is not proven read-only")
        return _safe("SAFE_LOCAL_INSPECTION")
    if executable == "pytest":
        if any(token.startswith("-") for token in lowered):
            return _unknown("pytest option form is not proven read-only")
        return _safe("SAFE_TEST")
    if executable == "terraform":
        return _unknown("infrastructure command is not proven safe")
    if executable in SAFE_READ_EXECUTABLES or executable == "sort" or executable == "uniq":
        return _classify_safe_read(executable, tokens[1:])
    return _unknown(f"executable is not in the allowed capability subset: {executable}")


def _classify_command(command: str, repo_root: pathlib.Path | None, stack: tuple[str, ...] = ()) -> CommandCapability:
    syntax_errors = shell_syntax_errors(command)
    if syntax_errors:
        return _unknown("; ".join(syntax_errors))
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _unknown("command cannot be parsed conservatively")
    decision = _classify_tokens(tokens, repo_root, stack)
    if decision.auto_execute:
        # A grammar proof is not an execution proof: PATH, shell functions,
        # executable contents, startup configuration, plugins, symlinks and the
        # runtime environment are not bound by this protocol. Even a familiar
        # command name must not grant execution authority. A future executor
        # needs an independently verified, immutable execution attestation;
        # a claim in an audit response cannot supply one.
        return _unknown("execution environment and transitive effects are not attested; STOP / MANUAL_REVIEW")
    return decision


def classify_command_semantics(command: str, repo_root: pathlib.Path | None = None) -> CommandCapability:
    """Classify a command before a READY pack may expose it to execution."""
    return _classify_command(command, repo_root)


def auto_execute_allowed(command: str, repo_root: pathlib.Path | None = None) -> bool:
    return classify_command_semantics(command, repo_root).auto_execute


def command_safety_errors(command: str, repo_root: pathlib.Path | None = None) -> list[str]:
    syntax_errors = shell_syntax_errors(command)
    if syntax_errors:
        return list(dict.fromkeys(syntax_errors + ["AUTO_EXECUTE=false: command syntax is not in the safe subset"]))
    decision = classify_command_semantics(command, repo_root)
    if decision.auto_execute:
        return []
    return [f"AUTO_EXECUTE=false: {decision.capability}: {decision.reason}"]


def execution_pack_errors(
    text: str,
    actionable_ids: list[str],
    repo_root: pathlib.Path | None = None,
) -> list[str]:
    errors: list[str] = []
    pack_section = section(text, "Remediation Execution Pack", "Test and Verification Assessment")
    packs = named_blocks(pack_section, "PTA") if pack_section else []
    pack_ids = [finding_id for finding_id, _ in packs]

    for finding_id in actionable_ids:
        count = pack_ids.count(finding_id)
        if count != 1:
            errors.append(f"execution pack count for {finding_id} is {count}, expected 1")
    for finding_id in set(pack_ids) - set(actionable_ids):
        errors.append(f"execution pack has no matching open finding: {finding_id}")

    for finding_id, block in packs:
        mode = scalar(block, "EXECUTION_MODE")
        status = scalar(block, "COMMAND_STATUS")
        report_required = scalar(block, "REPORT_REQUIRED")
        stop_reason = scalar(block, "STOP_REASON")
        prompt_match = re.search(
            r"^LOW_CAPABILITY_EXECUTION_PROMPT:\s*\n?(.*?)(?=^REPORT_REQUIRED:|^STOP_REASON:|^####\s+CMD-|\Z)",
            block,
            re.MULTILINE | re.DOTALL,
        )
        prompt = prompt_match.group(1).strip() if prompt_match else ""

        if mode not in {"GUIDED", "NORMAL"}:
            errors.append(f"{finding_id}: invalid or missing EXECUTION_MODE")
        if status not in COMMAND_STATUSES:
            errors.append(f"{finding_id}: invalid or missing COMMAND_STATUS")
        if report_required != "YES":
            errors.append(f"{finding_id}: REPORT_REQUIRED must be YES")
        if len(prompt) < 40 or PLACEHOLDER_RE.search(prompt):
            errors.append(f"{finding_id}: low-capability execution prompt is missing, too short, or contains placeholders")

        commands = named_blocks(block, "CMD", heading_level=4)
        if status == "READY":
            if not commands:
                errors.append(f"{finding_id}: READY execution pack requires at least one command")
            if stop_reason not in (None, "N/A"):
                errors.append(f"{finding_id}: READY execution pack must use STOP_REASON: N/A")
        elif status in COMMAND_STATUSES:
            if commands:
                errors.append(f"{finding_id}: non-READY execution pack must not include runnable commands")
            if not stop_reason or stop_reason == "N/A" or PLACEHOLDER_RE.search(stop_reason):
                errors.append(f"{finding_id}: non-READY execution pack requires a concrete STOP_REASON")

        seen_commands: set[str] = set()
        for command_id, command_block in commands:
            if command_id in seen_commands:
                errors.append(f"{finding_id}: duplicate command id {command_id}")
            seen_commands.add(command_id)
            required = {
                "PURPOSE", "WORKING_DIRECTORY", "COMMAND_CLASS", "EXECUTION_POLICY",
                "PRECONDITIONS", "COMMAND", "EXPECTED_RESULT", "ON_FAILURE",
            }
            fields = {name: scalar(command_block, name) for name in required}
            for name, value in fields.items():
                if not value:
                    errors.append(f"{finding_id}/{command_id}: missing {name}")
                elif PLACEHOLDER_RE.search(value):
                    errors.append(f"{finding_id}/{command_id}: {name} contains a placeholder")

            working_directory = fields["WORKING_DIRECTORY"] or ""
            parts = pathlib.PurePosixPath(working_directory).parts
            if working_directory.startswith(("/", "~")) or ".." in parts:
                errors.append(f"{finding_id}/{command_id}: WORKING_DIRECTORY must be repository-relative")

            command_class = fields["COMMAND_CLASS"]
            policy = fields["EXECUTION_POLICY"]
            command = fields["COMMAND"] or ""
            if command_class not in COMMAND_CLASSES:
                errors.append(f"{finding_id}/{command_id}: invalid COMMAND_CLASS")
            if policy not in EXECUTION_POLICIES:
                errors.append(f"{finding_id}/{command_id}: invalid EXECUTION_POLICY")
            if policy == "AUTO" and command_class not in {"SAFE_READ", "SAFE_TEST", "SAFE_BUILD"}:
                errors.append(f"{finding_id}/{command_id}: AUTO is not allowed for {command_class}")
            if command_class == "MODIFYING" and policy != "VERIFY_THEN_RUN":
                errors.append(f"{finding_id}/{command_id}: MODIFYING requires VERIFY_THEN_RUN")
            command_root = repo_root
            if command_root is not None and working_directory:
                candidate_root = (repo_root / working_directory).resolve()
                try:
                    candidate_root.relative_to(repo_root.resolve())
                except ValueError:
                    command_root = None
                else:
                    command_root = candidate_root
            for safety_error in command_safety_errors(command, command_root):
                errors.append(f"{finding_id}/{command_id}: {safety_error}")
            if "STOP" not in (fields["ON_FAILURE"] or "").upper():
                errors.append(f"{finding_id}/{command_id}: ON_FAILURE must explicitly STOP")
    return errors


def semantic_errors(
    text: str,
    values: dict[str, str],
    expected_findings: list[str],
    repo_root: pathlib.Path | None = None,
) -> list[str]:
    errors: list[str] = []
    verdict = values.get("VERDICT")
    is_xhs = values.get("AUDIT_TYPE") in {"XHS_USER_PANEL_REVIEW", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"}
    errors.extend(finding_namespace_errors(text, is_xhs))
    if is_xhs:
        required_sections = XHS_CANONICAL_SECTIONS[:-1]
        for title in required_sections:
            if not section(text, title):
                errors.append(f"XHS response is missing required section: {title}")
            if len(re.findall(rf"^##[ \t]+{re.escape(title)}[ \t]*$", text, re.MULTILINE)) > 1:
                errors.append(f"XHS response has duplicate section: {title}")
        errors.extend(xhs_finding_id_errors(text))
        personas = section(text, "SELF_SELECTED_PERSONAS", "RAW_USER_REACTIONS")
        if not re.search(r"PERSONA_NAME:\s*.+", personas):
            errors.append("XHS response is missing self-selected PERSONA_NAME")
        if re.search(r"PERSONA_NAME:\s*(?:普通用户|拼豆用户|宠物客户)\s*$", personas, re.MULTILINE):
            errors.append("XHS response uses a forbidden fixed persona")
        findings = section(text, "WHAT_DOES_NOT_WORK", "GOOD_POINTS")
        p01 = re.findall(r"^PRIORITY:\s*(P[01])\s*$", findings, re.MULTILINE)
        if verdict == "PASS" and p01:
            errors.append("XHS PASS is inconsistent with P0/P1 findings")
        if verdict == "FAIL" and not section(text, "CODEX_REMEDIATION_PROMPT"):
            errors.append("XHS FAIL requires CODEX_REMEDIATION_PROMPT")
        goal_status = scalar(section(text, "OVERALL_EVALUATION", "PREVIOUS_FINDING_CLOSURE_CHECK"), "GOAL_STATUS")
        if goal_status and goal_status not in {"GOAL_REACHED", "ITERATION_REQUIRED", "MORE_EVIDENCE_REQUIRED", "USER_DECISION_REQUIRED", "BLOCKED"}:
            errors.append("XHS response has invalid GOAL_STATUS")
        if goal_status == "GOAL_REACHED" and verdict != "PASS":
            errors.append("XHS GOAL_REACHED requires PASS")
        if verdict == "PASS" and goal_status != "GOAL_REACHED":
            errors.append("XHS PASS requires GOAL_REACHED")
        if len(re.findall(r"^##[ \t]+Final Gate Decision[ \t]*$", text, re.MULTILINE)) != 1:
            errors.append("XHS response must contain exactly one Final Gate Decision section")
    verdicts = all_verdicts(text)
    if verdicts and len(set(verdicts)) != 1:
        errors.append("contradictory verdict fields")
    final = section(text, "Final Gate Decision")
    final_verdicts = all_verdicts(final)
    if not final_verdicts:
        errors.append("missing Final Gate Decision verdict")
    elif final_verdicts[-1] != verdict:
        errors.append("Final Gate Decision verdict contradicts header")

    if verdict == "PASS":
        for marker in ("Status: OPEN", "Status: PARTIALLY_CLOSED", "Status: DISPUTED_BUT_UNRESOLVED", "INCOMPLETE", "MATERIAL_EVIDENCE_INSUFFICIENT", "AUDIT_EVIDENCE_SUFFICIENT: false"):
            if marker.lower() in text.lower():
                errors.append(f"PASS is inconsistent with {marker}")
        requests = re.findall(r"^###\s+ER-\d+.*?(?=^###\s+|^##\s+|\Z)", text, re.MULTILINE | re.DOTALL)
        if any(re.search(r"Required evidence:\s*(?!N/A\b).+", block, re.IGNORECASE) for block in requests):
            errors.append("PASS has a material evidence request")

    unresolved_prior: list[str] = []
    if values.get("AUDIT_TYPE") == "REMEDIATION_VERIFICATION":
        closure = section(text, "Previous Finding Closure", "Findings")
        if not closure:
            errors.append("missing Previous Finding Closure section")
        closure_states, closure_errors = prior_closure_states(text, expected_findings)
        errors.extend(closure_errors)
        unresolved_prior = [
            finding_id for finding_id, state in closure_states.items()
            if state in {"OPEN", "PARTIALLY_CLOSED", "DISPUTED_BUT_UNRESOLVED"}
        ]
    elif values.get("AUDIT_TYPE") == "XHS_USER_PANEL_REMEDIATION_VERIFICATION":
        closure = section(text, "PREVIOUS_FINDING_CLOSURE_CHECK")
        if not closure:
            errors.append("missing PREVIOUS_FINDING_CLOSURE_CHECK section")
        closure_states, closure_errors = xhs_prior_closure_states(text, expected_findings)
        errors.extend(closure_errors)
        unresolved_prior = [
            finding_id for finding_id, state in closure_states.items()
            if state in {"OPEN", "PARTIALLY_CLOSED", "DISPUTED_BUT_UNRESOLVED"}
        ]

    open_findings = [] if is_xhs else actionable_finding_ids(text)
    actionable_ids = sorted(set(open_findings) | set(unresolved_prior))
    if open_findings and not re.search(r"^##\s+Remediation Plan\b", text, re.MULTILINE):
        errors.append("actionable findings require a Remediation Plan")
    if verdict == "FAIL" and not actionable_ids and not is_xhs:
        errors.append("FAIL requires at least one open actionable finding")
    if verdict in {"FAIL", "PASS_WITH_NOTES"} and actionable_ids and not is_xhs:
        errors.extend(execution_pack_errors(text, actionable_ids, repo_root))
    return errors


def expected_findings_from_file(path: pathlib.Path, prefix: str = "PTA") -> tuple[list[str], list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"cannot load authoritative expected findings file {path}: {exc}"]
    if not isinstance(payload, dict) or not isinstance(payload.get("finding_ids"), list):
        return [], [f"authoritative expected findings file has invalid shape: {path}"]
    ids = payload["finding_ids"]
    if any(not isinstance(item, str) or not re.fullmatch(rf"{re.escape(prefix)}-\d{{3,}}", item) for item in ids):
        return [], [f"authoritative expected findings file has invalid finding IDs: {path}"]
    if len(ids) != len(set(ids)):
        return [], [f"authoritative expected findings file has duplicate finding IDs: {path}"]
    return ids, []


def resolve_expected_findings(
    response_path: pathlib.Path,
    audit_type: str | None,
    manual_ids: list[str],
    expected_file: str | None,
) -> tuple[list[str], list[str]]:
    if audit_type not in {"REMEDIATION_VERIFICATION", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"}:
        return manual_ids, []
    if manual_ids and expected_file:
        return [], ["use either --expected-finding or --expected-findings-file, not both"]
    if manual_ids:
        if len(manual_ids) != len(set(manual_ids)):
            return [], ["manual expected findings contain duplicate IDs"]
        return manual_ids, []
    path = pathlib.Path(expected_file) if expected_file else response_path.parent / "EXPECTED_PREVIOUS_FINDINGS.json"
    if not path.is_file():
        return [], [
            "remediation verification requires authoritative prior findings; "
            f"expected {path} or explicit --expected-finding overrides"
        ]
    prefix = "XHS" if audit_type == "XHS_USER_PANEL_REMEDIATION_VERIFICATION" else "PTA"
    return expected_findings_from_file(path, prefix)


def validate(args: argparse.Namespace) -> list[str]:
    response_path = pathlib.Path(args.response)
    text = response_path.read_text(encoding="utf-8")
    values, errors = exact_header(text)
    if values.get("AUDIT_PROTOCOL_VERSION") not in (None, "1"):
        errors.append("unsupported protocol version")
    if args.task_id and values.get("HANDOFF_ID") != args.task_id:
        errors.append(f"handoff id mismatch: {values.get('HANDOFF_ID')} != {args.task_id}")
    if args.round is not None and values.get("ROUND") != str(args.round):
        errors.append(f"round mismatch: {values.get('ROUND')} != {args.round}")
    if args.audit_type and values.get("AUDIT_TYPE") != args.audit_type:
        errors.append(f"audit type mismatch: {values.get('AUDIT_TYPE')} != {args.audit_type}")
    if args.expected_subject_id and values.get("AUDIT_SUBJECT_ID") != args.expected_subject_id:
        errors.append("audit subject id mismatch")
    expected_findings, expected_errors = resolve_expected_findings(
        response_path,
        args.audit_type or values.get("AUDIT_TYPE"),
        args.expected_finding or [],
        args.expected_findings_file,
    )
    errors.extend(expected_errors)
    repo_root = pathlib.Path(args.repo).resolve() if args.repo else pathlib.Path.cwd().resolve()
    errors.extend(semantic_errors(text, values, expected_findings, repo_root))
    if args.artifact:
        artifact = pathlib.Path(args.artifact).read_text(encoding="utf-8")
        artifact_values, artifact_errors = exact_header(artifact)
        errors.extend(f"artifact: {error}" for error in artifact_errors)
        errors.extend(f"artifact: {error}" for error in semantic_errors(artifact, artifact_values, expected_findings, repo_root))
        for key in ("HANDOFF_ID", "ROUND", "AUDIT_TYPE", "AUDIT_SUBJECT_ID", "VERDICT"):
            if values.get(key) != artifact_values.get(key):
                errors.append(f"chat/artifact conflict: {key}")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("response")
    ap.add_argument("--task-id")
    ap.add_argument("--round", type=int)
    ap.add_argument("--audit-type", choices=["INITIAL", "REMEDIATION_VERIFICATION", "XHS_USER_PANEL_REVIEW", "XHS_USER_PANEL_REMEDIATION_VERIFICATION"])
    ap.add_argument("--expected-subject-id")
    ap.add_argument("--expected-finding", action="append")
    ap.add_argument("--expected-findings-file")
    ap.add_argument("--artifact")
    ap.add_argument("--repo", help="Repository root used to resolve package scripts")
    args = ap.parse_args()
    errors = validate(args)
    if errors:
        print("SEMANTICALLY_INVALID" if any(
            marker in error for error in errors
            for marker in ("PASS", "conflict", "finding", "execution", "command")
        ) else "INVALID")
        for error in errors:
            print(f"- {error}")
        sys.exit(2)
    text = pathlib.Path(args.response).read_text(encoding="utf-8")
    print("VALID")
    print(f"VERDICT={exact_header(text)[0]['VERDICT']}")


if __name__ == "__main__":
    main()
