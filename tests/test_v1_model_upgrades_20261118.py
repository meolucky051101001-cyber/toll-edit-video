"""Regression & contract tests for Laptop-Optimized Model & Workflow Upgrades (2026-11-18).

Covers:
1. Stage 1.5 Silero VAD + SenseVoice speech-presence gate + whispered-speech peak normalization
   + Orchestrator fail-safe recovery when Visual OCR finds no usable dynamic captions.
2. Stage 3 Paraformer-large-zh + CT-Transformer standalone mode (with NonChineseAudioError fallback)
   + co-recognition `paraformer_alt` enrichment -> `asr_alt` translation metadata.
3. Stage 4 & Stage 9 Hybrid `PP-OCRv5_server_rec` auto-selection when cached on disk,
   verified-interval `build_ocr_hint` for pre-speech captions, and disjoint vertical transition QC.
4. Stage 5/6 Voice Lock vocal-tract spectral formant disambiguation (`158-208 Hz`) + `CAM++` 512-D
   speaker verification on dual-speaker dialogues.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import srt

from backend.ai.v1_caption_evidence import build_ocr_hint, translation_cue_meta
from backend.ai.v1_model_policy import V1ModelPolicy
from backend.ai.v1_paraformer_adapter import (
    NonChineseAudioError,
    _clean_paraformer_cjk_spacing,
    enrich_segments_with_paraformer,
)
from backend.ai.v1_speaker_embedding import (
    cosine_similarity,
    refine_ambiguous_pitch_gender,
)
from backend.ai.v1_vad_gate import VADGateResult, analyze_speech_presence_vad
from backend.v1_quality_gate import _transition_pixel_candidates


def test_model_policy_selects_pp_ocrv5_server_rec_only_when_model_dir_present(tmp_path):
    # Isolated temp root without server_rec stays on PP-OCRv6_tiny_rec
    p_default = V1ModelPolicy.from_env({}, project_root=tmp_path)
    assert p_default.paddle_detection_model == "PP-OCRv6_tiny_det"
    assert p_default.paddle_recognition_model == "PP-OCRv6_tiny_rec"

    # When PP-OCRv5_server_rec_onnx exists in local model cache, auto-upgrade recognition
    server_dir = tmp_path / "models" / "v1" / "paddlex" / "official_models" / "PP-OCRv5_server_rec_onnx"
    server_dir.mkdir(parents=True)
    p_server = V1ModelPolicy.from_env({}, project_root=tmp_path)
    assert p_server.paddle_detection_model == "PP-OCRv6_tiny_det"
    assert p_server.paddle_recognition_model == "PP-OCRv5_server_rec"


def test_disjoint_vertical_tracks_do_not_create_fullscreen_transition_roi():
    # Top banner [0.20..0.28] transitioning to bottom subtitle [0.71..0.81] at t=11.77s
    # must NOT union vertically into [0.20..0.81] (which falsely flags center-screen product text).
    seg = SimpleNamespace(
        index=1,
        tracking_blocks=[
            SimpleNamespace(
                text="收纳袋", start=10.0, end=11.77,
                x_pct=0.25, y_pct=0.20, max_x_pct=0.75, max_y_pct=0.28,
                is_static=False, is_packaging=False,
            ),
            SimpleNamespace(
                text="超好用推荐", start=11.77, end=13.50,
                x_pct=0.20, y_pct=0.71, max_x_pct=0.80, max_y_pct=0.81,
                is_static=False, is_packaging=False,
            ),
        ],
    )
    candidates, _ = _transition_pixel_candidates([seg], fps=30.0)
    assert candidates
    heights = [c["box"][3] - c["box"][1] for c in candidates]
    assert all(h < 0.20 for h in heights)

    # Adjacent/overlapping lines at transition DO still union cleanly
    adj_seg = SimpleNamespace(
        index=1,
        tracking_blocks=[
            SimpleNamespace(
                text="第一行", start=10.0, end=11.77,
                x_pct=0.20, y_pct=0.70, max_x_pct=0.80, max_y_pct=0.76,
                is_static=False, is_packaging=False,
            ),
            SimpleNamespace(
                text="第二行", start=11.77, end=13.50,
                x_pct=0.18, y_pct=0.75, max_x_pct=0.82, max_y_pct=0.82,
                is_static=False, is_packaging=False,
            ),
        ],
    )
    adj_candidates, _ = _transition_pixel_candidates([adj_seg], fps=30.0)
    assert any(c["box"] == (0.18, 0.70, 0.82, 0.82) for c in adj_candidates)


def test_verified_interval_ocr_hint_keeps_prespeech_caption_and_paraformer_alt():
    seg = srt.Subtitle(
        index=1,
        start=timedelta(seconds=1.28),
        end=timedelta(seconds=3.32),
        content="今天在木吉发现了一款存钱本",
    )
    seg.source_caption_timing_verified = True
    # Caption appeared at t=1.10s (before speech at 1.28s) and stayed until 2.20s;
    # previously sample_time=1.10 < 1.28 dropped this verified caption from ocr_hint!
    seg.tracking_blocks = [
        SimpleNamespace(
            text="今天在MUJI发现了",
            start=1.10,
            end=2.20,
            sample_time=1.10,
            prob=0.98,
            visual_locked=True,
            is_subtitle=True,
            in_subtitle_band=True,
            is_packaging=False,
            is_static=False,
        ),
        SimpleNamespace(
            text="一款超棒存钱本",
            start=2.20,
            end=3.30,
            sample_time=2.20,
            prob=0.99,
            visual_locked=True,
            is_subtitle=True,
            in_subtitle_band=True,
            is_packaging=False,
            is_static=False,
        ),
    ]
    seg.paraformer_alt = "今天在muji发现了一款存钱本。"

    assert build_ocr_hint(seg) == "今天在MUJI发现了 / 一款超棒存钱本"
    meta = translation_cue_meta([seg], [{"id": 1, "speaker": "female", "max_words": 12}])[0]
    assert meta["ocr_hint"] == "今天在MUJI发现了 / 一款超棒存钱本"
    assert meta["asr_alt"] == "今天在muji发现了一款存钱本。"


def test_paraformer_cjk_spacing_and_non_chinese_fallback(tmp_path, monkeypatch):
    import soundfile as sf
    from backend.ai import transcription as tr

    assert _clean_paraformer_cjk_spacing("今 天 在 MUJI 买 了 5 张 收 纳 袋") == "今天在MUJI买了5张收纳袋"

    wav_path = tmp_path / "en.wav"
    srt_path = tmp_path / "out.srt"
    sf.write(str(wav_path), np.zeros(16000, dtype=np.float32), 16000)

    monkeypatch.setattr(
        "backend.ai.v1_paraformer_adapter.check_paraformer_readiness",
        lambda: (True, "ready"),
    )

    def _raise_non_zh(*args, **kwargs):
        raise NonChineseAudioError("English speech detected")

    monkeypatch.setattr(
        "backend.ai.v1_paraformer_adapter.transcribe_audio_paraformer",
        _raise_non_zh,
    )
    monkeypatch.setattr(
        tr,
        "current_v1_model_policy",
        lambda: SimpleNamespace(
            model_cache_directory=str(tmp_path),
            whisper_candidates=("turbo",),
        ),
    )
    monkeypatch.setattr(
        tr,
        "_transcribe_once",
        lambda *a, **kw: [{"start": 0.0, "end": 1.2, "text": "Hello everyone"}],
    )

    subs = tr.extract_subtitles_whisper(str(wav_path), str(srt_path), model_id="paraformer_zh")
    assert len(subs) == 1
    assert subs[0].content == "Hello everyone"


def test_paraformer_co_recognition_enrichment_preserves_whisper_text(tmp_path, monkeypatch):
    import soundfile as sf
    import backend.ai.v1_paraformer_adapter as pf

    wav_path = tmp_path / "speech.wav"
    sf.write(str(wav_path), np.zeros(32000, dtype=np.float32), 16000)

    class _FakeStream:
        def accept_waveform(self, sr, samples):
            pass

        @property
        def result(self):
            return SimpleNamespace(text="今天买存钱袋")

    class _FakeRec:
        def create_stream(self):
            return _FakeStream()

        def decode_streams(self, streams):
            pass

    monkeypatch.setattr(pf, "check_paraformer_readiness", lambda: (True, "ok"))
    monkeypatch.setattr(pf, "_get_recognizer", lambda: _FakeRec())
    monkeypatch.setattr(pf, "_get_punctuator", lambda: None)

    segs = [{"start": 0.1, "end": 1.8, "text": "今天买存钱带"}]
    enriched = enrich_segments_with_paraformer(segs, str(wav_path))
    assert enriched[0]["text"] == "今天买存钱带"  # Whisper primary text untouched
    assert enriched[0]["paraformer_alt"] == "今天买存钱袋"


def test_vad_gate_digital_silence_and_orchestrator_failsafe(tmp_path):
    import soundfile as sf
    from backend.v1_orchestrator import V1Orchestrator, _ExecutionContext

    silent_wav = tmp_path / "silent.wav"
    sf.write(str(silent_wav), np.zeros(16000 * 3, dtype=np.float32), 16000)
    res = analyze_speech_presence_vad(str(silent_wav))
    assert res.has_speech is False
    assert res.reason == "digital_silence"

    # Verify Orchestrator fail-safe: if VAD bypassed Stage 2, and Visual OCR finds no captions,
    # Stage 3 automatically runs real vocal separation + ASR instead of losing speech!
    orch = V1Orchestrator(workspace_path=tmp_path)
    out_dir = tmp_path / "job_vad"
    out_dir.mkdir(parents=True)
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"fake-mp4")

    orch.manifest_manager.get_or_create_manifest("job_vad", str(video_file), "SHORT", {}, {})

    async def _notify(*args, **kwargs):
        pass

    async def _pause():
        pass

    async def _run_stage(coro, timeout=None):
        return await coro

    ctx = _ExecutionContext(
        v_path=video_file,
        job_id="job_vad",
        out_dir=out_dir,
        manifest=orch.manifest_manager.load_manifest("job_vad"),
        effective_cfg={
            "target_lang": "vi",
            "visual_sub_to_speech": True,
            "feature_flags": {"V1_VAD_SMART_BYPASS": True, "V1_VISUAL_SUB_TO_SPEECH": True},
        },
        planned={"asr_model": "whisper_turbo", "asr_chunking": False, "asr_chunk_size_s": 240.0, "asr_overlap_s": 0.75},
        resolved_mode="SHORT",
        overrides={},
        delivery_path=None,
        t_job_start=0.0,
        base_name="clip",
        visual_sub_to_speech=True,
        can_resume=False,
        verified_stages=[],
        video_dur_s=3.0,
        artifacts_acc={},
        notify=_notify,
        check_stop=lambda: None,
        pause_checkpoint=_pause,
        run_stage=_run_stage,
    )

    with patch(
        "ai.v1_vad_gate.analyze_speech_presence_vad",
        return_value=VADGateResult(False, 0.0, 3.0, 0.0, 0, -45.0, 0.05, "bgm_or_sfx_only_no_vocals"),
    ):
        voc, novoc = asyncio.run(orch._stage_separate(ctx, silent_wav))
    assert getattr(ctx, "_vad_bypassed_separation", False) is True
    assert voc == silent_wav and novoc == silent_wav

    recovered_voc = tmp_path / "real_vocals.wav"
    recovered_novoc = tmp_path / "real_no_vocals.wav"
    sf.write(str(recovered_voc), np.zeros(16000, dtype=np.float32), 16000)
    sf.write(str(recovered_novoc), np.zeros(16000, dtype=np.float32), 16000)

    def _fake_sep(audio_in, out_d, separation_mode="roformer"):
        return str(recovered_voc), str(recovered_novoc)

    def _fake_asr(audio_in, srt_out, **kwargs):
        sub = srt.Subtitle(index=1, start=timedelta(0), end=timedelta(seconds=1.5), content="你好世界")
        Path(srt_out).write_text(srt.compose([sub]), encoding="utf-8")
        return [sub]

    with patch.object(orch, "_stage_visual_transcript", side_effect=RuntimeError("no visual subs")), \
         patch.object(orch, "_check_gpu_guard"), \
         patch("video_utils.separate_vocals_demucs", side_effect=_fake_sep) as sep_mock, \
         patch("ai.v1_asr_isolated.extract_subtitles_isolated", side_effect=_fake_asr) as asr_mock:
        srt_p, segs, was_empty = asyncio.run(orch._stage_asr(ctx, voc, silent_wav))

    assert sep_mock.call_count == 1
    assert asr_mock.call_count == 1
    assert was_empty is False
    assert len(segs) == 1 and segs[0].content == "你好世界"
    assert ctx._recovered_vocals_audio == recovered_voc


def test_speaker_formant_disambiguation_and_cosine_similarity(monkeypatch):
    import backend.ai.v1_speaker_embedding as spk

    monkeypatch.setattr(spk, "extract_speaker_embedding", lambda *a, **kw: None)
    sr = 16000
    t = np.linspace(0, 1.2, int(sr * 1.2), endpoint=False)
    # Borderline F0 = 183 Hz with strong upper-formant energy (1200-3400 Hz -> shorter vocal tract / female)
    y_alto_female = (
        0.25 * np.sin(2 * np.pi * 320.0 * t)
        + 0.55 * np.sin(2 * np.pi * 2100.0 * t)
        + 0.45 * np.sin(2 * np.pi * 2900.0 * t)
    ).astype(np.float32)
    gen_f, conf_f = refine_ambiguous_pitch_gender(y_alto_female, sr, 183.0, "unknown", 0.45)
    assert gen_f == "female" and conf_f >= 0.72

    # Borderline F0 = 176 Hz with strong chest resonance (300-750 Hz -> longer vocal tract / male)
    y_high_male = (
        0.70 * np.sin(2 * np.pi * 350.0 * t)
        + 0.55 * np.sin(2 * np.pi * 550.0 * t)
        + 0.05 * np.sin(2 * np.pi * 1500.0 * t)
    ).astype(np.float32)
    gen_m, conf_m = refine_ambiguous_pitch_gender(y_high_male, sr, 176.0, "unknown", 0.45)
    assert gen_m == "male" and conf_m >= 0.72

    v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.8, 0.6, 0.0], dtype=np.float32)
    assert abs(cosine_similarity(v1, v2) - 0.8) < 1e-5


def test_duration_alignment_accepts_tail_padded_audio_overhang(tmp_path):
    import cv2
    import soundfile as sf
    from unittest.mock import MagicMock, patch
    from backend import v1_quality_gate as qg
    from backend.v1_media_streams import MainVideo

    src_vid = tmp_path / "src.mp4"
    out_vid = tmp_path / "out.mp4"
    mix_wav = tmp_path / "mixed.wav"
    src_vid.write_bytes(b"v" * 20000)
    out_vid.write_bytes(b"v" * 20000)
    # Source video track is 30.70s while original/mixed audio is 31.35s (+0.65s tail overhang)
    sf.write(str(mix_wav), np.zeros(int(16000 * 31.35), dtype=np.float32), 16000)

    def _fake_probe(p):
        dur = 30.70 if Path(p).name == "src.mp4" else 31.35
        return MainVideo(0, 720, 1280, 30.0, dur, 1, 1, False)

    fake_cap = MagicMock()
    fake_cap.isOpened.return_value = True
    fake_cap.read.return_value = (True, np.zeros((16, 16, 3), dtype=np.uint8))
    fake_cap.get.side_effect = lambda prop: {
        cv2.CAP_PROP_FRAME_COUNT: 940,
        cv2.CAP_PROP_FPS: 30.0,
        cv2.CAP_PROP_FRAME_WIDTH: 720,
        cv2.CAP_PROP_FRAME_HEIGHT: 1280,
    }.get(prop, 0)

    with patch("v1_media_streams.probe_main_video", side_effect=_fake_probe), \
         patch("cv2.VideoCapture", return_value=fake_cap):
        rep = qg.run_quality_gate(
            job_id="qc_tail_pad",
            final_video_path=out_vid,
            original_video_path=src_vid,
            mixed_audio_path=mix_wav,
            policy="BLOCK",
            workspace_path=tmp_path,
            enable_visual_pixel_qc=False,
        )
    dur_chk = next(c for c in rep.checks if c.name == "duration_alignment")
    assert dur_chk.status == "PASS"


def test_validate_video_output_accepts_tail_padded_audio_overhang():
    import pytest
    from unittest.mock import patch
    import v1_media_streams as ms

    src = ms.MainVideo(0, 720, 1280, 29.381, 30.70, 1, 1, False)
    out = ms.MainVideo(0, 720, 1280, 29.394, 31.333, 1, 1, False)

    with patch.object(ms, "probe_main_video", return_value=out):
        with pytest.raises(ValueError):
            ms.validate_video_output("out.mp4", src, require_audio=True)
        res = ms.validate_video_output("out.mp4", src, require_audio=True, allowed_tail_pad=0.687)
        assert res == out


def test_attach_source_caption_tracks_preserves_off_center_anchor_column():
    from types import SimpleNamespace
    import ocr_utils

    seg1 = SimpleNamespace(index=1, start=0.0, end=2.0, content="首先你需要一张卡膜", tracking_blocks=[])
    seg2 = SimpleNamespace(index=2, start=8.0, end=10.0, content="最后贴个封贴就完成了", tracking_blocks=[])

    # Off-center top-right column at cx ~ 0.69 (x=0.46..0.92 and x=0.58..0.80)
    t1 = SimpleNamespace(
        text="首先你需要一张卡膜", start=0.2, end=1.8, sample_time=0.5,
        x_pct=0.46, max_x_pct=0.92, y_pct=0.17, max_y_pct=0.21, prob=0.99,
    )
    t_silent = SimpleNamespace(
        text="硬卡套", start=4.5, end=6.0, sample_time=5.0,
        x_pct=0.60, max_x_pct=0.78, y_pct=0.17, max_y_pct=0.21, prob=0.99,
    )
    t_unrelated = SimpleNamespace(
        text="包装说明", start=4.5, end=6.0, sample_time=5.0,
        x_pct=0.12, max_x_pct=0.28, y_pct=0.17, max_y_pct=0.21, prob=0.99,
    )
    t2 = SimpleNamespace(
        text="最后贴个封贴就完成了", start=8.1, end=9.8, sample_time=8.5,
        x_pct=0.46, max_x_pct=0.92, y_pct=0.17, max_y_pct=0.21, prob=0.99,
    )

    ocr_utils._attach_source_caption_tracks([seg1, seg2], [t1, t_silent, t_unrelated, t2])
    attached_texts = [b.text for s in (seg1, seg2) for b in s.tracking_blocks]
    assert "硬卡套" in attached_texts
    assert "包装说明" not in attached_texts
