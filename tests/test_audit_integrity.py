from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
import sys

sys.path.insert(0, str(SCRIPT_DIR))

import audit_integrity as integrity


class TaskSpecIntegrityTests(unittest.TestCase):
    def base_task(self) -> dict[str, object]:
        return {
            "original_task": "Implement the bounded audit gate and verify it.",
            "user_task_transcript": "The user requires exact local evidence.",
            "task_id": "task-a",
            "round": 4,
            "audit_type": "REMEDIATION_VERIFICATION",
            "browser_prompt": "transport prompt A",
            "artifact_filename": "AUDIT_RESPONSE_task-a_R04.md",
            "max_audit_rounds": 3,
            "project_context_fingerprint": "context-a",
        }

    def test_manifest_records_version_sources_and_final_hash(self) -> None:
        manifest = integrity.task_spec_manifest(self.base_task())
        self.assertEqual(integrity.TASK_SPEC_CANONICALIZATION_VERSION, manifest["canonicalization_version"])
        self.assertEqual(["original_task", "user_task_transcript"], [
            source["logical_id"] for source in manifest["authoritative_input_sources"]
        ])
        self.assertEqual(
            integrity.task_spec_hash(
                self.base_task()["original_task"],  # type: ignore[arg-type]
                self.base_task()["user_task_transcript"],  # type: ignore[arg-type]
            ),
            manifest["task_spec_hash"],
        )
        for source in manifest["authoritative_input_sources"]:
            self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")
            self.assertIsInstance(source["utf8_bytes"], int)

    def test_transport_and_round_metadata_do_not_change_task_hash(self) -> None:
        baseline = integrity.task_spec_manifest(self.base_task())["task_spec_hash"]
        changed = self.base_task()
        changed.update({
            "round": 99,
            "audit_type": "INITIAL",
            "browser_prompt": "a completely different browser transport",
            "artifact_filename": "another-name.md",
            "audit_subject_id": "new-subject",
            "bundle_manifest_hash": "new-manifest",
            "extra_r04_authorization": True,
        })
        self.assertEqual(baseline, integrity.task_spec_manifest(changed)["task_spec_hash"])

    def test_semantic_requirement_amendment_changes_task_hash(self) -> None:
        changed = self.base_task()
        changed["original_task"] = "Implement the bounded audit gate and verify it, including package scripts."
        self.assertNotEqual(
            integrity.task_spec_manifest(self.base_task())["task_spec_hash"],
            integrity.task_spec_manifest(changed)["task_spec_hash"],
        )

    def test_reordered_task_metadata_is_deterministic(self) -> None:
        first = self.base_task()
        second = {key: first[key] for key in reversed(list(first))}
        self.assertEqual(integrity.task_spec_manifest(first), integrity.task_spec_manifest(second))
        with tempfile.TemporaryDirectory() as directory:
            task_file = pathlib.Path(directory) / "task.json"
            task_file.write_text(json.dumps(second, indent=2), encoding="utf-8")
            self.assertEqual(
                integrity.task_spec_manifest(first)["task_spec_hash"],
                integrity.task_spec_manifest_from_file(task_file)["task_spec_hash"],
            )

    def test_builder_and_staleness_use_the_same_semantic_inputs(self) -> None:
        task = self.base_task()
        with tempfile.TemporaryDirectory() as directory:
            task_file = pathlib.Path(directory) / "task.json"
            task_file.write_text(json.dumps(task, indent=2), encoding="utf-8")
            builder_hash = integrity.task_spec_manifest(task)["task_spec_hash"]
            staleness_hash = integrity.task_spec_manifest_from_file(task_file)["task_spec_hash"]
            self.assertEqual(builder_hash, staleness_hash)
            self.assertNotEqual(
                builder_hash,
                integrity.task_spec_hash(task_file.read_text(encoding="utf-8")),
                "whole task.json bytes must never be the task specification",
            )

    def test_explicit_transcript_override_is_semantic_but_metadata_is_not(self) -> None:
        task = self.base_task()
        with tempfile.TemporaryDirectory() as directory:
            task_file = pathlib.Path(directory) / "task.json"
            transcript_file = pathlib.Path(directory) / "transcript.txt"
            task_file.write_text(json.dumps(task), encoding="utf-8")
            transcript_file.write_text("A real implementation requirement amendment.", encoding="utf-8")
            overridden = integrity.task_spec_manifest_from_file(task_file, transcript_file)
            self.assertNotEqual(overridden["task_spec_hash"], integrity.task_spec_manifest(task)["task_spec_hash"])

    def test_staleness_helper_accepts_builder_hash_for_identical_task(self) -> None:
        task = self.base_task()
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory) / "repo"
            repo.mkdir()
            task_file = repo / "task.json"
            task_file.write_text(json.dumps(task, indent=2), encoding="utf-8")
            context_version, context_hash, _ = integrity.context_identity(repo)
            complete = pathlib.Path(directory) / "BUNDLE_COMPLETE.json"
            complete.write_text(json.dumps({
                "task_spec_hash": integrity.task_spec_manifest(task)["task_spec_hash"],
                "worktree_fingerprint": integrity.worktree_fingerprint(repo),
                "project_context_version": context_version,
                "project_context_fingerprint": context_hash,
            }), encoding="utf-8")
            self.assertEqual([], integrity.check_staleness(complete, repo, task_file, None))


if __name__ == "__main__":
    unittest.main()
