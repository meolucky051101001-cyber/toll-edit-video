import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestToolV1Upgrades(unittest.TestCase):
    def test_01_model_policy_and_runtime_module_cached(self):
        """Ensure V1ModelPolicy.from_env and runtime_module_available do not spawn subprocesses repeatedly."""
        from backend.ai import v1_model_policy, v1_model_runtime

        v1_model_policy._SEPARATOR_RUNTIME_CACHE.clear()
        v1_model_runtime._RUNTIME_MODULE_CACHE.clear()

        p1 = v1_model_policy.current_v1_model_policy()
        with patch("subprocess.run") as mock_sub:
            p2 = v1_model_policy.current_v1_model_policy()
            avail = v1_model_runtime.runtime_module_available("paddleocr", p2)
            avail2 = v1_model_runtime.runtime_module_available("paddleocr", p2)
            self.assertEqual(p1.separator_runtime_python, p2.separator_runtime_python)
            self.assertEqual(avail, avail2)
            # When site-packages/paddleocr exists or is cached, subprocess.run is not called on repeat
            self.assertEqual(mock_sub.call_count, 0)

    def test_02_ocr_worker_batch_predict_and_fallback(self):
        """Ensure v1_ocr_worker._run passes resolved_paths list to ocr.predict in a single batch call."""
        from backend.model_workers import v1_ocr_worker

        fake_pred_1 = {
            "res": {
                "rec_polys": [[[10, 10], [100, 10], [100, 40], [10, 40]]],
                "rec_texts": ["第一句"],
                "rec_scores": [0.98],
            }
        }
        fake_pred_2 = {
            "res": {
                "rec_polys": [[[10, 20], [100, 20], [100, 50], [10, 50]]],
                "rec_texts": ["第二句"],
                "rec_scores": [0.96],
            }
        }
        fake_ocr = MagicMock()
        fake_ocr.predict.return_value = [fake_pred_1, fake_pred_2]

        with tempfile.TemporaryDirectory() as tmp:
            img1 = str(Path(tmp) / "1.png")
            img2 = str(Path(tmp) / "2.png")
            Path(img1).write_bytes(b"x")
            Path(img2).write_bytes(b"y")
            with patch.object(v1_ocr_worker, "_model", return_value=fake_ocr):
                res = v1_ocr_worker._run({"images": [img1, img2]})
            # Called ONCE with a list of 2 paths instead of 2 separate calls
            self.assertEqual(fake_ocr.predict.call_count, 1)
            self.assertIsInstance(fake_ocr.predict.call_args.args[0], list)
            self.assertEqual(len(res["images"]), 2)
            self.assertEqual(res["images"][0]["rows"][0]["text"], "第一句")
            self.assertEqual(res["images"][1]["rows"][0]["text"], "第二句")

    def test_03_early_subtitle_band_lock_reduces_frames_in_full_mode(self):
        """Ensure Early Subtitle Band Lock activates after 24 frames on >=16 cues in 'full' mode."""
        import numpy as np
        from backend import ocr_utils

        segs = [
            SimpleNamespace(
                index=i,
                start=timedelta(seconds=(i - 1) * 3.0),
                end=timedelta(seconds=i * 3.0 - 0.2),
                content="这是当时导致伤口感染",
            )
            for i in range(1, 21)
        ]

        class FakeCap:
            def __init__(self):
                self.seeks = 0

            def isOpened(self):
                return True

            def get(self, prop):
                import cv2
                return {
                    cv2.CAP_PROP_FRAME_WIDTH: 720,
                    cv2.CAP_PROP_FRAME_HEIGHT: 1280,
                    cv2.CAP_PROP_FPS: 30.0,
                    cv2.CAP_PROP_FRAME_COUNT: 20 * 90,
                }.get(prop, 0)

            def set(self, prop, val):
                self.seeks += 1
                return True

            def read(self):
                return True, np.zeros((1280, 720, 3), dtype=np.uint8)

            def release(self):
                pass

        fake_cap = FakeCap()

        def fake_readtext(frames):
            out = []
            for f in frames:
                h = f.shape[0]
                # Return matching subtitle row whether full frame (h=1280) or cropped ROI
                y1 = int(h * 0.5) if h < 1000 else 1000
                y2 = y1 + 50
                out.append([([[80, y1], [600, y1], [600, y2], [80, y2]], "这是当时导致伤口感染", 0.96)])
            return out

        with patch.object(ocr_utils.cv2, "VideoCapture", return_value=fake_cap), \
             patch.object(ocr_utils, "_readtext_batch", side_effect=fake_readtext), \
             patch("backend.v1_ocr_proxy._prepare", side_effect=lambda p, *a, **k: Path(p), create=True), \
             patch("backend.v1_ocr_proxy.probe_main_video", return_value=SimpleNamespace(width=720, height=1280, fps=30.0, duration=60.0, index=0, input_map="0:v:0", has_cover=False), create=True):
            ocr_utils.perform_video_ocr("dummy.mp4", srt_segments=segs, ocr_strategy="full", adaptive=False, _source_duration=60.0)

        # Without Early Band Lock: 20 cues * 2 samples = 40 seeks.
        # With Early Band Lock after 24 frames (12 cues): 24 + 8 = 32 seeks (< 40), and all 20 cues located!
        self.assertLess(fake_cap.seeks, 40)
        self.assertTrue(all(getattr(s, "ocr_mask_status", None) == "located" for s in segs))

    def test_04_quality_gate_passes_cleanly_when_ocr_and_ass_verified(self):
        """When all segments have ocr_mask_status=='located' and ASS has BgStyle, overall_status is PASSED."""
        import soundfile as sf
        import numpy as np
        from backend import v1_quality_gate as qc
        from backend.v1_media_streams import MainVideo

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "out.mp4"
            video.write_bytes(b"valid video stream" * 2000)
            dub = tmp / "1.wav"
            bgm = tmp / "mixed.wav"
            sr = 16000
            sf.write(dub, 0.1 * np.sin(2 * np.pi * 200 * np.arange(sr * 2) / sr), sr)
            sf.write(bgm, 0.05 * np.sin(2 * np.pi * 200 * np.arange(sr * 6) / sr), sr)

            ass = tmp / "final.ass"
            ass.write_text(
                "[Script Info]\n" + "; pad\n" * 15 +
                "[V4+ Styles]\nStyle: BgStyle,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
                "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,BgStyle,,0,0,0,,{\\p1}m 0 0 l 100 0 100 40 0 40{\\p0}\n"
                "Dialogue: 1,0:00:01.00,0:00:03.00,TextStyle,,0,0,0,,Xin chào các bạn\n",
                encoding="utf-8",
            )
            seg = SimpleNamespace(
                index=1,
                start=timedelta(seconds=1),
                end=timedelta(seconds=3),
                content="Xin chào các bạn",
                ocr_mask_status="located",
                best_block=SimpleNamespace(x_pct=0.1, max_x_pct=0.9, y_pct=0.8, max_y_pct=0.88),
            )
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.get.side_effect = lambda prop: {7: 180, 5: 30.0, 3: 1080, 4: 1920}.get(prop, 0)
            cap.read.return_value = (True, "frame")

            with patch("v1_media_streams.probe_main_video", return_value=MainVideo(0, 1080, 1920, 30.0, 6.0, 1, 1, False)), \
                 patch("cv2.VideoCapture", return_value=cap):
                rep = qc.run_quality_gate(
                    job_id="clean_pass",
                    final_video_path=video,
                    original_video_path=video,
                    translated_subtitles=[seg],
                    dubbing_audio_files=[{"index": 1, "path": str(dub), "start": 1.0}],
                    mixed_audio_path=bgm,
                    ass_subtitle_path=ass,
                    policy="REPORT_ONLY",
                    workspace_path=tmp,
                )
            self.assertEqual(rep.overall_status, "PASSED")
            self.assertEqual(rep.warning_count, 0)
            self.assertEqual(rep.failure_count, 0)

    def test_05_smart_dialogue_hysteresis_smoothing(self):
        """In dialogue mode, an unknown short cue sandwiched between high-confidence male cues inherits male voice."""
        from backend.ai.v1_auto_voice import decide_video_voice, VOICE_MALE_ID

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="Câu 1 nam nói"),
            SimpleNamespace(index=2, start=timedelta(seconds=2.2), end=timedelta(seconds=2.8), content="Ừ"),
            SimpleNamespace(index=3, start=timedelta(seconds=3.0), end=timedelta(seconds=5.0), content="Câu 3 nam nói tiếp"),
        ]
        mock_evidence = {
            1: {"gender": "male", "confidence": 0.92, "median_f0": 130.0, "reason": "test"},
            2: {"gender": "unknown", "confidence": 0.0, "median_f0": 0.0, "reason": "too_short"},
            3: {"gender": "male", "confidence": 0.90, "median_f0": 132.0, "reason": "test"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch("backend.ai.v1_auto_voice.classify_all_segments_gender", return_value=mock_evidence):
                locked = decide_video_voice(
                    out_dir=Path(tmp) / "job_dlg",
                    srt_segments=segs,
                    voice_mode="dialogue",
                    workspace=tmp,
                )
            self.assertTrue(locked["dual_voice"])
            self.assertEqual(locked["segment_voices"]["2"]["gender"], "male")
            self.assertEqual(locked["segment_voices"]["2"]["id"], VOICE_MALE_ID)

    def test_06_custom_glossary_and_frosted_blur_ass(self):
        """Verify custom glossary injection and frosted_blur ASS cover generation."""
        from backend.ai.translation import save_custom_glossary, load_custom_glossary, build_translation_prompt
        from backend.ass_utils import generate_ass_file

        with tempfile.TemporaryDirectory() as tmp:
            saved = save_custom_glossary({"梅西": "Leo Messi"}, preset="bjd_handmade", merge=False, workspace=tmp)
            loaded = load_custom_glossary(tmp)
            self.assertEqual(loaded["梅西"], "Leo Messi")
            self.assertIn("手帐", loaded)

            prompt = build_translation_prompt(["梅西今天进了两个球"], glossary=loaded)
            self.assertIn("CUSTOM GLOSSARY", prompt)
            self.assertIn("'梅西' -> 'Leo Messi'", prompt)

            seg = SimpleNamespace(
                index=1,
                start=timedelta(seconds=0),
                end=timedelta(seconds=2),
                content="Leo Messi ghi hai bàn thắng",
                ocr_mask_status="located",
                best_block=SimpleNamespace(start=0, end=2, x_pct=0.1, max_x_pct=0.9, y_pct=0.8, max_y_pct=0.88),
                tracking_blocks=[],
            )
            ass_out = Path(tmp) / "frosted.ass"
            with patch.dict(os.environ, {"V1_SUBTITLE_COVER_STYLE": "frosted_blur"}):
                generate_ass_file([seg], [], str(ass_out), 1080, 1920)
            ass_txt = ass_out.read_text(encoding="utf-8-sig")
            self.assertIn("&H50FFFFFF", ass_txt)


if __name__ == "__main__":
    unittest.main()
