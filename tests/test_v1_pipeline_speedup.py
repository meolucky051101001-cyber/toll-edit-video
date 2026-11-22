"""Comprehensive CPU-only unit & regression tests for Tier 1 + Tier 2 V1 pipeline speedups."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Test 1: Stage 4 min_gap_frames + 1-frame overlap
# ---------------------------------------------------------------------------

def test_refine_observations_min_gap_frames_and_source_overlap(monkeypatch):
    from backend import ocr_utils as ocr
    from backend.visual_caption_timing import refine_observations

    def observe(frame: int) -> str:
        return "a" if frame < 30 else "b"

    samples_default, extra_default = refine_observations(
        {0: "a", 30: "b", 63: "b"}, 64, observe, lambda x: x
    )
    samples_gap2, extra_gap2 = refine_observations(
        {0: "a", 30: "b", 63: "b"}, 64, observe, lambda x: x, min_gap_frames=2
    )
    assert extra_gap2 <= extra_default
    assert 30 in samples_default and 30 in samples_gap2

    samples_mid_default, extra_mid1 = refine_observations(
        {0: "a", 63: "b"}, 64, observe, lambda x: x
    )
    samples_mid_gap2, extra_mid2 = refine_observations(
        {0: "a", 63: "b"}, 64, observe, lambda x: x, min_gap_frames=2
    )
    assert extra_mid2 < extra_mid1
    # Ensure boundary resolved within 2 frames when min_gap_frames=2
    diffs_gap2 = [
        r - l for l, r in zip(sorted(samples_mid_gap2), sorted(samples_mid_gap2)[1:])
        if samples_mid_gap2[l] != samples_mid_gap2[r]
    ]
    assert all(d <= 2 for d in diffs_gap2)

    with pytest.raises(ValueError, match="minimum refinement gap"):
        refine_observations({0: "a", 10: "b"}, 11, observe, lambda x: x, min_gap_frames=0)

    # _perform_visual_subtitle_ocr with source_band & 128 frames (4.0s @ 32fps):
    # Two separated active windows [0.0..0.3] and [3.7..4.0] leave a wide inactive gap (~1.5..2.7s)
    # where overlap must NOT be emitted, while a transition inside window 1 (1 < gap <= 2) DOES emit overlap cleanly.
    fps = 32.0
    frame_count = 128
    duration = 4.0

    class FakeCapture:
        def __init__(self):
            self.index = 0

        def set(self, prop, value):
            self.index = round(value * fps / 1000.0) if prop == cv2.CAP_PROP_POS_MSEC else int(value)

        def get(self, prop):
            return {
                cv2.CAP_PROP_FPS: fps,
                cv2.CAP_PROP_FRAME_COUNT: frame_count,
                cv2.CAP_PROP_POS_FRAMES: self.index,
                cv2.CAP_PROP_POS_MSEC: max(0, self.index - 1) * 1000.0 / fps,
            }.get(prop, 0)

        def read(self):
            frame = np.zeros((320, 180, 3), np.uint8)
            frame[0, 0, 0] = self.index
            self.index += 1
            return True, frame

    def recognize(images):
        out = []
        for frame in images:
            idx = int(frame[0, 0, 0])
            if idx < 17:
                txt = "第一句字幕内容"
            elif idx < 90:
                txt = "第二句完全不同"
            else:
                txt = "第三句尾段字幕"
            out.append([([[20, 240], [160, 240], [160, 270], [20, 270]], txt, 0.99)])
        return out

    source_band = SimpleNamespace(top=0.72, bottom=0.88, support=6, mode="data", selected_by_segment={})
    source_segs = [
        SimpleNamespace(start=timedelta(seconds=0.0), end=timedelta(seconds=0.30), content="第一句字幕内容"),
        SimpleNamespace(start=timedelta(seconds=3.70), end=timedelta(seconds=4.00), content="第三句尾段字幕"),
    ]
    metrics = {}
    monkeypatch.setenv("V1_OCR_SOURCE_MIN_GAP_FRAMES", "2")
    monkeypatch.setenv("V1_OCR_TIMELINE_FAST_REC", "0")
    with patch.object(ocr, "_readtext_batch", side_effect=recognize), \
         patch.object(ocr, "detect_static_watermarks", return_value=[]):
        tracks, _ = ocr._perform_visual_subtitle_ocr(
            FakeCapture(),
            180,
            320,
            duration,
            [],
            lambda: False,
            metrics=metrics,
            source_band=source_band,
            source_segments=source_segs,
        )

    assert metrics["source_timeline"]["min_gap_frames"] == 2
    # Must NEVER emit a bogus overlap track for window 2 caption ("第三句尾段字幕") back into the wide inactive gap (< 2.2s)
    assert not any(t.text == "第三句尾段字幕" and t.start < 2.2 for t in tracks)
    # Near idx 17 (t = 0.53125s), transition is resolved cleanly within 2 frames
    assert any(t.text == "第二句完全不同" and t.start <= (17 / fps + 1e-6) and t.end >= (17 / fps) for t in tracks)


# ---------------------------------------------------------------------------
# Test 2: Stage 4 Tiny Rec & ROI visual-diff skip
# ---------------------------------------------------------------------------

def test_timeline_fast_rec_model_and_roi_diff_skip(tmp_path, monkeypatch):
    from backend import ocr_utils as ocr

    policy = SimpleNamespace(
        ocr_backend="paddle",
        paddle_detection_model="PP-OCRv5_mobile_det",
        paddle_recognition_model="PP-OCRv5_server_rec",
        paddle_engine="onnx",
        runtime_python="python",
        model_cache_directory=str(tmp_path),
    )
    monkeypatch.setenv("V1_OCR_TIMELINE_FAST_REC", "1")
    assert ocr._resolve_timeline_fast_rec_model(policy) is None

    tiny_dir = tmp_path / "paddlex" / "official_models" / "PP-OCRv6_tiny_rec_onnx"
    tiny_dir.mkdir(parents=True)
    assert ocr._resolve_timeline_fast_rec_model(policy) == "PP-OCRv6_tiny_rec"
    # Policy object itself must stay completely unmutated
    assert policy.paddle_recognition_model == "PP-OCRv5_server_rec"

    monkeypatch.setenv("V1_OCR_TIMELINE_FAST_REC", "0")
    assert ocr._resolve_timeline_fast_rec_model(policy) is None

    # ROI diff skip behavior: textured frames vs uniform std==0 frames
    def _run_scan(textured: bool, roi_skip_env: str):
        fps = 30.0
        duration = 2.0
        frame_count = 60

        class FakeCap:
            def __init__(self):
                self.idx = 0

            def set(self, prop, value):
                self.idx = round(value * fps / 1000.0) if prop == cv2.CAP_PROP_POS_MSEC else int(value)

            def get(self, prop):
                return {
                    cv2.CAP_PROP_FPS: fps,
                    cv2.CAP_PROP_FRAME_COUNT: frame_count,
                    cv2.CAP_PROP_POS_FRAMES: self.idx,
                    cv2.CAP_PROP_POS_MSEC: max(0, self.idx - 1) * 1000.0 / fps,
                }.get(prop, 0)

            def read(self):
                if textured:
                    xs = np.linspace(20, 220, 360, dtype=np.uint8)[None, :]
                    frame = np.broadcast_to(xs, (640, 360)).copy()
                    frame = np.stack([frame, frame, frame], axis=-1)
                else:
                    frame = np.zeros((640, 360, 3), dtype=np.uint8)
                frame[0, 0, 0] = self.idx
                if textured and self.idx >= 50:
                    # Distinct subtitle edge change near the end
                    cv2.rectangle(frame, (80, 500), (280, 540), (255, 255, 255), 3)
                self.idx += 1
                return True, frame

        seen_batches = []

        def recognize(images):
            seen_batches.append(len(images))
            return [
                [([[60, 500], [300, 500], [300, 540], [60, 540]], "测试动态字幕", 0.99)]
                for _ in images
            ]

        metrics = {}
        band = SimpleNamespace(top=0.75, bottom=0.88, support=5, mode="data", selected_by_segment={})
        monkeypatch.setenv("V1_OCR_ROI_DIFF_SKIP", roi_skip_env)
        monkeypatch.setenv("V1_OCR_TIMELINE_FAST_REC", "0")
        with patch.object(ocr, "_readtext_batch", side_effect=recognize), \
             patch.object(ocr, "detect_static_watermarks", return_value=[]):
            ocr._perform_visual_subtitle_ocr(
                FakeCap(), 360, 640, duration, [], lambda: False,
                metrics=metrics, source_band=band,
            )
        return metrics

    m_textured = _run_scan(textured=True, roi_skip_env="1")
    assert m_textured.get("visual_roi_diff_skips", 0) >= 2
    assert m_textured.get("visual_requested_frames", 0) < 7

    m_uniform = _run_scan(textured=False, roi_skip_env="1")
    assert m_uniform.get("visual_roi_diff_skips", 0) == 0


# ---------------------------------------------------------------------------
# Test 3: Stage 4 warm OCR & Gatekeeper eviction
# ---------------------------------------------------------------------------

def test_release_ocr_reader_vram_ram_guards_and_gatekeeper_eviction(tmp_path, monkeypatch):
    from backend import ocr_utils as ocr
    from backend import v1_gpu_gatekeeper as gk
    from backend import v1_vram_monitor as vram_mon
    from backend import worker_settings
    from backend.ai import v1_ocr_session, v1_separator_session

    close_calls = []
    monkeypatch.setattr(ocr, "_close_ocr_session", lambda: close_calls.append("closed"))
    monkeypatch.setenv("V1_OCR_KEEP_WARM", "1")
    monkeypatch.setenv("V1_OCR_KEEP_WARM_MIN_VRAM_MB", "1800")
    monkeypatch.setenv("V1_OCR_KEEP_WARM_MIN_RAM_MB", "1500")

    import psutil

    # Case 1: plenty of VRAM and RAM -> stays warm
    monkeypatch.setattr(vram_mon, "get_vram_stats", lambda **kw: {"available": True, "free_mb": 3200.0})
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=4096 * 1024 * 1024))
    ocr.release_ocr_reader(force_close_session=False)
    assert close_calls == []

    # Case 2: low VRAM -> closes session
    monkeypatch.setattr(vram_mon, "get_vram_stats", lambda **kw: {"available": True, "free_mb": 1200.0})
    ocr.release_ocr_reader(force_close_session=False)
    assert len(close_calls) == 1

    # Case 3: high VRAM, low RAM -> closes session
    monkeypatch.setattr(vram_mon, "get_vram_stats", lambda **kw: {"available": True, "free_mb": 3200.0})
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=900 * 1024 * 1024))
    ocr.release_ocr_reader(force_close_session=False)
    assert len(close_calls) == 2

    # Case 4: gpu_gatekeeper evicts idle warm sessions and re-checks VRAM before cooldown sleep
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=8192 * 1024 * 1024))
    evicted = []
    monkeypatch.setattr(v1_ocr_session, "_process", object())
    monkeypatch.setattr(v1_separator_session, "_process", object())
    monkeypatch.setattr(v1_ocr_session, "close_session", lambda: evicted.append("ocr"))
    monkeypatch.setattr(v1_separator_session, "close_session", lambda: evicted.append("sep"))
    monkeypatch.setattr(gk, "GPU_LOCK_FILE", tmp_path / "gpu.lock")
    monkeypatch.setattr(gk, "_CURRENT_HOLDER", None)
    monkeypatch.setattr(gk, "_RECURSION_DEPTH", 0)
    monkeypatch.setattr(gk, "_HELD_HANDLE", None)
    monkeypatch.setattr(worker_settings, "get_worker_settings", lambda: {"auto_vram_guard": True})

    checks = iter([(False, "busy"), (True, "recovered")])
    monkeypatch.setattr(vram_mon, "is_vram_safe_for_gpu_step", lambda min_free_mb=1800.0: next(checks))
    monkeypatch.setattr(gk.time, "sleep", lambda *_: None)

    with gk.gpu_gatekeeper("speech_asr", timeout_seconds=5.0, min_free_mb=1800.0):
        pass
    assert evicted == ["ocr", "sep"]

    # studio_vocal_sep must NOT evict separator session
    evicted.clear()
    checks2 = iter([(False, "busy"), (True, "recovered")])
    monkeypatch.setattr(v1_ocr_session, "_process", object())
    monkeypatch.setattr(v1_separator_session, "_process", object())
    monkeypatch.setattr(vram_mon, "is_vram_safe_for_gpu_step", lambda min_free_mb=1800.0: next(checks2))
    with gk.gpu_gatekeeper("studio_vocal_sep", timeout_seconds=5.0, min_free_mb=1800.0):
        pass
    assert evicted == ["ocr"]


# ---------------------------------------------------------------------------
# Test 4: Stage 3 parallel SenseVoice + shared mono_16k + selective Paraformer
# ---------------------------------------------------------------------------

def test_parallel_sensevoice_and_selective_paraformer(tmp_path, monkeypatch):
    from backend.ai import transcription, v1_paraformer_adapter as pf, v1_sensevoice_adapter as sv

    wav_path = tmp_path / "vocals.wav"
    wav_path.write_bytes(b"RIFFdata")
    out_srt = tmp_path / "out.srt"

    sv_started = threading.Event()
    whisper_started = threading.Event()
    overlap_observed = {"parallel": False}
    shared_arr = np.ones(3200, dtype=np.float32) * 0.25

    def fake_analyze(path):
        sv_started.set()
        if whisper_started.wait(timeout=0.5):
            overlap_observed["parallel"] = True
        return {"windows": [], "tokens": [], "mono_16k": shared_arr}

    def fake_whisper(audio_path, model_name, num_workers, **kw):
        whisper_started.set()
        sv_started.wait(timeout=0.5)
        overlap_observed["parallel"] = True
        return [
            {"start": 0.1, "end": 1.6, "text": "这是一句很长而且非常清晰的中文台词。", "avg_logprob": -0.10},
            {"start": 2.5, "end": 3.1, "text": "对呀", "avg_logprob": -0.15},
        ]

    recorded_refine = {}
    recorded_enrich = {}

    def fake_refine(segs, **kw):
        recorded_refine.update(kw)
        return list(segs)

    def fake_enrich(segs, **kw):
        recorded_enrich.update(kw)
        return list(segs)

    monkeypatch.setenv("V1_ASR_PARALLEL_SENSEVOICE", "1")
    monkeypatch.setenv("V1_PARAFORMER_CO_ASR", "selective")
    monkeypatch.setattr(
        transcription,
        "current_v1_model_policy",
        lambda: SimpleNamespace(
            whisper_candidates=["large-v3-turbo"],
            model_cache_directory=str(tmp_path / "models"),
        ),
    )
    monkeypatch.setattr(sv, "check_sensevoice_readiness", lambda **kw: (True, "ok"))
    monkeypatch.setattr(sv, "analyze_audio_sensevoice", fake_analyze)
    monkeypatch.setattr(sv, "refine_whisper_with_sensevoice", fake_refine)
    monkeypatch.setattr(transcription, "_transcribe_once", fake_whisper)
    monkeypatch.setattr(pf, "check_paraformer_readiness", lambda: (True, "ok"))

    with patch.object(pf, "enrich_segments_with_paraformer", fake_enrich):
        subs = transcription.extract_subtitles_whisper(
            str(wav_path), str(out_srt), model_id="whisper_sensevoice"
        )
    assert len(subs) == 2
    assert overlap_observed["parallel"] is True
    assert recorded_refine.get("precomputed_sv_data") is not None
    assert recorded_enrich.get("preloaded_mono_16k") is shared_arr
    assert recorded_enrich.get("selective") is True

    # Direct test of enrich_segments_with_paraformer(selective=True)
    decode_calls = []
    monkeypatch.setattr(
        pf,
        "_decode_spans_paraformer",
        lambda mono, spans, **kw: (decode_calls.append(list(spans)) or ["改写短句" for _ in spans]),
    )
    monkeypatch.setattr(pf, "_load_mono_16k", lambda p: (_ for _ in ()).throw(AssertionError("should reuse preloaded_mono_16k")))

    high_conf_segs = [
        {"start": 0.1, "end": 1.8, "text": "今天天气真不错我们一起去逛街吧", "avg_logprob": -0.10, "confidence": 0.95},
        {"start": 2.0, "end": 3.5, "text": "这个视频非常精彩值得大家点赞", "avg_logprob": -0.15, "confidence": 0.92},
    ]
    res_skip = pf.enrich_segments_with_paraformer(
        high_conf_segs, wav_path, preloaded_mono_16k=shared_arr, selective=True
    )
    assert len(res_skip) == 2
    assert decode_calls == []

    mixed_segs = [
        {"start": 0.1, "end": 1.8, "text": "今天天气真不错我们一起去逛街吧", "avg_logprob": -0.10, "confidence": 0.95},
        {"start": 2.0, "end": 2.5, "text": "好吧", "avg_logprob": -0.12, "confidence": 0.92},
        {"start": 3.0, "end": 4.5, "text": "模糊不清的一句话", "avg_logprob": -0.55, "confidence": 0.92},
    ]
    res_mixed = pf.enrich_segments_with_paraformer(
        mixed_segs, wav_path, preloaded_mono_16k=shared_arr, selective=True
    )
    assert len(res_mixed) == 3
    assert decode_calls == [[(2.0, 2.5), (3.0, 4.5)]]


# ---------------------------------------------------------------------------
# Test 5: Stage 5 domain keyword fast-path, Gemini order, soft advisory skip
# ---------------------------------------------------------------------------

def test_domain_keyword_fastpath_gemini_order_and_soft_advisory_skip(monkeypatch):
    from backend.ai import translation as tr
    from backend.ai import v1_gemini_dispatcher as disp

    expected_order = [
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite",
    ]
    assert disp.DEFAULT_TRANSLATION_MODELS[:3] == expected_order
    assert disp.DEFAULT_CONDENSATION_MODELS[:3] == expected_order

    monkeypatch.delenv("V1_GEMINI_VISION_DOMAIN_CLASSIFY", raising=False)
    monkeypatch.setattr(
        disp,
        "call_gemini_api",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Remote Gemini API must not be called")),
    )

    assert tr.classify_video_domain([], ["RTX 4070 显卡测试跑分"], api_key="key") == "tech_pc"
    assert tr.classify_video_domain([], ["手帐贴纸DIY教程"], api_key="key") == "craft"
    assert tr.classify_video_domain([], ["今天做一道农村美食"], api_key="key") == "food_life"
    assert tr.classify_video_domain([], ["开箱测评看看性价比"], api_key="key") == "review"
    assert tr.classify_video_domain([], ["霸道总裁现场反转"], api_key="key") == "drama"
    assert tr.classify_video_domain([], ["今天天气真不错"], api_key="key") == "general"

    assert tr._is_repair_worthy_translation_reason("Câu 1: Gợi ý nhỏ về nhịp tự nhiên (cảnh báo)") is False
    assert tr._is_repair_worthy_translation_reason("Câu 1: còn sót chữ Hán (CJK)") is True
    assert tr._is_repair_worthy_translation_reason("Câu 1: vi phạm thuật ngữ glossary") is True

    # Verify targeted repair is skipped on soft-only reasons under warn policy, and triggered on hard reason
    llm_calls = []

    def fake_gemini_batch(texts, *a, **kw):
        llm_calls.append(list(texts))
        return ["Xin chào các bạn." for _ in texts]

    import srt as _srt
    monkeypatch.setenv("V1_TRANSLATION_MEANING_POLICY", "warn")
    monkeypatch.setattr(tr, "translate_with_gemini", fake_gemini_batch)
    monkeypatch.setattr(tr, "classify_video_domain", lambda *a, **k: "general")
    monkeypatch.setattr(tr, "resolve_effective_glossary", lambda *a, **k: {})

    seg_list = [_srt.Subtitle(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="你好朋友们")]

    # Soft-only quality failure -> NO extra LLM retry
    llm_calls.clear()
    with patch.object(
        tr,
        "validate_translation_batch_quality",
        return_value=(False, ["Câu 1: Gợi ý tham khảo sắc thái (warn)"]),
    ):
        out_soft = tr.translate_subtitles(
            [ _srt.Subtitle(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="你好朋友们") ],
            "vi",
            api_key="AIzaTestKey1234567890",
            strict=True,
        )
    assert [s.content for s in out_soft] == ["Xin chào các bạn"]
    assert len(llm_calls) == 1

    # Hard quality failure (e.g. CJK leak) -> DOES trigger targeted LLM retry
    llm_calls.clear()
    side_effects = [
        (False, ["Câu 1: còn sót chữ Hán (CJK)"]),
        (True, []),
    ]
    with patch.object(tr, "validate_translation_batch_quality", side_effect=side_effects):
        tr.translate_subtitles(
            seg_list,
            "vi",
            api_key="AIzaTestKey1234567890",
            strict=True,
        )
    assert len(llm_calls) == 2


# ---------------------------------------------------------------------------
# Test 6: Orchestrator async notify + manifest scan cap
# ---------------------------------------------------------------------------

def test_orchestrator_async_notify_nonblocking_cancellation_and_scan_limit(tmp_path, monkeypatch):
    from backend.v1_orchestrator import V1Orchestrator

    async def _run_case_a():
        root = tmp_path / "case_a"
        root.mkdir()
        src = root / "in.mp4"
        src.write_bytes(b"fixture" * 100)
        orch = V1Orchestrator(root)
        notify_calls = []

        async def slow_progress(stage, step, total, pct, msg, details):
            notify_calls.append(stage)
            if stage != "publish":
                await asyncio.sleep(0.25)

        monkeypatch.setattr(orch, "_prepare_execution", lambda *a, **k: SimpleNamespace(
            job_id="job1", resolved_mode="SHORT", total_steps=8, video_path=src,
            output_dir=root / "work", delivery_path=root / "del.mp4",
            effective_cfg={}, overrides=None, planned={}, visual_sub_to_speech=False,
            manifest=SimpleNamespace(stages={}),
        ))
        monkeypatch.setattr(orch, "_restore_execution_checkpoint", lambda c, r: None)

        async def _emit_and_return(c, val):
            await c.notify("stage_x", 1, 10, "msg", {})
            return val

        monkeypatch.setattr(orch, "_stage_extract", lambda c: _emit_and_return(c, root / "a.wav"))
        monkeypatch.setattr(orch, "_stage_separate", lambda c, a: _emit_and_return(c, (root / "v.wav", root / "nv.wav")))
        monkeypatch.setattr(orch, "_stage_asr", lambda c, v, a: _emit_and_return(c, (root / "o.srt", [], False)))
        monkeypatch.setattr(orch, "_stage_ocr", lambda c, s, seg, e: _emit_and_return(c, ([], [], 1080, 1920, 0.85)))
        monkeypatch.setattr(orch, "_stage_translate", lambda c, seg, **kw: _emit_and_return(c, ([], "default")))
        monkeypatch.setattr(orch, "_stage_tts", lambda c, *a: _emit_and_return(c, ([], [], root / "f.ass")))
        monkeypatch.setattr(orch, "_stage_mix", lambda c, *a: _emit_and_return(c, root / "m.wav"))
        monkeypatch.setattr(orch, "_stage_render", lambda c, *a: _emit_and_return(c, (root / "r.mp4", root / "tr.mp4")))

        async def _qc_pub(c, *a):
            await c.notify("quality_gate", 8, 95, "qc", {})
            await c.notify("publish", 8, 100, "done", {})
            return {"status": "success"}

        monkeypatch.setattr(orch, "_stage_qc_and_deliver", _qc_pub)

        t0 = time.monotonic()
        res = await orch.execute_job(
            src, "job1", output_dir=root / "work", delivery_path=root / "del.mp4",
            progress_callback=slow_progress,
        )
        elapsed = time.monotonic() - t0
        assert res["status"] == "success"
        # 8 slow 0.25s notifications coalesced + non-blocking: must take far less than 8 * 0.25 = 2.0s
        assert elapsed < 0.85
        assert "quality_gate" in notify_calls and "publish" in notify_calls

    asyncio.run(_run_case_a())

    async def _run_case_b():
        root = tmp_path / "case_b"
        root.mkdir()
        src = root / "in.mp4"
        src.write_bytes(b"fixture" * 100)
        orch = V1Orchestrator(root)

        async def cancelling_progress(stage, step, total, pct, msg, details):
            raise asyncio.CancelledError("stop UI")

        monkeypatch.setattr(orch, "_prepare_execution", lambda *a, **k: SimpleNamespace(
            job_id="job2", resolved_mode="SHORT", total_steps=8, video_path=src,
            output_dir=root / "work", delivery_path=root / "del.mp4",
            effective_cfg={}, overrides=None, planned={}, visual_sub_to_speech=False,
            manifest=SimpleNamespace(stages={}),
        ))
        monkeypatch.setattr(orch, "_restore_execution_checkpoint", lambda c, r: None)
        stage_after = []

        async def _s1(c):
            await c.notify("audio_extraction", 1, 10, "extract", {})
            return root / "a.wav"

        async def _s2(c, a):
            stage_after.append("separate")
            await c.notify("vocal_separation", 2, 20, "sep", {})
            return (root / "v.wav", root / "nv.wav")

        monkeypatch.setattr(orch, "_stage_extract", _s1)
        monkeypatch.setattr(orch, "_stage_separate", _s2)
        with pytest.raises(asyncio.CancelledError):
            await orch.execute_job(
                src, "job2", output_dir=root / "work", delivery_path=root / "del.mp4",
                progress_callback=cancelling_progress,
            )
        assert stage_after == []

    asyncio.run(_run_case_b())

    # Case C: V1_RESUME_MANIFEST_SCAN_LIMIT bounds manifest reads in _prepare_execution
    root_c = tmp_path / "case_c"
    root_c.mkdir()
    src_c = root_c / "vid.mp4"
    src_c.write_bytes(b"video" * 500)
    orch_c = V1Orchestrator(root_c)
    mdir = orch_c.manifest_manager.manifest_dir
    mdir.mkdir(parents=True, exist_ok=True)
    for i in range(18):
        p = mdir / f"old_{i:02d}.manifest.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))

    loaded_ids = []
    real_load = orch_c.manifest_manager.load_manifest

    def counting_load(jid):
        loaded_ids.append(jid)
        if jid == "fresh_job" and len(loaded_ids) == 1:
            return None
        return None

    monkeypatch.setenv("V1_RESUME_MANIFEST_SCAN_LIMIT", "10")
    monkeypatch.setattr(orch_c.manifest_manager, "load_manifest", counting_load)
    monkeypatch.setattr(orch_c, "plan_job", lambda *a, **k: {})
    with pytest.raises(RuntimeError, match="manifest"):
        orch_c._prepare_execution(
            src_c, "fresh_job", root_c / "work", root_c / "del.mp4",
            "SHORT", {}, resume_if_possible=True,
        )
    # 1 initial check + 10 candidate checks + 1 post-plan_job check = 12 (definitely not all 20)
    assert len(loaded_ids) == 12
    # Should have checked newest 10 (old_17 down to old_08)
    assert "old_17" in loaded_ids and "old_00" not in loaded_ids


# ---------------------------------------------------------------------------
# Test 7: Stage 2 warm separator session & CPU offload
# ---------------------------------------------------------------------------

def test_separator_warm_session_reuse_teardown_ram_guard_and_cpu_offload(tmp_path, monkeypatch):
    from backend.ai import v1_separator_session as sep_sess
    from backend.model_workers import v1_separator_worker as sep_worker
    import psutil

    sep_sess.close_session()
    try:
        spawned = []
        taskkill_calls = []

        class FakeStdin:
            def __init__(self, proc):
                self.proc = proc
                self.closed = False

            def write(self, line):
                req_meta = json.loads(line.strip())
                res_p = Path(req_meta["response"])
                if not self.proc.hang:
                    res_p.write_text(json.dumps({"success": True, "call": self.proc.calls + 1}), encoding="utf-8")
                    self.proc.calls += 1

            def flush(self):
                pass

            def close(self):
                self.closed = True

        class FakeSepProc:
            pid = 999999

            def __init__(self, hang=False):
                self.hang = hang
                self.calls = 0
                self.stdin = FakeStdin(self)
                self.stderr = io.StringIO("")
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        hang_mode = {"flag": False}

        def fake_popen(cmd, **kw):
            proc = FakeSepProc(hang=hang_mode["flag"])
            spawned.append((list(cmd), proc))
            return proc

        monkeypatch.setattr(sep_sess.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(
            sep_sess.subprocess,
            "run",
            lambda cmd, **kw: (taskkill_calls.append(list(cmd)) or SimpleNamespace(returncode=0)),
        )
        monkeypatch.setenv("V1_SEPARATOR_KEEP_WARM_MIN_RAM_MB", "2000")
        monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=4096 * 1024 * 1024))

        payload = {"input_audio": "a.wav", "output_dir": str(tmp_path), "model_dir": str(tmp_path)}
        r1 = sep_sess.run_separator_request("python", "worker.py", payload, timeout=5.0)
        r2 = sep_sess.run_separator_request("python", "worker.py", payload, timeout=5.0)
        assert len(spawned) == 1
        assert spawned[0][0][-1] == "--serve"
        assert r1["success"] is True and r2["success"] is True

        # Low RAM triggers warm session eviction automatically in finally
        monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=1200 * 1024 * 1024))
        sep_sess.run_separator_request("python", "worker.py", payload, timeout=5.0)
        assert sep_sess._process is None

        # High RAM + check_stop abort closes session and terminates worker
        monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=4096 * 1024 * 1024))
        hang_mode["flag"] = True

        def abort_stop():
            raise RuntimeError("stop requested")

        sep_sess.close_session()
        spawned.clear()
        called = [0]

        def delayed_stop():
            called[0] += 1
            if called[0] >= 5:
                raise RuntimeError("cancelled mid-wait")

        with pytest.raises(RuntimeError, match="cancelled"):
            sep_sess.run_separator_request("python", "worker.py", payload, timeout=5.0, check_stop=delayed_stop)
        assert sep_sess._process is None
        assert spawned[0][1].terminated is True

        # Timeout tears down process tree and raises TimeoutError
        hang_mode["flag"] = True
        mono_vals = iter([0.0, 0.0, 0.0, 0.0, 10.0])
        monkeypatch.setattr(sep_sess.time, "monotonic", lambda: next(mono_vals, 10.0))
        with pytest.raises(TimeoutError):
            sep_sess.run_separator_request("python", "worker.py", payload, timeout=1.0)
        assert sep_sess._process is None
    finally:
        sep_sess.close_session()

    # Worker CPU offload & GPU restore helpers
    events = []

    class FakeModelRun:
        def to(self, dev):
            events.append(("to", dev))
            return self

        def eval(self):
            events.append(("eval",))

    fake_mi = SimpleNamespace(
        model_run=FakeModelRun(),
        torch_device="cuda",
        output_dir="old_dir",
        primary_source="cached_tensor",
        secondary_source="cached_tensor",
        clear_gpu_cache=lambda: events.append(("clear_gpu_cache",)),
    )
    fake_sep = SimpleNamespace(output_dir="old_dir", torch_device="cuda", model_instance=fake_mi)
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            empty_cache=lambda: events.append(("empty_cache",)),
        )
    )

    sep_worker._offload_separator_to_cpu(fake_sep, fake_torch)
    assert ("to", "cpu") in events
    assert ("clear_gpu_cache",) in events
    assert fake_mi.primary_source is None
    assert fake_mi.secondary_source is None
    assert ("empty_cache",) in events

    events.clear()
    sep_worker._restore_separator_to_gpu(fake_sep, "new_out_dir")
    assert fake_sep.output_dir == "new_out_dir"
    assert fake_mi.output_dir == "new_out_dir"
    assert ("to", "cuda") in events
    assert ("eval",) in events


# ---------------------------------------------------------------------------
# Test 8: Stage 8 CPU in-place blur accuracy + 3-stage pipeline + NVENC preset
# ---------------------------------------------------------------------------

def test_cpu_inplace_blur_accuracy_3stage_pipeline_stop_and_nvenc_preset(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    from backend import batch_control
    from backend import v1_gpu_subtitle_blur as gpu_blur
    from backend import video_utils
    import torch

    # 8A. Pixel accuracy on a strided ROI slice vs FFmpeg boxblur (when ffmpeg available)
    fh, fw = 120, 360
    yy, xx = np.indices((fh, fw))
    base_plane = ((xx * 23 + yy * 17) % 220 + 16).astype(np.uint8)
    plane_copy = base_plane.copy()
    rx, ry, rw, rh = 32, 24, 296, 66
    gpu_blur._boxblur_plane_inplace_cv2(plane_copy[ry:ry + rh, rx:rx + rw], radius=12, power=3)

    # Outside pixels must be 100% bit-exact
    assert np.array_equal(plane_copy[:ry, :], base_plane[:ry, :])
    assert np.array_equal(plane_copy[ry + rh:, :], base_plane[ry + rh:, :])
    assert np.array_equal(plane_copy[:, :rx], base_plane[:, :rx])
    assert np.array_equal(plane_copy[:, rx + rw:], base_plane[:, rx + rw:])

    if shutil.which("ffmpeg"):
        roi_orig = base_plane[ry:ry + rh, rx:rx + rw]
        raw_in = tmp_path / "roi_in.yuv"
        raw_out = tmp_path / "roi_out.yuv"
        raw_in.write_bytes(roi_orig.tobytes() + bytes([128]) * (rw * rh // 2))
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{rw}x{rh}", "-i", str(raw_in),
                "-vf", "boxblur=luma_radius=12:luma_power=3:chroma_radius=6:chroma_power=3",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", str(raw_out),
            ],
            check=True,
        )
        ff_roi = np.frombuffer(raw_out.read_bytes()[: rw * rh], dtype=np.uint8).reshape(rh, rw)
        diff = np.abs(plane_copy[ry:ry + rh, rx:rx + rw].astype(int) - ff_roi.astype(int))
        assert int(diff.max()) <= 2
        assert float(diff.mean()) < 0.6

    # 8B. 3-stage double-buffered pipeline end-to-end (CPU-only mock)
    bw, bh = 320, 240
    n_frames = 30
    y_sz = bw * bh
    uv_sz = (bw // 2) * (bh // 2)
    fbytes = y_sz + 2 * uv_sz

    # Build source frames with distinct textured pattern in subtitle box [80:240, 180:220]
    src_buf = bytearray()
    for fi in range(n_frames):
        y = np.full((bh, bw), 40, dtype=np.uint8)
        # Checker stripes inside box so boxblur materially changes pixel values
        y[180:220, 80:240:2] = 235
        u = np.full((bh // 2, bw // 2), 128, dtype=np.uint8)
        v = np.full((bh // 2, bw // 2), 128, dtype=np.uint8)
        src_buf.extend(y.tobytes())
        src_buf.extend(u.tobytes())
        src_buf.extend(v.tobytes())

    captured_out = bytearray()
    out_mp4 = tmp_path / "out.mp4"

    class _ReadIntoPipe(io.BytesIO):
        def readinto(self, b):
            data = self.read(len(b))
            n = len(data)
            b[:n] = data
            return n

    class _FakeDecProc:
        pid = 999991

        def __init__(self, data: bytes):
            self.stdout = _ReadIntoPipe(data)
            self.stderr = io.BytesIO(b"")
            self.stdin = None
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    class _EncStdin:
        def __init__(self, sink: bytearray, out_path: Path):
            self.sink = sink
            self.out_path = out_path
            self.closed = False

        def write(self, data):
            self.sink.extend(data)
            return len(data)

        def close(self):
            self.closed = True
            self.out_path.write_bytes(b"MP4DATA" * 3000)

    class _FakeEncProc:
        pid = 999992

        def __init__(self, sink: bytearray, out_path: Path):
            self.stdin = _EncStdin(sink, out_path)
            self.stdout = None
            self.stderr = io.BytesIO(b"")
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_blur_popen(cmd, **kw):
        if "-rawvideo" in cmd or (isinstance(cmd, list) and "rawvideo" in cmd and kw.get("stdout") == subprocess.PIPE):
            return _FakeDecProc(bytes(src_buf))
        if kw.get("stdin") == subprocess.PIPE:
            return _FakeEncProc(captured_out, out_mp4)
        return _FakeDecProc(bytes(src_buf))

    monkeypatch.setenv("V1_BLUR_CPU_INPLACE", "1")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(gpu_blur, "_probe_sorted_packet_pts", lambda *a: [i / 30.0 for i in range(n_frames)])
    monkeypatch.setattr(gpu_blur, "validate_video_output", lambda *a, **kw: None)
    monkeypatch.setattr(gpu_blur, "_kill_proc_tree", lambda proc: (proc.stdin.close() if getattr(proc, "stdin", None) else None))
    monkeypatch.setattr(gpu_blur.subprocess, "Popen", fake_blur_popen)

    src_info = SimpleNamespace(index=0, input_map="0:0", fps=30.0, width=bw, height=bh, duration=1.0, has_cover=False)
    boxes = [{"start": 0.0, "end": 1.0, "x_pct": 0.25, "max_x_pct": 0.75, "y_pct": 0.75, "max_y_pct": 0.9166}]
    ok, reason = gpu_blur.render_video_with_gpu_localized_blur(
        video_path=str(tmp_path / "src.mp4"),
        mixed_audio_path=str(tmp_path / "mix.wav"),
        output_video_path=str(out_mp4),
        source_video=src_info,
        blur_boxes=boxes,
        blur_settings={"sub_blur_radius": 8, "sub_blur_padding_x": 0.0, "sub_blur_padding_y": 0.0},
        blur_width=bw,
        blur_height=bh,
        output_width=bw,
        output_height=bh,
        pre_blur_vf="",
        post_blur_vf="",
        enc_args=["h264_nvenc", "-preset", "p3", "-b:v", "2500k"],
    )
    assert ok, reason
    assert len(captured_out) == n_frames * fbytes
    first_out_y = np.frombuffer(bytes(captured_out[:y_sz]), dtype=np.uint8).reshape(bh, bw)
    # Top non-blur region unchanged (all 40), inside subtitle region blurred (< 235 stripes smoothed)
    assert np.all(first_out_y[:150, :] == 40)
    assert int(first_out_y[190:210, 90:230].max()) < 235

    # 8C. Stop propagation in 3-stage pipeline without thread hang
    calls = [0]

    def stop_pred():
        calls[0] += 1
        return calls[0] >= 2

    tok = batch_control.stop_check.set(stop_pred)
    try:
        with pytest.raises(RuntimeError, match="Batch stop requested"):
            gpu_blur.render_video_with_gpu_localized_blur(
                video_path=str(tmp_path / "src.mp4"),
                mixed_audio_path=str(tmp_path / "mix.wav"),
                output_video_path=str(tmp_path / "stopped.mp4"),
                source_video=src_info,
                blur_boxes=boxes,
                blur_settings={"sub_blur_radius": 8},
                blur_width=bw,
                blur_height=bh,
                output_width=bw,
                output_height=bh,
                pre_blur_vf="",
                post_blur_vf="",
                enc_args=["h264_nvenc", "-preset", "p3", "-b:v", "2500k"],
            )
    finally:
        batch_control.stop_check.reset(tok)

    # 8D. V1_NVENC_PRESET defaults to p3 (without -tune hq), and honors p4 (with -tune hq)
    from backend import v1_media_streams

    src_vid = tmp_path / "v_in.mp4"
    src_srt = tmp_path / "v_sub.srt"
    src_mix = tmp_path / "v_mix.wav"
    src_out = tmp_path / "v_out.mp4"
    src_vid.write_bytes(b"V" * 25000)
    src_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chao\n", encoding="utf-8")
    src_mix.write_bytes(b"A" * 5000)

    recorded_cmds = []

    def fake_run_batch(cmd, **kw):
        recorded_cmds.append(list(cmd))
        src_out.write_bytes(b"O" * 20000)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_utils, "clean_stale_temp_files", lambda **kw: None)
    monkeypatch.setattr(video_utils, "run_batch_subprocess", fake_run_batch)
    monkeypatch.setattr(
        v1_media_streams,
        "probe_main_video",
        lambda p: SimpleNamespace(index=0, input_map="0:0", width=1080, height=1920, duration=5.0, fps=30.0, has_cover=False),
    )
    monkeypatch.setattr(v1_media_streams, "validate_video_output", lambda *a, **kw: None)

    monkeypatch.delenv("V1_NVENC_PRESET", raising=False)
    monkeypatch.setenv("V1_SUBTITLE_COVER_STYLE", "solid_box")
    assert video_utils.process_video(str(src_vid), str(src_srt), str(src_mix), str(src_out)) is True
    nvenc_idx = recorded_cmds[-1].index("h264_nvenc")
    nvenc_slice = recorded_cmds[-1][nvenc_idx:]
    assert nvenc_slice[1:3] == ["-preset", "p3"]
    assert "-tune" not in nvenc_slice

    monkeypatch.setenv("V1_NVENC_PRESET", "p4")
    assert video_utils.process_video(str(src_vid), str(src_srt), str(src_mix), str(src_out)) is True
    nvenc_idx2 = recorded_cmds[-1].index("h264_nvenc")
    nvenc_slice2 = recorded_cmds[-1][nvenc_idx2:]
    assert nvenc_slice2[1:5] == ["-preset", "p4", "-tune", "hq"]
