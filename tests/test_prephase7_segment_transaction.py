import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class FileSetCommitTests(unittest.TestCase):
    def test_failure_after_replace_restores_attempted_destination(self):
        from backend import segment_artifacts as commit
        from backend.v1_integrity_errors import SegmentCommitError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            destination, staged = root / "audio.mp3", root / "staged"
            destination.write_bytes(b"old audio")
            staged.write_bytes(b"new audio")
            real_copy = commit.atomic_copy_file

            def inject(source, target):
                result = real_copy(source, target)
                if Path(source) == staged:
                    raise OSError("injected failure after replace")
                return result

            with patch.object(commit, "atomic_copy_file", side_effect=inject), self.assertRaises(SegmentCommitError):
                commit.commit_segment_files({destination: staged}, root / "rollback")
            self.assertEqual(destination.read_bytes(), b"old audio")

    def test_failed_second_or_third_write_restores_all_completed_replaces(self):
        from backend import segment_artifacts as commit
        from backend.v1_integrity_errors import SegmentCommitError

        for failing_index in (1, 2):
            with self.subTest(failing_index=failing_index), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                destinations = [root / "audio.mp3", root / "translated.srt", root / "draft.json"]
                staged = [root / f"staged-{i}" for i in range(3)]
                for i, (destination, source) in enumerate(zip(destinations, staged)):
                    destination.write_bytes(f"old-{i}".encode())
                    source.write_bytes(f"new-{i}".encode())
                real_copy = commit.atomic_copy_file

                def inject(source, destination):
                    if Path(source) == staged[failing_index]:
                        raise OSError("injected write failure")
                    return real_copy(source, destination)

                with patch.object(commit, "atomic_copy_file", side_effect=inject):
                    with self.assertRaises(SegmentCommitError):
                        commit.commit_segment_files(dict(zip(destinations, staged)), root / "rollback")
                self.assertEqual([p.read_bytes() for p in destinations], [b"old-0", b"old-1", b"old-2"])

    def test_new_audio_is_removed_on_failed_commit(self):
        from backend import segment_artifacts as commit
        from backend.v1_integrity_errors import SegmentCommitError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            new_audio, srt = root / "new.mp3", root / "translated.srt"
            audio_stage, srt_stage = root / "audio-stage", root / "srt-stage"
            audio_stage.write_bytes(b"new audio")
            srt_stage.write_text("new srt")
            srt.write_text("old srt")
            real_copy = commit.atomic_copy_file

            def inject(source, destination):
                if Path(source) == srt_stage:
                    raise OSError("injected")
                return real_copy(source, destination)

            with patch.object(commit, "atomic_copy_file", side_effect=inject), self.assertRaises(SegmentCommitError):
                commit.commit_segment_files({new_audio: audio_stage, srt: srt_stage}, root / "rollback")
            self.assertFalse(new_audio.exists())
            self.assertEqual(srt.read_text(), "old srt")

    def test_rollback_failure_keeps_recovery_journal_and_original_audio(self):
        from backend import segment_artifacts as commit
        from backend.v1_integrity_errors import SegmentCommitError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio, srt, staged_audio, staged_srt = [root / name for name in ("audio.mp3", "translated.srt", "new-audio", "new-srt")]
            audio.write_bytes(b"old audio")
            srt.write_bytes(b"old srt")
            staged_audio.write_bytes(b"new audio")
            staged_srt.write_bytes(b"new srt")
            real_copy = commit.atomic_copy_file

            def inject(source, destination):
                if Path(source) == staged_srt or Path(source).name == "0.original":
                    raise OSError("injected unavailable disk")
                return real_copy(source, destination)

            with patch.object(commit, "atomic_copy_file", side_effect=inject):
                with self.assertRaises(SegmentCommitError) as caught:
                    commit.commit_segment_files({audio: staged_audio, srt: staged_srt}, root / "rollback")
            self.assertEqual(caught.exception.recovery_dir, str(root))
            self.assertTrue((root / "rollback/recovery.json").is_file())
            self.assertEqual((root / "rollback/0.original").read_bytes(), b"old audio")


class SegmentRegenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from backend import segment_editor_service as editor
        import voice_preview_service

        self.editor = editor
        self.voice = voice_preview_service
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / "fixture_job"
        self.dub = self.job / "dubbing"
        self.dub.mkdir(parents=True)
        self.drafts = self.root / "drafts"
        self.drafts.mkdir()
        self.primary = self.job / "translated.srt"
        self.secondary = self.job / "pipeline_v2/artifacts/translation/translated.srt"
        self.secondary.parent.mkdir(parents=True)
        self.primary.write_text("old primary SRT")
        self.secondary.write_text("old secondary SRT")
        self.data = {"job_id": "fixture_job", "revision": 1, "segments": [
            {"segment_id": f"seg_{i}", "index": i, "start": (i-1)*2, "end": i*2,
             "duration": 2.0, "translated_text": f"new text {i}", "speed": 1.0}
            for i in (1, 2)
        ]}
        self.draft = self.drafts / "fixture_job.json"
        self.draft.write_text(json.dumps(self.data))
        for i in (1, 2):
            (self.dub / f"{i}.mp3").write_bytes(f"old audio {i}".encode())
        self.preview = self.root / "preview.mp3"
        self.preview.write_bytes(b"new preview audio")
        self.synth = AsyncMock(return_value={"preview_id": "fixture-preview"})
        for target, name, value in [
            (editor, "get_job_segments", lambda name: self.data),
            (editor, "find_job_workspace", lambda name: self.job),
            (editor, "get_segment_drafts_dir", lambda: self.drafts),
            (editor, "get_audio_duration_seconds", lambda path: 1.0),
            (editor, "_IDEMPOTENCY_CACHE", {}),
            (editor, "_segment_lock", asyncio.Lock()),
            (voice_preview_service, "synthesize_voice_preview", self.synth),
            (voice_preview_service, "get_preview_audio_path", lambda key: self.preview),
        ]:
            stub = patch.object(target, name, value)
            stub.start()
            self.addCleanup(stub.stop)
        self.before = self.original_files()

    def original_files(self):
        return {p: p.read_bytes() for p in [self.primary, self.secondary, self.draft, self.dub / "1.mp3", self.dub / "2.mp3"]}

    def assert_original_revision(self):
        self.assertEqual(self.original_files(), self.before)
        self.assertEqual(self.data["revision"], 1)
        self.assertNotIn("audio_path", self.data["segments"][0])
        self.assertEqual(self.editor._IDEMPOTENCY_CACHE, {})
        self.assertEqual(list(self.job.glob(".segment-regenerate-*")), [])

    async def test_required_backup_failure_aborts_before_tts(self):
        from backend.v1_integrity_errors import SegmentCommitError

        with patch.object(self.editor, "atomic_copy_file", side_effect=PermissionError("fixture")):
            with self.assertRaises(SegmentCommitError):
                await self.editor.regenerate_segments("fixture_job", ["seg_1"], idempotency_key="fixture-key")
        self.synth.assert_not_awaited()
        self.assert_original_revision()

    async def test_second_tts_failure_does_not_replace_first_audio(self):
        self.synth.side_effect = [{"preview_id": "fixture-preview"}, RuntimeError("injected TTS failure")]
        with self.assertRaises(RuntimeError):
            await self.editor.regenerate_segments("fixture_job", ["seg_1", "seg_2"], idempotency_key="fixture-key")
        self.assert_original_revision()

    async def test_srt_or_draft_commit_failure_rolls_back_audio_and_all_metadata(self):
        from backend import segment_artifacts as commit
        from backend.v1_integrity_errors import SegmentCommitError

        for fail_destination in [self.primary, self.secondary, self.draft]:
            with self.subTest(destination=fail_destination.name):
                real_copy = commit.atomic_copy_file

                def inject(source, destination):
                    if Path(destination) == fail_destination and Path(source).name in {"translated.srt", "draft.json"}:
                        raise OSError("injected artifact write failure")
                    return real_copy(source, destination)

                with patch.object(commit, "atomic_copy_file", side_effect=inject):
                    with self.assertRaises(SegmentCommitError):
                        await self.editor.regenerate_segments("fixture_job", ["seg_1", "seg_2"], idempotency_key="fixture-key")
                self.assert_original_revision()

    async def test_success_commits_same_revision_to_audio_srt_and_draft_then_caches(self):
        result = await self.editor.regenerate_segments("fixture_job", ["seg_1", "seg_2"], idempotency_key="fixture-key")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["regenerated_count"], 2)
        self.assertEqual(json.loads(self.draft.read_text())["revision"], 2)
        self.assertEqual(self.primary.read_bytes(), self.secondary.read_bytes())
        self.assertIn("new text 1", self.primary.read_text())
        self.assertEqual((self.dub / "1.mp3").read_bytes(), self.preview.read_bytes())
        self.assertEqual((self.dub / "1.mp3.bak").read_bytes(), b"old audio 1")
        repeated = await self.editor.regenerate_segments("fixture_job", ["seg_2", "seg_1"], idempotency_key="fixture-key")
        self.assertEqual(repeated, result)
        self.assertEqual(self.synth.await_count, 2)
        with self.assertRaises(ValueError):
            await self.editor.regenerate_segments("fixture_job", ["seg_1"], idempotency_key="fixture-key")
        self.assertEqual(list(self.job.glob(".segment-regenerate-*")), [])

    async def test_invalid_segment_selection_has_no_side_effects(self):
        with self.assertRaises(ValueError):
            await self.editor.regenerate_segments("fixture_job", ["does-not-exist"])
        self.synth.assert_not_awaited()
        self.assert_original_revision()
