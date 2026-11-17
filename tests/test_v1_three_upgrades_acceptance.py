"""Kiểm thử nghiệm thu độc lập 3 đợt nâng cấp Tool V1 (Phần 3, 4, 5)."""

import asyncio
import io
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
import srt

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestDot1DashboardVoiceReviewAndScopedGlossary(unittest.TestCase):
    def test_load_job_segments_exposes_voice_review_and_audio_url(self):
        from backend import segment_editor_service as ses

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            job_dir = ws / "job_demo_01"
            job_dir.mkdir(parents=True, exist_ok=True)
            out_video = Path(tmp_dir) / "banve_demo" / "Dubbed_job_demo_01.mp4"
            out_video.parent.mkdir(parents=True, exist_ok=True)
            out_video.write_bytes(b"video")

            (job_dir / "translated.srt").write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nXin chào\n\n"
                "2\n00:00:01,600 --> 00:00:02,200\nỪ\n",
                encoding="utf-8",
            )
            (job_dir / "voice_lock.json").write_text(
                json.dumps(
                    {
                        "voice_id": "capcut-BV075_streaming",
                        "voice_source": "capcut",
                        "voice_param": "BV075_streaming",
                        "detected_gender": "dual",
                        "confidence": 0.9,
                        "dual_voice": True,
                        "requires_voice_review": True,
                        "unresolved_segment_indices": [2],
                        "segment_voices": {
                            "1": {"id": "capcut-BV075_streaming", "gender": "male"},
                            "2": {"id": "capcut-BV075_streaming", "gender": "unknown"},
                        },
                        "gender_evidence": {
                            "1": {"confidence": 0.92, "reason": "median_f0_male"},
                            "2": {"confidence": 0.15, "reason": "too_short"},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            sr = 16000
            samples = 0.1 * np.sin(2 * np.pi * 220 * np.arange(sr * 3) / sr)
            sf.write(job_dir / "vocals.wav", samples, sr)

            with patch.object(ses, "resolve_workspace_dir", return_value=ws), \
                 patch.object(ses, "resolve_output_dir", return_value=out_video.parent), \
                 patch.object(ses, "find_job_workspace", return_value=job_dir), \
                 patch.object(ses, "_workspace_roots", return_value=[ws]):
                data = ses.get_job_segments("job_demo_01")

                self.assertTrue(data["requires_voice_review"])
                self.assertEqual(data["unresolved_count"], 1)
                self.assertTrue(data["dual_voice"])
                segs = data["segments"]
                self.assertEqual(len(segs), 2)
                self.assertEqual(segs[0]["detected_gender"], "male")
                self.assertFalse(segs[0]["needs_voice_review"])
                self.assertEqual(segs[1]["detected_gender"], "unknown")
                self.assertTrue(segs[1]["needs_voice_review"])
                self.assertIn("/api/jobs/job_demo_01/segments/seg_2/original-audio", segs[1]["original_audio_url"])

                wav_bytes = ses.read_segment_original_audio_wav("job_demo_01", "seg_1")
                self.assertIsNotNone(wav_bytes)
                self.assertTrue(wav_bytes.startswith(b"RIFF"))
                info = sf.info(io.BytesIO(wav_bytes))
                self.assertGreaterEqual(info.duration, 1.3)

    def test_scoped_glossary_precedence_and_isolation(self):
        from ai import translation as tr
        import glossary_learning_service as gls

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_ws = Path(tmp_dir)
            sugg_file = tmp_ws / "control" / "glossary_suggestions.json"
            sugg_file.parent.mkdir(parents=True, exist_ok=True)
            with patch.dict("os.environ", {"AUTODUB_WORKSPACE": tmp_dir}), \
                 patch.object(gls, "WORKSPACE", tmp_ws), \
                 patch.object(gls, "SUGGEST_FILE", sugg_file):
                tr.save_custom_glossary({"手帐": "sổ chung", "全局": "toàn cục"}, workspace=tmp_dir, scope="global")
                tr.save_custom_glossary({"手帐": "sổ craft", "胶带": "washi tape"}, workspace=tmp_dir, scope="domain:craft")
                tr.save_custom_glossary({"手帐": "sổ banve_a"}, workspace=tmp_dir, scope="folder:banve_a")
                tr.save_custom_glossary({"手帐": "sổ banve_b"}, workspace=tmp_dir, scope="folder:banve_b")

                eff_a = tr.resolve_effective_glossary(workspace=tmp_dir, folder_scope="D:\\videos\\banve_a", domain="craft")
                eff_b = tr.resolve_effective_glossary(workspace=tmp_dir, folder_scope="folder:banve_b", domain="craft")
                self.assertEqual(eff_a["全局"], "toàn cục")
                self.assertEqual(eff_a["胶带"], "washi tape")
                self.assertEqual(eff_a["手帐"], "sổ banve_a")
                self.assertEqual(eff_b["手帐"], "sổ banve_b")

                eff_ovr = tr.resolve_effective_glossary(
                    workspace=tmp_dir,
                    folder_scope="folder:banve_a",
                    domain="craft",
                    override_glossary={"手帐": "override"},
                )
                self.assertEqual(eff_ovr["手帐"], "override")

                for _ in range(3):
                    gls.record_edit("trước kia", "hiện tại", scope="folder:banve_a")
                sugs_a = gls.get_suggestions(scope="folder:banve_a")
                sugs_global = gls.get_suggestions(scope="global")
                self.assertTrue(any(s["before"] == "trước kia" and s["after"] == "hiện tại" for s in sugs_a))
                self.assertFalse(any(s["before"] == "trước kia" for s in sugs_global))

                ok = gls.accept_suggestion("trước kia", "hiện tại", scope="folder:banve_a", target_scope="folder:banve_a")
                self.assertTrue(ok.get("added"))
                loaded_a = tr.load_custom_glossary(workspace=tmp_dir, scope="folder:banve_a")
                loaded_global = tr.load_custom_glossary(workspace=tmp_dir, scope="global")
                self.assertEqual(loaded_a.get("trước kia"), "hiện tại")
                self.assertNotIn("trước kia", loaded_global)

    def test_dashboard_and_original_audio_route_contracts(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import phase_c_routes

        app = FastAPI()
        app.include_router(phase_c_routes.router)
        client = TestClient(app)

        with patch.object(phase_c_routes, "read_segment_original_audio_wav", return_value=b"RIFFdemo_wav"):
            resp = client.get("/api/jobs/job123/segments/seg_1/original-audio")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("audio/wav", resp.headers.get("content-type", ""))
            self.assertEqual(resp.content, b"RIFFdemo_wav")

        with patch.object(phase_c_routes, "read_segment_original_audio_wav", return_value=None):
            resp_404 = client.get("/api/jobs/job123/segments/seg_999/original-audio")
            self.assertEqual(resp_404.status_code, 404)

        html = (BACKEND_DIR / "templates" / "dashboard.html").read_text(encoding="utf-8")
        for token in (
            "filterReviewOnlyCheckbox",
            "segmentVoiceReviewBadge",
            "glossaryScopeSelect",
            "selectSubCoverStyle",
            "selectDelogoMode",
            "flagQcVisualPixelCheck",
            "/original-audio",
        ):
            self.assertIn(token, html)


class TestDot2CoverBlurDelogoAndPixelQC(unittest.TestCase):
    def test_sample_box_bg_colors_and_adaptive_ass_cover(self):
        from backend import ocr_utils
        from backend import ass_utils

        dark_frame = np.full((200, 300, 3), (15, 20, 30), dtype=np.uint8)
        bg_dark, fg_dark = ocr_utils._sample_box_bg_colors(dark_frame, 1.0, 0, 300, 200, 0.2, 0.8, 0.6, 0.8)
        self.assertEqual(fg_dark, "&H00FFFFFF")
        self.assertEqual(bg_dark, "&H000F141E")

        light_frame = np.full((200, 300, 3), (240, 240, 240), dtype=np.uint8)
        bg_light, fg_light = ocr_utils._sample_box_bg_colors(light_frame, 1.0, 0, 300, 200, 0.2, 0.8, 0.6, 0.8)
        self.assertEqual(fg_light, "&H00000000")
        self.assertEqual(bg_light, "&H00F0F0F0")

        seg = srt.Subtitle(index=1, start=timedelta(seconds=0.5), end=timedelta(seconds=2.5), content="Xin chào")
        seg.ocr_mask_status = "located"
        seg.geometry_verified = True
        seg.tracking_blocks = [
            {
                "start": 0.5,
                "end": 2.5,
                "x_pct": 0.15,
                "max_x_pct": 0.85,
                "y_pct": 0.78,
                "max_y_pct": 0.86,
                "bg_color_ass": bg_dark,
                "text_contrast_color_ass": fg_dark,
                "geometry_verified": True,
            }
        ]
        seg.best_block = seg.tracking_blocks[0]

        with tempfile.TemporaryDirectory() as tmp_dir:
            ass_adaptive = Path(tmp_dir) / "adaptive.ass"
            ass_utils.generate_ass_file([seg], [], str(ass_adaptive), 720, 1280, cover_style="adaptive_box")
            txt_adaptive = ass_adaptive.read_text(encoding="utf-8-sig")
            self.assertIn(f"\\1c{bg_dark}", txt_adaptive)
            self.assertIn(f"\\1c{fg_dark}", txt_adaptive)

            ass_blur = Path(tmp_dir) / "blur.ass"
            ass_utils.generate_ass_file([seg], [], str(ass_blur), 720, 1280, cover_style="localized_blur")
            txt_blur = ass_blur.read_text(encoding="utf-8-sig")
            self.assertNotIn("Dialogue: 0,", txt_blur)
            self.assertIn("ToolV1BlurData:", txt_blur)
            self.assertIn("Style: TextStyle,Arial,38,&H00FFFFFF", txt_blur)
            self.assertNotIn(f"\\1c{bg_dark}", txt_blur)

    def test_detect_static_watermarks_and_process_video_filtergraph(self):
        from backend import ocr_subtitle_locator as loc
        from backend import video_utils
        from backend import v1_media_streams
        import canvas_settings
        from backend.v1_media_streams import MainVideo

        blocks = [
            {"start": 0.0, "end": 18.0, "x_pct": 0.03, "max_x_pct": 0.22, "y_pct": 0.04, "max_y_pct": 0.10, "is_static": True, "text": "@DouyinChannel"},
            {"start": 1.0, "end": 17.0, "x_pct": 0.20, "max_x_pct": 0.80, "y_pct": 0.80, "max_y_pct": 0.88, "is_static": True, "text": "Thoại ở vùng sub"},
        ]
        wms = loc.detect_static_watermarks(blocks, duration=20.0, subtitle_top=0.75, subtitle_bottom=0.92)
        self.assertEqual(len(wms), 1)
        self.assertEqual(wms[0]["text"], "@DouyinChannel")

        self.assertEqual(canvas_settings.DEFAULT_CANVAS_SETTINGS["sub_cover_style"], "solid_box")
        self.assertEqual(canvas_settings.DEFAULT_CANVAS_SETTINGS["delogo_mode"], "off")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_vid = tmp / "in.mp4"
            in_vid.write_bytes(b"in")
            in_ass = tmp / "sub.ass"
            in_ass.write_text("[Script Info]\n", encoding="utf-8")
            in_wav = tmp / "mix.wav"
            in_wav.write_bytes(b"wav")
            out_vid = tmp / "out.mp4"

            captured_cmds = []

            def fake_run(cmd, **kwargs):
                captured_cmds.append(cmd)
                out_vid.write_bytes(b"out" * 5000)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(v1_media_streams, "probe_main_video", return_value=MainVideo(0, 720, 1280, 30.0, 4.0, 1, 1, False)), \
                 patch.object(v1_media_streams, "validate_video_output"), \
                 patch.object(video_utils, "run_batch_subprocess", side_effect=fake_run), \
                 patch.object(canvas_settings, "get_canvas_settings", return_value={"sub_cover_style": "localized_blur", "delogo_mode": "smart", "watermark_enabled": False}):
                ok = video_utils.process_video(
                    str(in_vid),
                    str(in_ass),
                    str(in_wav),
                    str(out_vid),
                    is_ass=True,
                    subtitle_cover_style="localized_blur",
                    localized_blur_boxes=[{"start": 0.5, "end": 2.0, "x_pct": 0.1, "max_x_pct": 0.9, "y_pct": 0.8, "max_y_pct": 0.88}],
                    delogo_mode="smart",
                    delogo_boxes=wms,
                )
            self.assertTrue(ok)
            cmd0 = captured_cmds[0]
            vf_flag = "-filter_complex" if "-filter_complex" in cmd0 else "-vf"
            vf = cmd0[cmd0.index(vf_flag) + 1]
            self.assertIn("delogo=x=", vf)
            self.assertIn("format=yuv420p", vf)
            self.assertIn("split=2[", vf)
            self.assertIn("enable='gte(t,0.500000)*lt(t,2.000000)'", vf)

            captured_cmds.clear()
            with patch.object(v1_media_streams, "probe_main_video", return_value=MainVideo(0, 720, 1280, 30.0, 4.0, 1, 1, False)), \
                 patch.object(v1_media_streams, "validate_video_output"), \
                 patch.object(video_utils, "run_batch_subprocess", side_effect=fake_run), \
                 patch.object(canvas_settings, "get_canvas_settings", return_value={"sub_cover_style": "solid_box", "delogo_mode": "off", "watermark_enabled": False}):
                ok_off = video_utils.process_video(
                    str(in_vid),
                    str(in_ass),
                    str(in_wav),
                    str(out_vid),
                    is_ass=True,
                    delogo_mode="off",
                )
            self.assertTrue(ok_off)
            cmd_off = captured_cmds[0]
            vf_flag_off = "-filter_complex" if "-filter_complex" in cmd_off else "-vf"
            vf_off = cmd_off[cmd_off.index(vf_flag_off) + 1]
            self.assertNotIn("delogo=", vf_off)

    def test_post_render_pixel_qc_warn_and_pass_never_fails(self):
        from backend import ocr_utils
        from backend import v1_quality_gate as qg
        from backend.v1_media_streams import MainVideo

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "rendered.mp4"
            video.write_bytes(b"video bytes" * 2000)
            dub = tmp / "1.wav"
            bgm = tmp / "mixed.wav"
            sr = 16000
            sf.write(dub, 0.05 * np.sin(2 * np.pi * 200 * np.arange(sr * 2) / sr), sr)
            sf.write(bgm, 0.05 * np.sin(2 * np.pi * 200 * np.arange(sr * 4) / sr), sr)
            ass = tmp / "final.ass"
            ass.write_text(
                "[Script Info]\n" + "; line\n" * 12 +
                "[V4+ Styles]\nStyle: BgStyle,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
                "[Events]\nDialogue: 0,0:00:00.50,0:00:02.50,BgStyle,,0,0,0,,{\\p1}m 0 0 l 100 0 100 40 0 40{\\p0}\n"
                "Dialogue: 1,0:00:00.50,0:00:02.50,TextStyle,,0,0,0,,Xin chào\n",
                encoding="utf-8",
            )
            seg = SimpleNamespace(
                index=1,
                start=timedelta(seconds=0.5),
                end=timedelta(seconds=2.5),
                content="Xin chào",
                ocr_mask_status="located",
                best_block=SimpleNamespace(x_pct=0.1, max_x_pct=0.9, y_pct=0.8, max_y_pct=0.9, is_static=False, is_packaging=False),
            )
            fake_frame = np.zeros((1280, 720, 3), dtype=np.uint8)
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (True, fake_frame)
            import cv2
            cap.get.side_effect = lambda prop: {cv2.CAP_PROP_FPS: 30., cv2.CAP_PROP_FRAME_COUNT: 120,
                                                cv2.CAP_PROP_FRAME_WIDTH: 720,
                                                cv2.CAP_PROP_FRAME_HEIGHT: 1280}.get(prop, 0)

            with patch("cv2.VideoCapture", return_value=cap), \
                 patch.object(ocr_utils, "_readtext_batch", return_value=[[[[[0, 0], [10, 0], [10, 10], [0, 10]], "你好世界", 0.92]]]), \
                 patch.object(ocr_utils, "release_ocr_reader") as mock_release, \
                 patch("v1_media_streams.probe_main_video", return_value=MainVideo(0, 720, 1280, 30.0, 4.0, 1, 1, False)):
                rep_warn = qg.run_quality_gate(
                    job_id="qc_pixel_warn",
                    final_video_path=video,
                    original_video_path=video,
                    translated_subtitles=[seg],
                    dubbing_audio_files=[{"index": 1, "path": str(dub), "start": 0.5}],
                    mixed_audio_path=bgm,
                    ass_subtitle_path=ass,
                    policy="BLOCK",
                    workspace_path=tmp,
                    enable_visual_pixel_qc=True,
                )
            self.assertEqual(rep_warn.overall_status, "PASSED_WITH_WARNINGS")
            self.assertEqual(rep_warn.failure_count, 0)
            px = next(c for c in rep_warn.checks if c.name == "visual_pixel_coverage")
            self.assertEqual(px.status, "WARN")
            self.assertGreaterEqual(mock_release.call_count, 1)

            with patch("cv2.VideoCapture", return_value=cap), \
                 patch.object(ocr_utils, "_readtext_batch", return_value=[[[[[0, 0], [10, 0], [10, 10], [0, 10]], "Xin chao", 0.95]]]), \
                 patch.object(ocr_utils, "release_ocr_reader") as mock_release_ok, \
                 patch("v1_media_streams.probe_main_video", return_value=MainVideo(0, 720, 1280, 30.0, 4.0, 1, 1, False)):
                rep_pass = qg.run_quality_gate(
                    job_id="qc_pixel_pass",
                    final_video_path=video,
                    original_video_path=video,
                    translated_subtitles=[seg],
                    dubbing_audio_files=[{"index": 1, "path": str(dub), "start": 0.5}],
                    mixed_audio_path=bgm,
                    ass_subtitle_path=ass,
                    policy="BLOCK",
                    workspace_path=tmp,
                    enable_visual_pixel_qc=True,
                )
            self.assertEqual(rep_pass.overall_status, "PASSED")
            px_pass = next(c for c in rep_pass.checks if c.name == "visual_pixel_coverage")
            self.assertEqual(px_pass.status, "PASS")
            self.assertGreaterEqual(mock_release_ok.call_count, 1)


class TestDot3StructuredTranslationAndDomainPresets(unittest.TestCase):
    def test_structured_response_id_mapping_and_domain_prompts(self):
        from backend.ai import v1_gemini_response as gresp
        from backend.ai import v1_translation_cache as tcache
        from backend.ai import translation as tr

        reordered = json.dumps([{"id": 2, "text": "Câu hai"}, {"id": 1, "text": "Câu một"}], ensure_ascii=False)
        data = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": reordered}]}}]}
        self.assertEqual(gresp.translation_response(data, 2, "vi", expected_ids=[1, 2]), ["Câu một", "Câu hai"])

        legacy = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '["Một", "Hai"]'}]}}]}
        self.assertEqual(gresp.translation_response(legacy, 2, "vi", expected_ids=[1, 2]), ["Một", "Hai"])

        mismatch = json.dumps([{"id": 1, "text": "Một"}, {"id": 99, "text": "Sai"}], ensure_ascii=False)
        with self.assertRaises(gresp.GeminiResponseError):
            gresp.translation_response(
                {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": mismatch}]}}]},
                2, "vi", expected_ids=[1, 2],
            )

        p_food = tr.build_translation_prompt(
            ["新鲜蘑菇"],
            cue_meta=[{"id": 1, "speaker": "female", "max_words": 8, "ocr_hint": "蘑菇"}],
            domain="food_life",
            structured_output=True,
        )
        self.assertIn("food_life", p_food)
        self.assertNotIn("RTX 4070", p_food)
        self.assertNotIn("贴手帐", p_food)
        self.assertIn('"speaker": "female"', p_food)

        p_tech = tr.build_translation_prompt(["显卡测试"], domain="tech_pc", structured_output=True)
        self.assertIn("RTX 4070", p_tech)

        k1 = tcache.cache_key([{"text": "a"}], ["m"], "acc", domain="craft", folder_scope="folder:banve")
        k2 = tcache.cache_key([{"text": "a"}], ["m"], "acc", domain="tech_pc", folder_scope="folder:banve")
        k3 = tcache.cache_key([{"text": "a"}], ["m"], "acc", domain="craft", folder_scope="folder:other")
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_translate_with_gemini_schema_and_http400_fallback(self):
        from backend.ai import translation as tr
        from backend.ai import v1_gemini_dispatcher as dispatcher

        tr._DOMAIN_CLASSIFICATION_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict("os.environ", {"AUTODUB_WORKSPACE": tmp_dir}), \
                 patch.object(dispatcher, "circuit_breaker", dispatcher.GeminiCircuitBreaker(Path(tmp_dir) / "cb.db")):
                calls = []

                def fake_post(url, json=None, headers=None, timeout=None):
                    calls.append(json)
                    text_part = (json or {}).get("contents", [{}])[0].get("parts", [{}])[0].get("text", "")
                    if "Phân loại chủ đề chính" in text_part:
                        r = MagicMock(status_code=200)
                        r.json.return_value = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"domain": "craft"}'}]}}]}
                        return r
                    gen_cfg = (json or {}).get("generationConfig", {})
                    if "responseSchema" in gen_cfg:
                        r400 = MagicMock(status_code=400)
                        r400.json.return_value = {"error": {"message": "Schema syntax unsupported"}}
                        return r400
                    r200 = MagicMock(status_code=200)
                    r200.json.return_value = {
                        "candidates": [{
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": '[{"id": 2, "text": "Câu thứ hai"}, {"id": 1, "text": "Câu thứ nhất"}]'}]},
                        }]
                    }
                    return r200

                with patch.object(tr.requests, "post", side_effect=fake_post), \
                     patch.object(tr, "extract_video_frames_base64", return_value=["ZmFrZWJhc2U2NA=="]):
                    out = tr.translate_with_gemini(
                        ["第一句", "第二句"],
                        target_lang="vi",
                        api_key="test-gemini-key-for-unit-tests",
                        video_path="D:\\banve\\craft_demo.mp4",
                        cue_meta=[
                            {"id": 1, "speaker": "female", "max_words": 8},
                            {"id": 2, "speaker": "male", "max_words": 8},
                        ],
                    )
                self.assertEqual(out, ["Câu thứ nhất", "Câu thứ hai"])
                self.assertEqual(len(calls), 3)
                self.assertIn("responseSchema", calls[1]["generationConfig"])
                self.assertEqual(calls[2]["generationConfig"].get("responseMimeType"), "application/json")
                self.assertNotIn("responseSchema", calls[2]["generationConfig"])

    def test_orchestrator_stage_translate_builds_speaker_and_max_words_metadata(self):
        from backend.v1_orchestrator import V1Orchestrator, _ExecutionContext

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            video_file = ws / "thu_muc_rieng" / "clip.mp4"
            video_file.parent.mkdir(parents=True, exist_ok=True)
            video_file.write_bytes(b"v")
            out_dir = ws / "job_orch"
            out_dir.mkdir(parents=True, exist_ok=True)
            vocals = out_dir / "vocals.wav"
            vocals.write_bytes(b"a")

            orch = V1Orchestrator(workspace_path=ws)
            orch.manifest_manager = MagicMock()
            orch.manifest_manager.record_stage_start = MagicMock()
            orch.manifest_manager.record_stage_complete = MagicMock()

            async def _notify(*args, **kwargs):
                return None

            async def _pause():
                return None

            async def _run_stage(coro, timeout=None):
                return await coro

            ctx = _ExecutionContext(
                v_path=video_file,
                job_id="job_orch",
                out_dir=out_dir,
                manifest=SimpleNamespace(stages={}),
                effective_cfg={"target_lang": "vi", "voice_mode": "auto", "voice_config": {"enabled": True, "dual_voice": True}, "feature_flags": {}},
                planned={},
                resolved_mode="SHORT",
                overrides={},
                delivery_path=None,
                t_job_start=0.0,
                base_name="clip",
                visual_sub_to_speech=False,
                can_resume=False,
                verified_stages=set(),
                video_dur_s=10.0,
                notify=_notify,
                check_stop=lambda: None,
                pause_checkpoint=_pause,
                run_stage=_run_stage,
            )

            s1 = srt.Subtitle(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="你好")
            s1.best_block = {"text": "你好OCR"}
            s2 = srt.Subtitle(index=2, start=timedelta(seconds=2.2), end=timedelta(seconds=5.2), content="再见")
            s2.best_block = {"text": "再见OCR"}

            captured = {}

            def fake_decide_voice(**kwargs):
                return {
                    "voice_id": "capcut-BV075_streaming",
                    "voice_source": "capcut",
                    "voice_param": "BV075_streaming",
                    "voice_label": "Dual",
                    "detected_gender": "dual",
                    "segment_voices": {"1": {"gender": "male"}, "2": {"gender": "female"}},
                }

            def fake_translate_subtitles(segs, *args, **kwargs):
                captured["kwargs"] = kwargs
                for idx, seg in enumerate(segs, 1):
                    seg.orig_content = seg.content
                    seg.content = f"Dịch {idx}"
                return segs

            with patch("ai.v1_auto_voice.decide_video_voice", side_effect=fake_decide_voice), \
                 patch("ai.translation.translate_subtitles", side_effect=fake_translate_subtitles), \
                 patch("ai.transcription.save_srt"):
                translated, _ = asyncio.run(orch._stage_translate(ctx, [s1, s2], original_audio=vocals, vocals_audio=vocals))

            self.assertEqual([s.content for s in translated], ["Dịch 1", "Dịch 2"])
            meta = captured["kwargs"]["cue_meta"]
            self.assertEqual(len(meta), 2)
            self.assertEqual(meta[0]["speaker"], "male")
            self.assertEqual(meta[1]["speaker"], "female")
            self.assertEqual(meta[0]["ocr_hint"], "你好OCR")
            self.assertGreaterEqual(meta[0]["max_words"], 3)
            self.assertEqual(Path(captured["kwargs"]["folder_scope"]).name, "thu_muc_rieng")


if __name__ == "__main__":
    unittest.main()
