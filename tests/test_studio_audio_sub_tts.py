"""Kiểm thử toàn diện 3 quy trình Studio Quy Trình: Tách giọng BS-RoFormer, Tạo/Dịch Phụ đề & Lồng tiếng AI."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import workflow_api as wf  # noqa: E402


def _write_valid_wav(path: Path, frames: int = 1600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_f:
        wav_f.setnchannels(2)
        wav_f.setsampwidth(2)
        wav_f.setframerate(44100)
        wav_f.writeframes(b"\x01\x00\x01\x00" * frames)
    return path


class TestStudioVocalSeparation(unittest.TestCase):
    def test_find_vocal_files_skips_corrupt_and_internal_wavs(self):
        with tempfile.TemporaryDirectory() as td:
            job = Path(td) / "vid_sep"
            job.mkdir()
            # Tệp hệ thống nội bộ không bao giờ được nhận nhầm là stem tách âm
            _write_valid_wav(job / "original.wav")
            _write_valid_wav(job / "mixed.wav")
            _write_valid_wav(job / "manual_tts_track.wav")
            # Tệp BS-RoFormer bị lỗi 0-byte / hỏng header phải bị bỏ qua
            bs = job / "bs_roformer"
            bs.mkdir()
            (bs / "original_(Instrumental)_corrupt.wav").write_bytes(b"corrupt")
            (bs / "original_(Vocals)_empty.wav").write_bytes(b"")

            inst, voc = wf._find_vocal_files(job)
            self.assertIsNone(inst)
            self.assertIsNone(voc)

            # Khi có file WAV chuẩn -> trả về chính xác cả 2 kênh
            real_inst = _write_valid_wav(bs / "original_(Instrumental)_bs_roformer.wav")
            real_voc = _write_valid_wav(bs / "original_(Vocals)_bs_roformer.wav")
            inst2, voc2 = wf._find_vocal_files(job)
            self.assertEqual(inst2, real_inst)
            self.assertEqual(voc2, real_voc)

    def test_vocal_status_reports_partial_when_only_no_vocals_exists(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "workspace"
            job = ws / "vid_legacy"
            _write_valid_wav(job / "no_vocals.wav")

            with patch.object(wf, "WORKSPACE", ws), patch.object(wf, "_vocal_separation_tasks", {}):
                res = asyncio.run(wf.api_get_vocal_status("vid_legacy"))

            self.assertFalse(res["has_separated"])
            self.assertTrue(res["has_instrumental"])
            self.assertFalse(res["has_vocals"])
            self.assertEqual(res["status"], "partial")
            self.assertIn("no_vocals.wav", res["instrumental_url"])

    def test_vocal_separation_worker_acquires_gpu_gatekeeper(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "workspace"
            job = ws / "vid_gpu"
            _write_valid_wav(job / "original.wav")
            tasks = {"vid_gpu": {"status": "queued", "progress": 0, "message": ""}}
            gate_calls = []

            @contextmanager
            def fake_gatekeeper(tag):
                gate_calls.append(tag)
                yield

            def fake_sep(orig_wav_str, out_dir_str):
                bs = Path(out_dir_str) / "bs_roformer"
                v = _write_valid_wav(bs / "original_(Vocals)_model.wav")
                i = _write_valid_wav(bs / "original_(Instrumental)_model.wav")
                return str(v), str(i)

            import v1_gpu_gatekeeper
            import video_utils
            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "_vocal_separation_tasks", tasks), \
                 patch.object(v1_gpu_gatekeeper, "gpu_gatekeeper", side_effect=fake_gatekeeper), \
                 patch.object(video_utils, "separate_vocals_demucs", side_effect=fake_sep):
                wf._run_vocal_separation_worker("vid_gpu")

            self.assertEqual(gate_calls, ["studio_vocal_sep"])
            self.assertEqual(tasks["vid_gpu"]["status"], "completed")
            self.assertTrue(tasks["vid_gpu"]["has_separated"])
            self.assertTrue(tasks["vid_gpu"]["instrumental_url"])
            self.assertTrue(tasks["vid_gpu"]["vocals_url"])


class TestStudioSubtitlesAndAssSync(unittest.TestCase):
    def test_generate_cn_uses_isolated_vocals_when_available_and_returns_subtitles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inp = root / "phoi"
            dl = root / "downloads"
            for d in (ws, inp, dl):
                d.mkdir()
            raw_vid = inp / "vid_cn.mp4"
            raw_vid.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 4096)
            job = ws / "vid_cn"
            voc_wav = _write_valid_wav(job / "bs_roformer" / "original_(Vocals)_roformer.wav")
            _write_valid_wav(job / "bs_roformer" / "original_(Instrumental)_roformer.wav")

            asr_calls = []

            def fake_ffmpeg_run(cmd, **kw):
                _write_valid_wav(Path(cmd[-1]))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            def fake_asr(audio_in, srt_out, original_audio_path=None, **kw):
                asr_calls.append((Path(audio_in), Path(srt_out), Path(original_audio_path) if original_audio_path else None))
                Path(srt_out).write_text(
                    "1\n00:00:00,200 --> 00:00:02,100\n你好世界\n\n2\n00:00:02,200 --> 00:00:04,000\n非常结实\n",
                    encoding="utf-8",
                )
                return [object(), object()]

            @contextmanager
            def noop_ctx(*args, **kwargs):
                yield

            import ai.v1_asr_isolated as asr_mod
            import batch_control
            import v1_gpu_gatekeeper

            async def _run_job():
                res = await wf.api_manual_generate_cn({"stem": "vid_cn"})
                jid = res["job_id"]
                for _ in range(40):
                    st = await wf.api_manual_job_status(jid)
                    if st["status"] in ("done", "error"):
                        return st
                    await asyncio.sleep(0.05)
                raise TimeoutError("generate-cn timed out")

            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "INPUT_DIR", inp), \
                 patch.object(wf, "DOWNLOADS_DIR", dl), \
                 patch.object(wf, "_manual_pipeline_ownership", side_effect=noop_ctx), \
                 patch.object(batch_control, "run", side_effect=fake_ffmpeg_run), \
                 patch.object(v1_gpu_gatekeeper, "gpu_gatekeeper", side_effect=noop_ctx), \
                 patch.object(asr_mod, "extract_subtitles_isolated", side_effect=fake_asr):
                final_st = asyncio.run(_run_job())

            self.assertEqual(final_st["status"], "done")
            self.assertEqual(len(asr_calls), 1)
            self.assertEqual(asr_calls[0][0], voc_wav, "Phải ưu tiên truyền kênh Vocals đã lọc sạch nhạc cho Whisper")
            self.assertEqual(asr_calls[0][2], job / "original.wav", "Truyền kèm original.wav để khôi phục năng lượng câu nhỏ")
            subs = final_st["result"]["subtitles"]
            self.assertEqual(len(subs), 2)
            self.assertEqual(subs[0]["text"], "你好世界")

    def test_manual_translate_accepts_chinese_subtitles_and_writes_translated_srt(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "workspace"
            ws.mkdir()

            @contextmanager
            def noop_ctx(*args, **kwargs):
                yield

            def fake_translate(segments, target_lang, **kw):
                for seg in segments:
                    seg.content = f"[VI] {seg.content}"

            import ai.translation as tr_mod

            async def _run_tr():
                res = await wf.api_manual_translate({
                    "stem": "vid_tr",
                    "chinese_subtitles": [
                        {"start": 0.0, "end": 2.0, "text": "第一句"},
                        {"start": 2.2, "end": 4.5, "text": "第二句"},
                    ],
                })
                jid = res["job_id"]
                for _ in range(40):
                    st = await wf.api_manual_job_status(jid)
                    if st["status"] in ("done", "error"):
                        return st
                    await asyncio.sleep(0.05)
                raise TimeoutError("translate timed out")

            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "_manual_pipeline_ownership", side_effect=noop_ctx), \
                 patch.object(tr_mod, "translate_subtitles", side_effect=fake_translate):
                st = asyncio.run(_run_tr())

            self.assertEqual(st["status"], "done")
            self.assertEqual(len(st["result"]["subtitles"]), 2)
            self.assertEqual(st["result"]["subtitles"][0]["text"], "[VI] 第一句")
            self.assertTrue((ws / "vid_tr" / "translated.srt").is_file())

    def test_build_custom_ass_file_syncs_edited_translated_srt_while_keeping_bg_covers(self):
        with tempfile.TemporaryDirectory() as td:
            job = Path(td) / "vid_ass"
            job.mkdir()
            orig_ass = job / "final.ass"
            orig_ass.write_text(
                "[Script Info]\nScriptType: v4.00+\nPlayResX: 720\nPlayResY: 1280\n\n"
                "[V4+ Styles]\n"
                "Style: BgStyle,Arial,38,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
                "Style: TextStyle,Arial,38,&H00000000,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,10,10,10,1\n\n"
                "[Events]\n"
                "Dialogue: 0,0:00:00.00,0:00:03.00,BgStyle,,0,0,0,,{\\an7\\pos(38,940)}{\\p1}m 0 0 l 644 0 l 644 100 l 0 100{\\p0}\n"
                "Dialogue: 1,0:00:00.00,0:00:03.00,TextStyle,,0,0,0,,{\\an8\\pos(360,953)}Câu cũ chưa sửa\n",
                encoding="utf-8",
            )
            old_t = time.time() - 10
            os.utime(orig_ass, (old_t, old_t))

            trans_srt = job / "translated.srt"
            trans_srt.write_text(
                "1\n00:00:00,000 --> 00:00:03,000\nCâu mới đã chỉnh sửa trong Studio\n",
                encoding="utf-8",
            )

            out_ass = wf.build_custom_ass_file(job, {"sub_font_color": "#facc15", "sub_bg_color": "#000000", "sub_bg_opacity": 0.85})
            self.assertIsNotNone(out_ass)
            content = out_ass.read_text(encoding="utf-8")
            self.assertIn("BgStyle,,0,0,0,,{\\an7\\pos(38,940)}{\\p1}", content, "Phải giữ nguyên 100% hộp che OCR BgStyle")
            self.assertIn("Câu mới đã chỉnh sửa trong Studio", content, "Phải cập nhật nội dung câu mới từ translated.srt")
            self.assertNotIn("Câu cũ chưa sửa", content)


class TestStudioTtsAndExportNoDoubleVoice(unittest.TestCase):
    def test_custom_export_with_tts_mix_uses_instrumental_instead_of_mixed_wav_and_fresh_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inp = root / "phoi"
            dl = root / "downloads"
            out = root / "banve"
            for d in (ws, inp, dl, out):
                d.mkdir()

            stem = "vid_export_tts"
            raw_v = inp / f"{stem}.mp4"
            raw_v.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"r" * 4096)

            job = ws / stem
            job.mkdir()
            final_v = job / f"final_{stem}.mp4"
            final_v.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"f" * 4096)

            # Có cả mixed.wav (chứa giọng cũ) lẫn bs_roformer instrumental (không lời) + manual_tts_track.wav (giọng mới)
            old_mixed = _write_valid_wav(job / "mixed.wav")
            inst_bgm = _write_valid_wav(job / "bs_roformer" / "original_(Instrumental)_roformer.wav")
            _write_valid_wav(job / "bs_roformer" / "original_(Vocals)_roformer.wav")
            new_tts = _write_valid_wav(job / "manual_tts_track.wav")
            (job / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nGiọng mới không chồng lấn\n", encoding="utf-8")

            captured_cmds = []

            def fake_ffmpeg_ctl(cmd, job_ctl, timeout=600):
                captured_cmds.append(cmd)
                Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"o" * 4096)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "INPUT_DIR", inp), \
                 patch.object(wf, "DOWNLOADS_DIR", dl), \
                 patch.object(wf, "OUTPUT_DIR", out), \
                 patch.object(wf, "_valid_final_video", return_value=True), \
                 patch.object(wf, "probe_stream_durations", return_value={"video": 5.0, "audio": 5.0}), \
                 patch.object(wf, "detect_letterbox_crop", return_value=None), \
                 patch.object(wf, "_run_ffmpeg_ctl", side_effect=fake_ffmpeg_ctl):
                res = wf._execute_custom_export({
                    "stem": stem,
                    "resolution": "1080p",
                    "bitrate_kbps": 8000,
                    "fps": "30",
                    "format": "mp4",
                    "export_srt": True,
                    "srt_type": "translated",
                    "output_folder": str(out),
                    "use_anti_reup": False,
                    "prepend_thumbnail": False,
                    "tts_mix": True,
                    "vocal_mode": "full",
                    "canvas_settings": {"aspect_ratio": "9:16", "auto_crop_black_bars": False},
                })

            self.assertEqual(res["status"], "ok")
            self.assertEqual(len(captured_cmds), 1)
            cmd = captured_cmds[0]
            cmd_str = " ".join(str(x) for x in cmd)
            # 1. Phải lấy source_video thay vì final_video cũ, và đốt phụ đề mới
            self.assertEqual(cmd[cmd.index("-i") + 1], str(raw_v))
            self.assertIn("subtitles=", cmd_str)
            # 2. Phải trộn manual_tts_track.wav trên nền inst_bgm (no_vocals), TUYỆT ĐỐI không dùng mixed.wav (giọng cũ)!
            self.assertIn(str(inst_bgm), cmd)
            self.assertIn(str(new_tts), cmd)
            self.assertNotIn(str(old_mixed), cmd, "Không được lấy mixed.wav cũ gây hiện tượng 2 giọng đọc đè lên nhau!")
            self.assertIn("volume=0.85[expbase]", cmd_str)

    def test_apply_timeline_edits_mixes_manual_tts_and_unlinks_stale_proxy_final(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inp = root / "phoi"
            dl = root / "downloads"
            th = root / "thumbs"
            for d in (ws, inp, dl, th):
                d.mkdir()

            stem = "vid_cut_tts"
            raw_v = inp / f"{stem}.mp4"
            raw_v.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 4096)

            job = ws / stem
            job.mkdir()
            stale_proxy = job / "proxy_final.mp4"
            stale_proxy.write_bytes(b"stale-proxy")
            _write_valid_wav(job / "bs_roformer" / "original_(Instrumental)_roformer.wav")
            _write_valid_wav(job / "bs_roformer" / "original_(Vocals)_roformer.wav")
            _write_valid_wav(job / "manual_tts_track.wav")

            probe_json = json.dumps({"streams": [
                {"codec_type": "video", "width": 1080, "height": 1920},
                {"codec_type": "audio", "sample_rate": "44100", "channels": 2, "channel_layout": "stereo"},
            ]})

            ffmpeg_Graph_cmds = []

            def fake_subproc_run(cmd, **kw):
                if cmd and cmd[0] == "ffprobe":
                    return subprocess.CompletedProcess(cmd, 0, probe_json, "")
                if cmd and cmd[0] == "ffmpeg" and str(cmd[-1]).endswith("_studio_tts_bed.wav"):
                    _write_valid_wav(Path(cmd[-1]))
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            def fake_ffmpeg_ctl(cmd, job_ctl, timeout=600):
                ffmpeg_Graph_cmds.append(cmd)
                Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"e" * 4096)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "INPUT_DIR", inp), \
                 patch.object(wf, "DOWNLOADS_DIR", dl), \
                 patch.object(wf, "THUMBNAILS_DIR", th), \
                 patch.object(wf, "_valid_final_video", return_value=True), \
                 patch.object(wf.subprocess, "run", side_effect=fake_subproc_run), \
                 patch.object(wf, "_run_ffmpeg_ctl", side_effect=fake_ffmpeg_ctl):
                out = wf._execute_apply_edits({
                    "stem": stem,
                    "clips": [{"start": 0.0, "end": 3.0, "speed": 1.25}],
                    "target_version": "raw",
                    "vocal_mode": "full",
                    "tts_mix": True,
                })

            self.assertEqual(out["status"], "ok")
            self.assertFalse(stale_proxy.exists(), "Phải xóa proxy_final.mp4 cũ sau khi cắt ghép để tránh swap lại video cũ")
            self.assertEqual(len(ffmpeg_Graph_cmds), 1)
            cmd = ffmpeg_Graph_cmds[0]
            self.assertIn(str(job / "_studio_tts_bed.wav"), cmd, "Cắt ghép có tts_mix phải cắt đúng trên bản _studio_tts_bed.wav")
            fc = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("[1:a]", fc, "Filter cắt ghép tốc độ 1.25x phải lấy nguồn âm thanh [1:a] đã trộn giọng AI mới")

    def test_video_data_exposes_manual_tts_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inp = root / "phoi"
            dl = root / "downloads"
            out = root / "banve"
            for d in (ws, inp, dl, out):
                d.mkdir()
            (inp / "vid_meta.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"m" * 4096)
            job = ws / "vid_meta"
            _write_valid_wav(job / "manual_tts_track.wav")

            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "INPUT_DIR", inp), \
                 patch.object(wf, "DOWNLOADS_DIR", dl), \
                 patch.object(wf, "OUTPUT_DIR", out):
                data = asyncio.run(wf.api_get_workflow_video_data("vid_meta"))

            self.assertTrue(data["has_manual_tts"])
            self.assertTrue(data["manual_tts_newer"])
            self.assertIn("manual_tts_track.wav", data["manual_tts_url"])
            self.assertTrue(data["steps"]["step5"]["ready"])

    def test_manual_generate_tts_full_and_single_cue_partial_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inp = root / "phoi"
            dl = root / "downloads"
            for d in (ws, inp, dl):
                d.mkdir()
            (inp / "vid_tts.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"t" * 4096)

            synth_indices = []

            async def fake_single_tts(cue, out_dir, source, param, **kw):
                synth_indices.append(cue.index)
                Path(out_dir, f"{cue.index}.mp3").write_bytes(b"ID3-fake-mp3")

            def fake_batch_run(cmd, **kw):
                _write_valid_wav(Path(cmd[-1]))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            @contextmanager
            def noop_ctx(*args, **kwargs):
                yield

            import ai.voice_cloning as vc_mod
            import batch_control
            import disk_guard
            import voice_selection

            fake_catalog = [{"id": "capcut-BV562_streaming", "source": "capcut", "param": "BV562_streaming", "label": "Mai (Nữ chuẩn)"}]
            subs_payload = [
                {"start": 0.0, "end": 2.0, "text": "Câu thứ nhất"},
                {"start": 2.2, "end": 4.2, "text": "Câu thứ hai"},
            ]

            async def _run_tts(only_idx: int):
                res = await wf.api_manual_generate_tts({
                    "stem": "vid_tts",
                    "voice_id": "capcut-BV562_streaming",
                    "subtitles": subs_payload,
                    "only_cue_index": only_idx,
                    "duration": 5.0,
                })
                jid = res["job_id"]
                for _ in range(40):
                    st = await wf.api_manual_job_status(jid)
                    if st["status"] in ("done", "error"):
                        return st
                    await asyncio.sleep(0.05)
                raise TimeoutError("generate-tts timed out")

            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf, "INPUT_DIR", inp), \
                 patch.object(wf, "DOWNLOADS_DIR", dl), \
                 patch.object(wf, "_manual_pipeline_ownership", side_effect=noop_ctx), \
                 patch.object(voice_selection, "catalog", return_value=fake_catalog), \
                 patch.object(disk_guard, "ensure_free_space", return_value=None), \
                 patch.object(vc_mod, "generate_single_tts", side_effect=fake_single_tts), \
                 patch.object(batch_control, "run", side_effect=fake_batch_run):
                # Lần 1: tạo toàn bộ 2 câu
                st_full = asyncio.run(_run_tts(0))
                self.assertEqual(st_full["status"], "done")
                self.assertEqual(synth_indices, [1, 2])
                self.assertIn("manual_tts_track.wav", st_full["result"]["track_url"])

                # Lần 2: chỉ đọc lại đúng câu #2 -> tận dụng cache 1.mp3, chỉ gọi TTS cho câu #2
                synth_indices.clear()
                st_partial = asyncio.run(_run_tts(2))
                self.assertEqual(st_partial["status"], "done")
                self.assertEqual(synth_indices, [2], "Đọc lại 1 câu chỉ được tổng hợp lại đúng câu #2, giữ nguyên cache câu #1")
                self.assertEqual(st_partial["result"]["partial_cue"], 2)
                self.assertIn("2.mp3", st_partial["result"]["cue_audio_url"])


class TestCrossToolAndPipelineIsolation(unittest.TestCase):
    def test_active_pipeline_video_is_protected_while_other_videos_stay_editable(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "workspace"
            ws.mkdir()
            active_status = {
                "active": True,
                "video_name": "telegram_inflight_123.mp4",
                "workers": {"1": {"active": True, "video_name": "worker_inflight_456.mp4"}},
            }
            with patch.object(wf, "WORKSPACE", ws), \
                 patch.object(wf.job_tracker, "get_status", return_value=active_status):
                # 1. Đúng video đang chạy trên Tool V1 -> chặn ghi đè 409 để bảo vệ pipeline chính
                with self.assertRaises(wf.HTTPException) as ctx1:
                    asyncio.run(wf.api_save_workflow_subtitles({
                        "stem": "telegram_inflight_123",
                        "subtitles": [{"start": 0, "end": 2, "text": "test"}],
                    }))
                self.assertEqual(ctx1.exception.status_code, 409)

                with self.assertRaises(wf.HTTPException) as ctx2:
                    asyncio.run(wf.api_trigger_separate_vocals({"stem": "worker_inflight_456"}))
                self.assertEqual(ctx2.exception.status_code, 409)

                # 2. Video khác đã xong -> vẫn chỉnh sửa và lưu phụ đề bình thường 100%!
                ok_res = asyncio.run(wf.api_save_workflow_subtitles({
                    "stem": "telegram_completed_999",
                    "subtitles": [{"start": 0, "end": 2, "text": "Sửa bình thường"}],
                }))
                self.assertEqual(ok_res["status"], "ok")

    def test_proxy_worker_yields_gpu_to_cpu_when_main_pipeline_is_active(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src = base / "src.mp4"
            src.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"p" * 4096)
            proxy = base / "proxy_final.mp4"
            cmds = []

            def fake_run(cmd, **kw):
                cmds.append(cmd)
                Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"c" * 4096)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(wf.job_tracker, "get_status", return_value={"active": True, "video_name": "busy.mp4"}), \
                 patch.object(wf.subprocess, "run", side_effect=fake_run):
                wf._proxy_generate_worker(src, proxy, "busy|final")

            self.assertEqual(len(cmds), 1, "Chỉ chạy thẳng 1 lệnh CPU libx264, không đụng NVENC/CUDA khi Tool V1 đang bận")
            self.assertIn("libx264", cmds[0])
            self.assertNotIn("h264_nvenc", cmds[0])
            self.assertTrue(proxy.is_file())


if __name__ == "__main__":
    unittest.main()
