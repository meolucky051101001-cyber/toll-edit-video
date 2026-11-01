import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class FrozenConfigTests(unittest.TestCase):
    def test_missing_snapshot_store_remains_optional(self):
        from backend import job_config_service as service

        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(service.get_frozen_config("fixture", workspace_path=folder))

    def test_corrupt_and_invalid_snapshot_stores_are_never_empty_defaults(self):
        from backend import job_config_service as service
        from backend.v1_integrity_errors import FrozenConfigError

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control" / "job_frozen_configs.json"
            path.parent.mkdir()
            for content in ('{broken', '[]', '{"fixture":null}', '{"fixture":{"effective_config":[]}}'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(FrozenConfigError):
                        service.get_frozen_config("fixture", workspace_path=folder)
                    with self.assertRaises(FrozenConfigError):
                        service.list_frozen_configs(workspace_path=folder)
                    self.assertEqual(path.read_text(), content)

    def test_corrupt_scoped_snapshot_cannot_fall_back_to_global_settings(self):
        from backend import job_config_service as service
        from backend.v1_integrity_errors import FrozenConfigError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "control").mkdir()
            (root / "control" / "job_frozen_configs.json").write_text('{broken')
            global_store = root / "global.json"
            global_store.write_text(json.dumps({"fixture": {"effective_config": {"voice_id": "wrong-global-voice"}}}))
            with patch.object(service, "FROZEN_FILE", global_store), self.assertRaises(FrozenConfigError):
                service.get_frozen_config(str(root / "fixture" / "original.wav"))

    def test_snapshot_failure_stops_separator_before_gpu_work(self):
        from backend import video_utils
        import audio_settings
        import job_config_service
        from backend.v1_integrity_errors import FrozenConfigError

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.wav"
            source.write_bytes(b"fixture")
            with patch.object(audio_settings, "get_audio_settings", return_value={}), \
                 patch.object(job_config_service, "get_frozen_config", side_effect=OSError("fixture")), \
                 patch.object(video_utils.subprocess, "Popen") as process:
                with self.assertRaises(FrozenConfigError):
                    video_utils.separate_vocals_demucs(str(source), str(Path(folder) / "out"))
            process.assert_not_called()

    def test_integrity_failures_are_not_network_retried_even_when_wrapped(self):
        from backend.v1_retry_policy import should_retry_job, is_transient_network_error
        from backend.v1_integrity_errors import FrozenConfigError, GlossaryConfigError, AudioDurationError, SegmentCommitError

        for kind in (FrozenConfigError, GlossaryConfigError, AudioDurationError, SegmentCommitError):
            with self.subTest(error=kind.__name__):
                error = RuntimeError("worker timeout")
                error.__cause__ = kind("fixture")
                self.assertFalse(should_retry_job(error))
                self.assertFalse(is_transient_network_error(error))


class SegmentReadDurationTests(unittest.TestCase):
    def test_decoder_error_cannot_return_a_partial_segment_list(self):
        from backend import segment_editor_service as editor
        from backend.v1_integrity_errors import AudioDurationError

        for layout in ("v1", "v2"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                job = root / "job"
                drafts = root / "drafts"
                drafts.mkdir()
                if layout == "v1":
                    dub = job / "dubbing"
                    dub.mkdir(parents=True)
                    (job / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nfirst\n\n2\n00:00:01,000 --> 00:00:02,000\nsecond\n")
                else:
                    dub = job / "tts"
                    dub.mkdir(parents=True)
                    (dub / "segments.json").write_text(json.dumps({"runtime_segments": [
                        {"index": i, "start": i-1, "end": i, "content": "fixture"} for i in (1, 2)
                    ]}))
                for i in (1, 2):
                    (dub / f"{i}.mp3").write_bytes(b"fixture")
                with patch.object(editor, "find_job_workspace", return_value=job), \
                     patch.object(editor, "get_segment_drafts_dir", return_value=drafts), \
                     patch.object(editor, "resolve_job_voice", return_value=("fixture", "fixture")), \
                     patch.object(editor, "get_audio_duration_seconds", side_effect=[1.0, AudioDurationError("fixture")]):
                    with self.assertRaises(AudioDurationError):
                        editor.get_job_segments("job")


class GlossaryTests(unittest.TestCase):
    def test_glossary_path_that_is_a_directory_is_not_treated_as_missing(self):
        from backend.ai import translation
        from backend.v1_integrity_errors import GlossaryConfigError

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(translation, "_glossary_paths", return_value=[Path(folder)]):
                with self.assertRaises(GlossaryConfigError):
                    translation.load_custom_glossary()

    def test_missing_primary_uses_valid_legacy_glossary(self):
        from backend.ai import translation

        with tempfile.TemporaryDirectory() as folder:
            primary, legacy = Path(folder) / "missing.json", Path(folder) / "legacy.json"
            legacy.write_text(json.dumps({"entries": {"term": "value"}}))
            with patch.object(translation, "_glossary_paths", return_value=[primary, legacy]):
                self.assertEqual(translation.load_custom_glossary(), {"term": "value"})

    def test_existing_bad_glossary_cannot_be_bypassed_by_provider_fallback(self):
        from backend.ai import translation
        from backend.v1_integrity_errors import GlossaryConfigError

        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "glossary.json"
            for content in ('{broken', '[]', '{"entries":[]}'):
                with self.subTest(content=content):
                    primary.write_text(content)
                    with patch.object(translation, "_glossary_paths", return_value=[primary]), \
                         patch.object(translation, "translate_with_gemini") as provider, \
                         patch.object(translation, "translate_with_g4f") as fallback:
                        with self.assertRaises(GlossaryConfigError):
                            translation.translate_subtitles([SimpleNamespace(content="fixture")], api_key="fixture")
                    provider.assert_not_called()
                    fallback.assert_not_called()


class MeasuredDurationTests(unittest.TestCase):
    def test_probe_error_or_nan_uses_real_decoded_duration(self):
        from backend import segment_editor_service as editor

        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "fixture.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setnchannels(2)
                stream.setsampwidth(2)
                stream.setframerate(48000)
                stream.writeframes(b"\0" * 96000)
            for probe in (OSError("fixture"), SimpleNamespace(returncode=0, stdout="nan")):
                with self.subTest(probe=type(probe).__name__):
                    failure = {"side_effect": probe} if isinstance(probe, Exception) else {"return_value": probe}
                    with patch.object(editor, "_AUDIO_DURATION_CACHE", {}), \
                         patch.object(editor.subprocess, "run", **failure):
                        duration = editor.get_audio_duration_seconds(audio)
                        self.assertEqual(duration, 0.5)
                        self.assertEqual(editor.get_audio_duration_seconds(audio), duration)
                    self.assertNotEqual(duration, round(audio.stat().st_size / 16000.0, 2))
