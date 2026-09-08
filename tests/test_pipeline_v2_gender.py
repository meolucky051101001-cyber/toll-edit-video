import unittest
from datetime import timedelta
from pathlib import Path
import tempfile
import numpy as np

from backend.pipeline_v2.segments import RuntimeSegment, segment_to_dict, segment_from_dict
from backend.pipeline_v2.gender_detector import detect_segment_gender, enrich_segments_with_gender

class TestPipelineV2Gender(unittest.TestCase):
    def test_segment_gender_serialization(self):
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0.0),
            end=timedelta(seconds=2.0),
            content="Xin chào",
            gender="male",
        )
        d = segment_to_dict(seg)
        self.assertEqual(d["gender"], "male")
        
        restored = segment_from_dict(d)
        self.assertEqual(restored.gender, "male")

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

if __name__ == "__main__":
    unittest.main()
