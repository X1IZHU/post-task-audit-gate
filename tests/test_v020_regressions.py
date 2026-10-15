"""Real disposable-repository regressions; no external browser audit is simulated."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import audit_integrity
import build_handoff
import remediation_protocol
from transport_policy import record_verified_download
from test_remediation_protocol import audit_response


class RepositoryRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = pathlib.Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.audit = self.repo / ".codex-audit"
        self.audit.mkdir()
        self.task = self.audit / "task.json"
        self.task.write_text(json.dumps({"task_id": "task", "original_task": "Verify the local fixture.",
                                         "baseline_quality": "UNKNOWN", "max_audit_rounds": 3}))
        self.impl = self.audit / "impl.md"
        self.results = self.audit / "tests.md"
        self.impl.write_text("Local synthetic fixture, not an external audit.\n")
        self.results.write_text("Local fixture assertion only.\n")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo)

    def build(self, number=1, previous=None, extra=()):
        argv = ["build_handoff.py", "--repo", str(self.repo), "--round", str(number),
                "--implementation-report", str(self.impl), "--test-results", str(self.results)]
        if previous:
            argv += ["--previous-audit", str(previous)]
        argv.extend(extra)
        with mock.patch.object(sys, "argv", argv):
            build_handoff.main()
        return self.audit / "sessions" / json.loads(self.task.read_text())["task_id"] / f"round-{number:02d}"

    def manifest(self, directory):
        return {entry["repository_path"]: entry
                for entry in json.loads((directory / "FILE_MANIFEST.json").read_text())}

    def test_untracked_nested_sources_are_bundled_without_changing_index(self):
        paths = ["new_module/binding.py", "new_module/nested/helper.py", "说明.md", "new_module/space name.py"]
        if os.name != "nt":
            paths += ['quote"file.py', "arrow -> name.py", "line\nbreak.py", "[literal].py", "back\\slash.py", "line\rreturn.py", "unicode\u2028separator.py"]
        for name in paths:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic source\n", encoding="utf-8")
        index_before = self.git("ls-files", "--stage", "-z")
        directory = self.build()
        manifest = self.manifest(directory)
        with zipfile.ZipFile(directory / "AUDIT_BUNDLE_task_R01.zip") as archive:
            for name in paths:
                self.assertEqual("COMPLETE", manifest[name]["status"], name)
                self.assertEqual((self.repo / name).read_bytes(), archive.read("CHANGED_FILES/" + name))
        self.assertEqual(index_before, self.git("ls-files", "--stage", "-z"))
        self.assertEqual([], audit_integrity.validate_deliverable_bundle(directory))

    def test_deleted_renamed_and_sensitive_entries_are_distinct(self):
        for name in ["old.py", "deleted.py"]:
            (self.repo / name).write_text("original source\n")
        self.git("add", "old.py", "deleted.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "Disposable fixture")
        self.git("mv", "old.py", "renamed.py")
        (self.repo / "deleted.py").unlink()
        (self.repo / "new_module").mkdir()
        (self.repo / "new_module/.env").write_text("SYNTHETIC-PRIVATE-MARKER\n")
        directory = self.build()
        manifest = self.manifest(directory)
        self.assertEqual("DELETED", manifest["deleted.py"]["status"])
        self.assertEqual("COMPLETE", manifest["renamed.py"]["status"])
        self.assertEqual("old.py", manifest["renamed.py"]["previous_repository_path"])
        self.assertEqual("OMITTED_SENSITIVE", manifest["new_module/.env"]["status"])
        with zipfile.ZipFile(directory / "AUDIT_BUNDLE_task_R01.zip") as archive:
            self.assertFalse(any(name.startswith("CHANGED_FILES/.codex-audit/") for name in archive.namelist()))
            self.assertFalse(any(b"SYNTHETIC-PRIVATE-MARKER" in archive.read(name) for name in archive.namelist()))

    def test_read_failure_records_reason_and_does_not_complete_bundle(self):
        target = self.repo / "unreadable.py"
        target.write_text("synthetic source\n")
        original = pathlib.Path.read_bytes
        def denied(path):
            if path == target:
                raise PermissionError("synthetic denied read")
            return original(path)
        with mock.patch.object(pathlib.Path, "read_bytes", denied):
            with self.assertRaisesRegex(ValueError, "READ_ERROR"):
                self.build()
        manifests = list((self.audit / "sessions/task").glob(".round-01.staging-*/FILE_MANIFEST.json"))
        self.assertEqual(1, len(manifests))
        self.assertEqual("READ_ERROR", self.manifest(manifests[0].parent)["unreadable.py"]["status"])
        self.assertFalse((self.audit / "sessions/task/round-01").exists())
        self.assertFalse(list((self.audit / "sessions/task").rglob("BUNDLE_COMPLETE.json")))

    def test_binary_is_not_misreported_as_unreadable(self):
        (self.repo / "binary.dat").write_bytes(b"\xff\xfe\x00")
        directory = self.build()
        self.assertEqual("OMITTED_BINARY", self.manifest(directory)["binary.dat"]["status"])

    def test_symlink_outside_repository_cannot_expand_evidence(self):
        with tempfile.TemporaryDirectory() as outside:
            target = pathlib.Path(outside) / "external.txt"
            target.write_text("SYNTHETIC-OUTSIDE-MARKER\n")
            try:
                (self.repo / "alias.py").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("Host does not support creating symlinks")
            directory = self.build()
            self.assertEqual("DENIED_PATH", self.manifest(directory)["alias.py"]["status"])
            with zipfile.ZipFile(directory / "AUDIT_BUNDLE_task_R01.zip") as archive:
                self.assertFalse(any(b"SYNTHETIC-OUTSIDE-MARKER" in archive.read(name) for name in archive.namelist()))

    def test_baseline_quality_defaults_unknown_and_preserves_explicit_choice(self):
        spec = self.audit / "original.md"
        spec.write_text("Local fixture task.\n")
        (self.repo / "existing-dirty.py").write_text("pre-existing candidate\n")
        for quality in [None, "EARLY_INITIALIZATION", "LATE_INITIALIZATION"]:
            with self.subTest(quality=quality):
                argv = [sys.executable, "-B", str(SCRIPT_DIR / "init_task.py"), "--repo", str(self.repo),
                        "--task-file", str(spec), "--task-id", "baseline-fixture"]
                if quality:
                    argv += ["--baseline-quality", quality]
                result = subprocess.run(argv, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                metadata = json.loads(self.task.read_text())
                self.assertEqual(quality or "UNKNOWN", metadata["baseline_quality"])
                self.assertTrue(metadata["worktree_was_dirty"])
        directory = self.build()
        self.assertIn("BASELINE_QUALITY: LATE_INITIALIZATION", (directory / "BASELINE_QUALITY.txt").read_text())

    def test_expected_missing_file_is_not_a_deletion(self):
        original = build_handoff.read_status
        def raced_status(repo):
            entries = original(repo)
            return entries + [{"status": "??", "path": "vanished.py"}]
        with mock.patch.object(build_handoff, "read_status", raced_status):
            with self.assertRaisesRegex(ValueError, "READ_ERROR.*vanished.py"):
                self.build()

    def test_staged_and_unstaged_changes_use_current_content(self):
        path = self.repo / "both.py"
        path.write_text("original\n")
        self.git("add", "both.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "Disposable fixture")
        path.write_text("staged\n")
        self.git("add", "both.py")
        path.write_text("unstaged candidate\n")
        before = self.git("ls-files", "--stage", "-z")
        directory = self.build()
        entry = self.manifest(directory)["both.py"]
        self.assertEqual("MM", entry["git_status"])
        self.assertEqual("unstaged candidate\n", (directory / entry["bundle_path"]).read_text())
        self.assertEqual(before, self.git("ls-files", "--stage", "-z"))
        self.assertIn("+unstaged candidate", (directory / "FINAL_DIFF.patch").read_text())

    def test_sensitive_symlink_target_is_omitted(self):
        (self.repo / ".env").write_text("SYNTHETIC-SENSITIVE-TARGET\n")
        try:
            (self.repo / "alias.py").symlink_to(".env")
        except (OSError, NotImplementedError):
            self.skipTest("Host does not support creating symlinks")
        directory = self.build()
        self.assertEqual("OMITTED_SENSITIVE", self.manifest(directory)["alias.py"]["status"])
        with zipfile.ZipFile(directory / "AUDIT_BUNDLE_task_R01.zip") as archive:
            self.assertFalse(any(b"SYNTHETIC-SENSITIVE-TARGET" in archive.read(n) for n in archive.namelist()))

    def test_explicit_missing_evidence_stops_completion(self):
        with self.assertRaisesRegex(ValueError, "READ_ERROR.*missing.py"):
            self.build(extra=["--evidence-path", "missing.py"])
        self.assertFalse((self.audit / "sessions/task/round-01").exists())

    def test_directory_candidate_is_explicitly_unsupported(self):
        (self.repo / "directory").mkdir()
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED.*directory"):
            self.build(extra=["--evidence-path", "directory"])

    def test_tracked_literal_pathspec_file_has_a_real_diff(self):
        (self.repo / "[g].py").write_text("original\n")
        (self.repo / "g.py").write_text("neighbor\n")
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "Disposable fixture")
        (self.repo / "[g].py").write_text("literal candidate\n")
        directory = self.build()
        diff = (directory / "FINAL_DIFF.patch").read_text()
        self.assertIn("+literal candidate", diff)
        self.assertIn("b/[g].py", diff)
        self.assertNotIn("b/g.py", diff)

    def response(self, directory, number, verdict):
        subject = json.loads((directory / "AUDIT_SUBJECT.json").read_text())["audit_subject_id"]
        text = audit_response(verdict, ()).replace("ROUND: 1", f"ROUND: {number}", 1)
        if number > 1:
            text = text.replace("AUDIT_TYPE: INITIAL", "AUDIT_TYPE: REMEDIATION_VERIFICATION", 1)
            text = text.replace("N/A — initial audit", "No prior actionable findings; previous round requested evidence.")
        text = text.replace("AUDIT_SUBJECT_ID: subject", f"AUDIT_SUBJECT_ID: {subject}", 1)
        response = directory / "AUDIT_RESPONSE.md"
        artifact = directory / f"AUDIT_RESPONSE_task_R{number:02d}.md"
        response.write_text(text)
        artifact.write_text(text)
        errors = record_verified_download(str(directory / "STATE.json"), str(artifact),
            expected_handoff_id="task", expected_round=number,
            expected_audit_type="INITIAL" if number == 1 else "REMEDIATION_VERIFICATION",
            expected_subject_id=subject)
        self.assertEqual([], errors)
        return response, artifact, subject

    def test_blocked_then_pass_reaches_completion_without_code_remediation(self):
        # Match init_task local exclusion so this test isolates completion policy,
        # not the separately planned candidate-fingerprint scope redesign.
        (self.repo / ".git/info/exclude").write_text(".codex-audit/\n")
        (self.repo / "source.py").write_text("synthetic source\n")
        if os.name != "nt":
            (self.repo / "line\nbreak.py").write_text("lineage path fixture\n")
        first = self.build()
        _, previous, _ = self.response(first, 1, "BLOCKED")
        second = self.build(2, previous)
        response, artifact, subject = self.response(second, 2, "PASS")
        self.assertFalse((second / "PREVIOUS_REMEDIATION_MASTER_PROMPT.md").exists())
        self.assertFalse((second / "REMEDIATION_REPORT.md").exists())
        self.assertEqual([], remediation_protocol.check_completion(response, artifact, second / "STATE.json",
                         task_file=self.task, repo=self.repo, expected_subject_id=subject))
    def test_previous_fail_and_notes_still_require_bound_remediation(self):
        # Isolate the prior-verdict branch; full bundle/transport validation is
        # exercised by the BLOCKED pipeline above and existing integrity tests.
        for verdict in ["FAIL", "PASS_WITH_NOTES"]:
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as temp:
                directory = pathlib.Path(temp)
                (directory / "PREVIOUS_AUDIT_RESPONSE.md").write_text(audit_response(verdict))
                response = directory / "AUDIT_RESPONSE.md"
                response.write_text(audit_response("PASS", ()))
                errors = remediation_protocol.check_completion(response, None, directory / "STATE.json")
                self.assertIn("previous remediation round is missing bound Master Prompt or report", errors)



if __name__ == "__main__":
    unittest.main()
