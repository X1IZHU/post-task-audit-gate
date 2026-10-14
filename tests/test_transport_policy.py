from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import audit_integrity
import build_handoff
from transport_policy import (
    EXTERNAL_BROWSER,
    PRIMARY_BROWSER_TRANSPORT,
    STOP,
    advance_state,
    apply_transport_decision,
    attachment_ready,
    choose_transport,
    new_transport_state,
    response_and_artifact_ready,
    record_verified_download,
    send_allowed,
    validate_bundle_path,
    validate_download,
    validate_transport_state,
)


class TransportPolicyTests(unittest.TestCase):
    def _build_fixture(self, repo: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / ".codex-audit").mkdir()
        task = {"task_id": "transport-cli", "original_task": "local transport", "user_task_transcript": ""}
        task["task_spec_hash"] = audit_integrity.task_spec_manifest(task)["task_spec_hash"]
        (repo / ".codex-audit" / "task.json").write_text(json.dumps(task), encoding="utf-8")
        (repo / "impl.md").write_text("implemented", encoding="utf-8")
        (repo / "tests.md").write_text("tested", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "build_handoff.py"), "--repo", str(repo), "--round", "1",
             "--implementation-report", str(repo / "impl.md"), "--test-results", str(repo / "tests.md")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        round_dir = repo / ".codex-audit" / "sessions" / "transport-cli" / "round-01"
        state_path = round_dir / "STATE.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return state_path, pathlib.Path(state["bundle_path"])

    def test_in_app_browser_is_primary(self) -> None:
        decision = choose_transport(True, True)
        self.assertEqual(PRIMARY_BROWSER_TRANSPORT, decision["transport"])
        self.assertFalse(decision["external_browser_used"])

    def test_unavailable_in_app_browser_stops_without_silent_fallback(self) -> None:
        decision = choose_transport(False)
        self.assertEqual(STOP, decision["transport"])
        self.assertEqual("IN_APP_BROWSER_UNAVAILABLE", decision["reason"])
        fallback = choose_transport(False, True)
        self.assertEqual(EXTERNAL_BROWSER, fallback["transport"])
        self.assertTrue(fallback["user_approved"])

    def test_transport_state_records_explicit_fallback(self) -> None:
        state = new_transport_state("/private/tmp/AUDIT_BUNDLE_task_R01.zip", "task", 1)
        self.assertIn("REUSE_SAME_IN_APP_SESSION", state["conversation_policy"])
        updated = apply_transport_decision(state, choose_transport(False, True))
        self.assertEqual(EXTERNAL_BROWSER, updated["browser_transport"])
        self.assertTrue(updated["external_browser_used"])
        self.assertTrue(updated["user_approved"])
        self.assertTrue(updated["transport_fallback_reason"])

    def test_external_fallback_requires_reason_and_consistent_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            state_path, bundle = self._build_fixture(repo)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update({
                "browser_transport": EXTERNAL_BROWSER,
                "external_browser_used": True,
                "user_approved": True,
                "transport_fallback_reason": "",
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")
            errors = validate_transport_state(
                str(state_path), expected_handoff_id="transport-cli", expected_round=1,
                expected_bundle_path=str(bundle), expected_bundle_filename=bundle.name,
            )
            self.assertTrue(any("fallback reason" in item for item in errors))

    def test_previous_round_completion_metadata_is_complete_and_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            state_path, _ = self._build_fixture(repo)
            round_dir = state_path.parent
            complete_path = round_dir / "BUNDLE_COMPLETE.json"
            complete = json.loads(complete_path.read_text(encoding="utf-8"))
            complete["state"] = "HANDOFF_READY"
            complete["file_count"] = 0
            complete_path.write_text(json.dumps(complete), encoding="utf-8")
            errors = build_handoff.previous_round_errors(round_dir, "transport-cli", 1, "INITIAL")
            self.assertTrue(any("state" in item or "file_count" in item for item in errors))

    def test_summary_only_previous_fail_cannot_erase_authoritative_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            previous = root / "previous.md"
            previous.write_text(
                "AUDIT_PROTOCOL_VERSION: 1\nHANDOFF_ID: task\nROUND: 1\n"
                "AUDIT_TYPE: INITIAL\nAUDIT_SUBJECT_ID: subject\nVERDICT: FAIL\n",
                encoding="utf-8",
            )
            report = root / "report.md"
            report.write_text("summary", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "canonical actionable findings"):
                build_handoff.validate_round_inputs(2, str(previous), str(report))

    def test_attachment_pending_and_wrong_filename_block_send(self) -> None:
        expected = "AUDIT_BUNDLE_task_R01.zip"
        self.assertEqual((False, "ATTACHMENT_STILL_UPLOADING"), attachment_ready([expected], expected, True))
        self.assertEqual((False, "EXPECTED_BUNDLE_NOT_EXACTLY_ATTACHED"), attachment_ready(["R00.zip"], expected))
        self.assertFalse(send_allowed("FILE_ATTACHED", True, [expected], expected, True))
        self.assertTrue(send_allowed("FILE_ATTACHED", True, [expected], expected))
        self.assertFalse(send_allowed("FILE_UPLOAD_IN_PROGRESS", True, [expected], expected))

    def test_state_machine_does_not_skip_attachment_gate(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid transport transition"):
            advance_state("CHATGPT_READY", "SUBMITTING_REQUEST")
        self.assertEqual("FILE_ATTACHED", advance_state("FILE_UPLOAD_IN_PROGRESS", "FILE_ATTACHED"))

    def test_response_and_artifact_gate_requires_both_parts(self) -> None:
        self.assertFalse(response_and_artifact_ready(True, False))
        self.assertFalse(response_and_artifact_ready(False, True))
        with self.assertRaisesRegex(ValueError, "both be ready"):
            advance_state(
                "WAITING_FOR_AUDIT_ARTIFACT",
                "RESPONSE_AND_ARTIFACT_READY",
                final_response_ready=True,
                downloadable_artifact_ready=False,
            )
        self.assertEqual(
            "RESPONSE_AND_ARTIFACT_READY",
            advance_state(
                "WAITING_FOR_AUDIT_ARTIFACT",
                "RESPONSE_AND_ARTIFACT_READY",
                final_response_ready=True,
                downloadable_artifact_ready=True,
            ),
        )

    def test_exact_bundle_and_audit_artifact_download_paths_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            expected = root / "AUDIT_BUNDLE_task_R01.zip"
            expected.write_bytes(b"zip fixture")
            self.assertEqual([], validate_bundle_path(str(expected), str(expected), expected_filename=expected.name, bundle_complete=True, hashes_valid=True))
            self.assertTrue(validate_bundle_path(str(root / "AUDIT_BUNDLE_task_R00.zip"), str(expected), expected_filename=expected.name, bundle_complete=True, hashes_valid=True))
            artifact = root / "AUDIT_RESPONSE_task_R01.md"
            artifact.write_text("HANDOFF_ID: task\nROUND: 1\nVERDICT: PASS\n", encoding="utf-8")
            self.assertTrue(validate_download(str(artifact), artifact.name, expected_handoff_id="task", expected_round=1))
            self.assertTrue(validate_download(str(artifact), artifact.name, expected_handoff_id="task", expected_round=1, expected_audit_type="INITIAL", expected_subject_id="subject"))
            wrong_identity = root / "AUDIT_RESPONSE_other_R01.md"
            wrong_identity.write_text("HANDOFF_ID: task\nROUND: 1\n", encoding="utf-8")
            self.assertTrue(validate_download(str(wrong_identity), wrong_identity.name, expected_handoff_id="task", expected_round=1))
            wrong_round = root / "AUDIT_RESPONSE_task_R00.md"
            wrong_round.write_text("HANDOFF_ID: task\nROUND: 0\n", encoding="utf-8")
            self.assertTrue(validate_download(str(wrong_round), artifact.name, expected_handoff_id="task", expected_round=1))
            empty = root / "AUDIT_RESPONSE_task_R01.empty.md"
            empty.write_bytes(b"")
            self.assertTrue(validate_download(str(empty), artifact.name, expected_handoff_id="task", expected_round=1))

    def test_builder_cli_records_exact_bundle_state_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / ".codex-audit").mkdir()
            task = {"task_id": "transport-cli", "original_task": "local transport", "user_task_transcript": ""}
            task["task_spec_hash"] = audit_integrity.task_spec_manifest(task)["task_spec_hash"]
            (repo / ".codex-audit" / "task.json").write_text(json.dumps(task), encoding="utf-8")
            (repo / "impl.md").write_text("implemented", encoding="utf-8")
            (repo / "tests.md").write_text("tested", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "build_handoff.py"), "--repo", str(repo), "--round", "1", "--implementation-report", str(repo / "impl.md"), "--test-results", str(repo / "tests.md")],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            round_dir = repo / ".codex-audit" / "sessions" / "transport-cli" / "round-01"
            state_path = round_dir / "STATE.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(PRIMARY_BROWSER_TRANSPORT, state["browser_transport"])
            bundle = pathlib.Path(state["bundle_path"])
            self.assertEqual(bundle.name, state["expected_bundle_filename"])
            preflight = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "transport_policy.py"), str(state_path),
                 "--expected-handoff-id", "transport-cli", "--expected-round", "1",
                 "--expected-bundle-path", str(bundle), "--expected-bundle-filename", bundle.name],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, preflight.returncode, preflight.stderr)
            self.assertEqual("TRANSPORT_READY", preflight.stdout.strip())
            artifact = repo / "AUDIT_RESPONSE_transport-cli_R01.md"
            subject = json.loads((round_dir / "AUDIT_SUBJECT.json").read_text(encoding="utf-8"))["audit_subject_id"]
            artifact.write_text(
                "AUDIT_PROTOCOL_VERSION: 1\nHANDOFF_ID: transport-cli\nROUND: 1\n"
                "AUDIT_TYPE: INITIAL\nAUDIT_SUBJECT_ID: " + subject + "\nVERDICT: PASS\n",
                encoding="utf-8",
            )
            capture_check = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "transport_policy.py"), str(state_path),
                 "--expected-handoff-id", "transport-cli", "--expected-round", "1",
                 "--expected-bundle-path", str(bundle), "--expected-bundle-filename", bundle.name,
                 "--download-path", str(artifact), "--expected-download-audit-type", "INITIAL",
                 "--expected-download-subject-id", subject],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, capture_check.returncode, capture_check.stderr)
            self.assertEqual("TRANSPORT_READY", capture_check.stdout.strip())
            moved = bundle.with_name(bundle.name + ".held")
            bundle.rename(moved)
            try:
                blocked = validate_transport_state(
                    str(state_path), expected_handoff_id="transport-cli", expected_round=1,
                    expected_bundle_path=str(bundle), expected_bundle_filename=bundle.name,
                )
                self.assertTrue(any("does not exist" in item for item in blocked))
            finally:
                moved.rename(bundle)

    def test_cli_rejects_valid_bundle_for_wrong_task_or_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            state_path, bundle = self._build_fixture(repo)
            common = [sys.executable, str(SCRIPT_DIR / "transport_policy.py"), str(state_path),
                      "--expected-bundle-path", str(bundle), "--expected-bundle-filename", bundle.name]
            wrong_task = subprocess.run(common + ["--expected-handoff-id", "other", "--expected-round", "1"], capture_output=True, text=True, check=False)
            wrong_round = subprocess.run(common + ["--expected-handoff-id", "transport-cli", "--expected-round", "2"], capture_output=True, text=True, check=False)
            self.assertNotEqual(0, wrong_task.returncode)
            self.assertIn("identity mismatch", wrong_task.stdout)
            self.assertNotEqual(0, wrong_round.returncode)
            self.assertIn("round mismatch", wrong_round.stdout)

    def test_renderer_uses_exact_filename_without_local_path(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "render_browser_prompt.py"), "--task-id", "task", "--round", "1", "--audit-type", "INITIAL", "--subject-id", "subject", "--bundle-filename", "AUDIT_BUNDLE_task_R01.zip"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("EXPECTED_BUNDLE_FILENAME: AUDIT_BUNDLE_task_R01.zip", result.stdout)
        self.assertNotIn("EXPECTED_BUNDLE_PATH", result.stdout)

    def test_transport_state_schema_covers_recorded_fields(self) -> None:
        schema = json.loads((SCRIPT_DIR.parent / "schema" / "transport-state.schema.json").read_text(encoding="utf-8"))
        required = set(schema["required"])
        self.assertTrue({"browser_transport", "bundle_path", "upload_verified", "download_verified"} <= required)
        self.assertEqual("CODEX_IN_APP_BROWSER", schema["properties"]["browser_transport"]["enum"][0])

    def test_instructions_have_no_system_browser_or_standalone_finder_default(self) -> None:
        root = SCRIPT_DIR.parent
        files = [path for path in (root / "scripts").glob("*.py") if not path.name.startswith("test_")]
        from transport_policy import forbidden_launches
        def scan_sources(paths: list[pathlib.Path]) -> list[pathlib.Path]:
            return [path for path in paths if forbidden_launches(path)]

        self.assertEqual([], scan_sources(files))
        forbidden_sources = (
            "import os; os.system('open https://chatgpt.com')",
            "import os; os.system('open ~/Downloads')",
            "import os; os.system('open -R /tmp/file.zip')",
            "import os; os.system('start https://chatgpt.com')",
            'import subprocess; subprocess.run(["open", "https://chatgpt.com"])',
            "import webbrowser; webbrowser.open('https://chatgpt.com')",
            "import os; os.system('xdg-open https://chatgpt.com')",
            "import os; os.system('open -a Safari')",
            "import os; os.system('open -a Google Chrome')",
            "import os; os.system('activate Finder')",
            "import os; os.system('reveal Finder')",
            "import os; os.system('osascript -e \'tell application \"Safari\" to activate\'')",
            "import webbrowser; webbrowser.get().open('https://chatgpt.com')",
            "import os; os.system('Start-Process https://chatgpt.com')",
            'import subprocess; subprocess.check_call(["open", "https://chatgpt.com"])',
            'import subprocess; subprocess.check_output(["open", "https://chatgpt.com"])',
            "import os; os.system('open https://chatgpt.com')",
            "import os; os.system('osascript -e \'tell application\\n\"Safari\" to activate\'')",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "fake.py"
            for command in forbidden_sources:
                source.write_text(command, encoding="utf-8")
                self.assertEqual([source], scan_sources([source]), command)
        for path in files:
            self.assertIsNone(re.search(r"\b(?:time\.)?sleep\s*\(", path.read_text(encoding="utf-8")), path)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("PRIMARY_BROWSER_TRANSPORT = CODEX_IN_APP_BROWSER", skill)
        self.assertIn("IN_APP_BROWSER_UNAVAILABLE", skill)
        self.assertIn("FILE_ATTACHED", skill)

    def test_structural_detector_catches_aliases_paths_and_windows_launchers(self) -> None:
        from transport_policy import forbidden_launches
        fixtures = (
            "from subprocess import run\nrun([\"open\", \"https://chatgpt.com\"])\n",
            "import subprocess as sp\nsp.run([\"/usr/bin/open\", \"https://chatgpt.com\"])\n",
            "import subprocess\nsubprocess.run([\"cmd\", \"/c\", \"start\", \"https://chatgpt.com\"])\n",
            "import subprocess\nsubprocess.run([\"powershell\", \"-Command\", \"Start-Process\", \"https://chatgpt.com\"])\n",
            "import webbrowser; webbrowser.open_new(\"https://chatgpt.com\")\n",
            "from webbrowser import open_new_tab; open_new_tab(\"https://chatgpt.com\")\n",
            "import subprocess; subprocess.run([\"osascript\", \"-e\", \"tell application 'Safari' to activate\"])\n",
            "import subprocess; subprocess.run([\"/usr/bin/osascript\", \"-e\", \"tell application 'Finder' to activate\"])\n",
            "import subprocess; command = [\"open\", \"https://chatgpt.com\"]; subprocess.run(command)\n",
            "import subprocess; subprocess.run([\"sh\", \"-c\", \"open https://chatgpt.com\"])\n",
            "import subprocess; subprocess.run([\"explorer.exe\", \"https://chatgpt.com\"])\n",
            "import subprocess; subprocess.run([\"gio\", \"open\", \"https://chatgpt.com\"])\n",
            "import subprocess; subprocess.run([\"bash\", \"-lc\", \"open https://chatgpt.com\"])\n",
            "from webbrowser import get; get().open(\"https://chatgpt.com\")\n",
            "import subprocess; subprocess.run([\"python3\", \"-m\", \"webbrowser\", \"https://chatgpt.com\"])\n",
            "import subprocess; command = [\"open\", \"https://chatgpt.com\"]; subprocess.run(command); command = [\"git\", \"status\"]; subprocess.run(command)\n",
            "import subprocess; shell = \"open https://chatgpt.com\"; subprocess.run([\"bash\", \"-lc\", shell])\n",
            "import os; command = \"open https://chatgpt.com\"; os.system(command)\n",
            "import subprocess; script = \"tell application 'Safari' to activate\"; subprocess.run([\"osascript\", \"-e\", script])\n",
            "import subprocess, sys; subprocess.run([sys.executable, \"-m\", \"webbrowser\", \"https://chatgpt.com\"])\n",
            "import subprocess; payload = \"echo ok\"; payload = make_payload(); subprocess.run([\"bash\", \"-lc\", payload])\n",
            "import subprocess; script = \"echo ok\"; script = make_script(); subprocess.run([\"osascript\", \"-e\", script])\n",
            "import os; command = \"echo ok\"; command = make_command(); os.system(command)\n",
            "import os; os.system(f\"open {url}\")\n",
            "import webbrowser; browser = webbrowser.get(); browser.open(\"https://chatgpt.com\")\n",
            "import webbrowser; launch = webbrowser.open; launch(\"https://chatgpt.com\")\n",
            "import subprocess; runner = subprocess.run; runner([\"open\", \"https://chatgpt.com\"])\n",
            "import os; command = \"open https://chatgpt.com\"; runner = os.system; runner(command)\n",
            "import os; command = \"open https://chatgpt.com\"; system = os.system; system(command)\n",
            "import os; os.system(\"explorer.exe https://chatgpt.com\")\n",
            "import os; os.system(\"python3 -m webbrowser https://chatgpt.com\")\n",
            "import subprocess; subprocess.run(command, shell=True)\n",
            "import subprocess; command = make_command(); subprocess.run(command, shell=True)\n",
            "from webbrowser import get as get_browser; get_browser_alias = get_browser; browser = get_browser_alias(); browser.open(url)\n",
            "import webbrowser; browser = webbrowser.get(); launch = browser.open; launch(url)\n",
            "import webbrowser; launch = webbrowser.open; alias = launch; alias(url)\n",
            "from webbrowser import open as launch; alias = launch; alias(url)\n",
            "import subprocess; runner = subprocess.run; alias = runner; alias([\"open\", url])\n",
            "from subprocess import run as runner; alias = runner; alias([\"open\", url])\n",
            "import subprocess; subprocess.run([\"python3.12\", \"-m\", \"webbrowser\", url])\n",
            "import subprocess; script_file = \"/tmp/payload.scpt\"; subprocess.run([\"osascript\", \"-e\", script_file])\n",
            "import subprocess; script_file = \"/tmp/payload.scpt\"; subprocess.run([\"osascript\", script_file])\n",
            "import subprocess\nif flag:\n    command = [\"open\", url]\nelse:\n    command = [\"git\", \"status\"]\nsubprocess.run(command)\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, source in enumerate(fixtures):
                path = pathlib.Path(directory) / f"fixture{index}.py"
                path.write_text(source, encoding="utf-8")
                self.assertTrue(forbidden_launches(path), source)

    def test_structural_detector_catches_keyword_env_and_windows_equivalents(self) -> None:
        from transport_policy import forbidden_launches
        fixtures = (
            'import subprocess; subprocess.run(args=["open", url])\n',
            'import subprocess; subprocess.Popen(args=["open", url])\n',
            'import webbrowser; webbrowser.open(url=url)\n',
            'import webbrowser; browser = webbrowser.get(); browser.open(url=url)\n',
            'import subprocess; shell = True; subprocess.run(command, shell=shell)\n',
            'import subprocess; subprocess.run([r"C:\\Windows\\explorer.exe", url])\n',
            'import subprocess; subprocess.run([r"C:\\Windows\\System32\\cmd.exe", "/c", "start", url])\n',
            'import subprocess; subprocess.run(["powershell.exe", "-Command", "Start-Process", url])\n',
            'import subprocess; subprocess.run([r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-Command", "Start-Process", url])\n',
            'import subprocess; subprocess.run(["PwSh.ExE", "-Command", "Start-Process", url])\n',
            'import subprocess; subprocess.run(["powershell.exe", "-Command", "Start-Process https://example.test"])\n',
            'import subprocess; subprocess.run(["pwsh", "-Command", "Start-Process https://example.test"])\n',
            'import subprocess; subprocess.run(["/usr/bin/env", "open", url])\n',
            'import os; os.popen("open https://chatgpt.com")\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, source in enumerate(fixtures):
                path = pathlib.Path(directory) / f"keyword{index}.py"
                path.write_text(source, encoding="utf-8")
                self.assertTrue(forbidden_launches(path), source)

    def test_verified_download_is_recorded_and_exact_previous_path_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            state_path, _ = self._build_fixture(repo)
            round_dir = state_path.parent
            subject = json.loads((round_dir / "AUDIT_SUBJECT.json").read_text(encoding="utf-8"))["audit_subject_id"]
            response = repo / "AUDIT_RESPONSE_transport-cli_R01.md"
            response.write_text(
                "AUDIT_PROTOCOL_VERSION: 1\nHANDOFF_ID: transport-cli\nROUND: 1\n"
                "AUDIT_TYPE: INITIAL\nAUDIT_SUBJECT_ID: " + subject + "\nVERDICT: FAIL\n"
                "## Findings\n### PTA-001\n", encoding="utf-8"
            )
            self.assertEqual([], record_verified_download(
                str(state_path), str(response), expected_handoff_id="transport-cli",
                expected_round=1, expected_audit_type="INITIAL", expected_subject_id=subject,
            ))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            provenance = json.loads((state_path.parent / "DOWNLOAD_PROVENANCE.json").read_text(encoding="utf-8"))
            self.assertTrue(provenance["download_verified"])
            self.assertEqual(audit_integrity.sha256_file(response), provenance["download_sha256"])
            replacement = repo / "replacement.md"
            replacement.write_text(response.read_text(encoding="utf-8"), encoding="utf-8")
            errors = build_handoff.previous_round_errors(round_dir, "transport-cli", 1, "INITIAL", replacement)
            self.assertTrue(any("exact verified downloaded" in item for item in errors))

    def test_test_provenance_binds_successful_runs_to_candidate_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            output = repo / "run.txt"
            output.write_text("Ran 1 test in 0.001s\nOK\n", encoding="utf-8")
            fingerprint = "a" * 64
            record = {
                "schema_version": 1,
                "candidate_worktree_fingerprint": fingerprint,
                "runs": [{
                    "name": "full",
                    "command": ["python3", "-m", "unittest"],
                    "cwd": str(repo),
                    "exit_code": 0,
                    "pre_run_worktree_fingerprint": fingerprint,
                    "post_run_worktree_fingerprint": fingerprint,
                    "output_path": "run.txt",
                }],
            }
            path = repo / "TEST_PROVENANCE.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual([], build_handoff.test_provenance_errors(path, repo, fingerprint))
            record["runs"][0]["post_run_worktree_fingerprint"] = "b" * 64
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertTrue(any("post_run_worktree_fingerprint" in item for item in build_handoff.test_provenance_errors(path, repo, fingerprint)))


if __name__ == "__main__":
    unittest.main()
