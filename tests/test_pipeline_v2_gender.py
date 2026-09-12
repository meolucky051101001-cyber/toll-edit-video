import unittest
from datetime import timedelta
from pathlib import Path
import tempfile
import numpy as np

from backend.pipeline_v2.segments import RuntimeSegment, segment_to_dict, segment_from_dict
from backend.pipeline_v2.gender_detector import (
    detect_segment_gender,
    enrich_segments_with_gender,
    enrich_segments_with_speaker_and_gender,
)


class TestPipelineV2Gender(unittest.TestCase):
    def test_segment_gender_and_speaker_serialization(self):
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0.0),
            end=timedelta(seconds=2.0),
            content="Xin chào",
            gender="male",
            speaker_id="SPEAKER_00",
        )
        d = segment_to_dict(seg)
        self.assertEqual(d["gender"], "male")
        self.assertEqual(d["speaker_id"], "SPEAKER_00")

        restored = segment_from_dict(d)
        self.assertEqual(restored.gender, "male")
        self.assertEqual(restored.speaker_id, "SPEAKER_00")

    def test_gender_fallback_when_file_missing(self):
        gender = detect_segment_gender(Path("non_existent.wav"), 0.0, 2.0, fallback_gender="female")
        self.assertEqual(gender, "female")

    def test_enrich_segments_default(self):
        segs = [
            RuntimeSegment(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="Câu 1"),
            RuntimeSegment(index=2, start=timedelta(seconds=2.5), end=timedelta(seconds=4.0), content="Câu 2"),
        ]
        enriched = enrich_segments_with_gender(segs, "non_existent.wav", default_gender="female")
        self.assertEqual(len(enriched), 2)
        self.assertEqual(enriched[0].gender, "female")
        self.assertEqual(enriched[1].gender, "female")
        self.assertEqual(enriched[0].speaker_id, "speaker_female")
        self.assertEqual(enriched[1].speaker_id, "speaker_female")

    def test_speaker_voice_map_routing(self):
        import asyncio
        from unittest.mock import patch, AsyncMock
        from backend.pipeline_v2.tts import generate_tts_audio_v2

        segs = [
            RuntimeSegment(
                index=1,
                start=timedelta(seconds=0.0),
                end=timedelta(seconds=2.0),
                content="Giọng nam chính",
                speaker_id="SPEAKER_MALE",
                gender="male",
            ),
            RuntimeSegment(
                index=2,
                start=timedelta(seconds=2.5),
                end=timedelta(seconds=4.5),
                content="Giọng nữ phụ",
                speaker_id="SPEAKER_FEMALE",
                gender="female",
            ),
        ]
        speaker_voice_map = {
            "SPEAKER_MALE": "vi-VN-NamMinhNeural",
            "SPEAKER_FEMALE": "vi-VN-HoaiMyNeural",
        }

        mock_edge = AsyncMock()
        from backend.pipeline_v2.tts import _prepare_legacy_imports
        _prepare_legacy_imports()
        import ai.voice_cloning
        with tempfile.TemporaryDirectory() as td, \
             patch("ai.voice_cloning.generate_tts_edge", mock_edge), \
             patch("backend.pipeline_v2.tts.fit_audio_to_window") as mock_fit:
            # Create dummy raw audio files so fit_audio_to_window works
            for s in segs:
                p = Path(td) / f"{s.index}_raw.mp3"
                p.write_bytes(b"dummy")
            asyncio.run(
                generate_tts_audio_v2(
                    segs,
                    td,
                    voice_source="edge",
                    speaker_voice_map=speaker_voice_map,
                )
            )
            self.assertEqual(mock_edge.call_count, 2)
            called_voices = [call.args[2] for call in mock_edge.call_args_list]
            self.assertIn("vi-VN-NamMinhNeural", called_voices)
            self.assertIn("vi-VN-HoaiMyNeural", called_voices)


if __name__ == "__main__":
    unittest.main()
