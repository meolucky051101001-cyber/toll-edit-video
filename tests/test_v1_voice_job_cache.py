import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.ai import v1_auto_voice as voice


class VoiceJobCacheTests(unittest.TestCase):
    def seed(self, folder):
        data = {"job_id": "old", "video_hash": "same", "voice_mode": "manual", "dual_voice": False,
                "voice_id": "old-voice", "voice_source": "edge", "voice_param": "old-param",
                "voice_label": "Old", "rule": "manual"}
        (Path(folder) / voice.VOICE_LOCK_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_new_job_uses_new_voice_on_same_video(self):
        with tempfile.TemporaryDirectory() as folder:
            self.seed(folder)
            selected = {"voice_id": "new-voice", "voice_source": "edge", "voice_param": "new-param", "voice_label": "New"}
            with patch.object(voice, "compute_file_sha256", return_value="same"), \
                 patch.object(voice, "resolve_locked_voice", return_value=selected) as choose:
                result = voice.decide_video_voice(folder, [], voice_mode="manual", job_id="new",
                                                 voice_config={"manual_voice": {"id": "new-voice"}})
            self.assertEqual(result["voice_id"], "new-voice")
            self.assertEqual(result["job_id"], "new")
            choose.assert_called_once()

    def test_same_job_resume_keeps_locked_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            self.seed(folder)
            with patch.object(voice, "compute_file_sha256", return_value="same"), \
                 patch.object(voice, "resolve_locked_voice") as choose:
                result = voice.decide_video_voice(folder, [], voice_mode="manual", job_id="old",
                                                 voice_config={"manual_voice": {"id": "global-changed"}})
            self.assertEqual(result["voice_id"], "old-voice")
            choose.assert_not_called()

    def test_same_job_different_content_invalidates_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            self.seed(folder)
            self.assertIsNone(voice.get_locked_voice(folder, expected_job_id="old", expected_video_hash="changed"))
