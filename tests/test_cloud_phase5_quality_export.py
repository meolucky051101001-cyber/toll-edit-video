"""Phase 5 offline tests: Translation strictness, CapCut TTS enforcement, QC validation & atomic export."""

from __future__ import annotations

import errno
import hashlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_contract import CloudErrorCode
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.gpu_scripts import full_pipeline


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_sample_video(directory: Path, name: str = "sample.mp4", payload: bytes | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_bytes(payload if payload is not None else (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"ok-video" * 4))
    return p


def _enqueue_and_claim(store: CloudJobStore, video_path: Path, *, owner_id: str = "w1", voice: str = "BV562_streaming"):
    job = store.enqueue(str(video_path), video_name=video_path.name, gpu_type="T4", cu_estimated=0.5, voice=voice)
    claimed = store.claim_job(job.job_id, owner_id=owner_id)
    assert claimed is not None
    return claimed


# ---------------------------------------------------------------------------
# 1. Translation Validation Tests
# ---------------------------------------------------------------------------


def test_asr_cannot_inflate_source_duration_or_reach_ocr_with_out_of_bounds_cues(monkeypatch):
    monkeypatch.setattr(full_pipeline.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"format": {"duration": "32.633333"}}), stderr=""))
    valid = [{"id": 1, "start": 0.0, "end": 12.0}]
    assert full_pipeline.validate_asr_source_timeline("source.mp4", valid) == pytest.approx(32.633333)
    invalid = [{"id": 17, "start": 31.0, "end": 59.98}]
    with pytest.raises(full_pipeline.PipelineStageError) as error:
        full_pipeline.step3b_ocr_and_fix_subtitles("source.mp4", invalid)
    assert error.value.error_code == "ASR_TIMING_INVALID"
    assert invalid[0]["end"] == 59.98  # Never silently discard/shorten an unverified speech cue.


@pytest.mark.parametrize("aligned_end", [32.5, 59.98])
def test_asr_aligns_words_and_rejects_remaining_overrun_before_writing_srt(tmp_path, monkeypatch, aligned_end):
    """Use acoustically aligned boundaries, but never assume alignment guarantees valid output."""
    calls = []
    loaded_models = []
    decoded = [0] * 522133

    def transcribe(path, **options):
        calls.append((path, options))
        return {"segments": [{"start": 30.0, "end": aligned_end, "text": "最后一句"}]}

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    def load_model(name, **options):
        loaded_models.append(name)
        return SimpleNamespace(transcribe=transcribe)
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(load_model=load_model))
    monkeypatch.setattr(full_pipeline, "_asr_speech_audio", lambda *a: (
        decoded, {"speech_windows_seconds": [{"start": 30.0, "end": 32.633333}]}))
    monkeypatch.setattr(full_pipeline.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"format": {"duration": "32.633333"}}), stderr=""))
    srt = tmp_path / "source.srt"
    if aligned_end > 32.633333:
        with pytest.raises(full_pipeline.PipelineStageError) as error:
            full_pipeline.step3_asr_whisper("vocals.wav", str(srt))
        assert error.value.error_code == "ASR_TIMING_INVALID"
        assert not srt.exists()
    else:
        segments = full_pipeline.step3_asr_whisper("vocals.wav", str(srt))
        assert segments == [{"id": 1, "start": 30.0, "end": 32.5, "text": "最后一句"}]
        assert "00:00:32,500" in srt.read_text(encoding="utf-8")
    assert calls == [(decoded, {"verbose": False, "word_timestamps": True,
                       "condition_on_previous_text": False, "clip_timestamps": [30.0, 32.63],
                       "temperature": 0.0, "beam_size": 5})]
    audit = json.loads(srt.with_suffix(".asr.json").read_text(encoding="utf-8"))
    manifest = json.loads((REPO_ROOT / "backend/cloud/gpu_scripts/cloud_env_manifest.json").read_text(encoding="utf-8"))
    assert loaded_models == [manifest["models"]["whisper"]["model_id"]] == ["large-v3-turbo"]
    assert audit["model"] == loaded_models[0]
    assert audit["segments"][0]["end"] == aligned_end


def test_asr_speech_windows_union_preserves_original_only_speech_and_offsets():
    # Vocal separation misses a quiet original phrase; include it and retain the later offset.
    windows = [{"start": 16000, "end": 32000}, {"start": 28000, "end": 40000},
               {"start": 48000, "end": 56000}, {"start": 160000, "end": 180000}]
    assert full_pipeline._merge_speech_windows(windows, 200000) == [
        {"start": 16000, "end": 56000}, {"start": 160000, "end": 180000}]
    # Absolute timestamps must remain in the second speech window, not reset to zero.
    full_pipeline._validate_asr_alignment([{"start": 10.1, "end": 10.8}], [{"start": 10, "end": 11.25}])
    with pytest.raises(full_pipeline.PipelineStageError):
        full_pipeline._validate_asr_alignment([{"start": 0.1, "end": 0.8}], [{"start": 10, "end": 11.25}])


def test_asr_vad_uses_original_when_separation_loses_quiet_speech(monkeypatch):
    vocal, original = [0] * 32000, [1] * 32000
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=lambda a: a))
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(
        audio=SimpleNamespace(SAMPLE_RATE=16000), load_audio=lambda path: vocal if path == "vocals" else original))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", SimpleNamespace(
        VadOptions=lambda **kw: SimpleNamespace(**kw, min_speech_duration_ms=0, speech_pad_ms=400),
        get_speech_timestamps=lambda samples, *a, **kw: [] if samples is vocal else [{"start": 8000, "end": 24000}]))
    decoded, audit = full_pipeline._asr_speech_audio("vocals", "original")
    assert decoded is vocal
    assert audit["track_windows_seconds"]["vocals"] == []
    assert audit["speech_windows_seconds"] == [{"start": 0.5, "end": 1.5}]


def test_asr_rejects_collapsed_word_alignment_even_when_segment_fits_source():
    words = [{"word": "字", "start": 30.1, "end": 30.1} for _ in range(6)]
    words.append({"word": "尾", "start": 30.1, "end": 32.28})
    with pytest.raises(full_pipeline.PipelineStageError, match="0 giây"):
        full_pipeline._validate_asr_alignment(
            [{"start": 30.1, "end": 32.28, "words": words}], [{"start": 30, "end": 32.64}])


def test_asr_clip_end_cannot_round_past_last_mel_frame_and_loop_forever():
    # Real long Canary: round(59.83925*100)=5984, but model only has 5983 frames.
    windows = [{"start": 0.088, "end": 27.88}, {"start": 29.08, "end": 59.83925}]
    clips = full_pipeline._whisper_clip_timestamps(windows, 957428)
    assert clips == [0.09, 27.88, 29.08, 59.83]
    assert round(clips[-1] * 100) == 957428 // 160
    with pytest.raises(full_pipeline.PipelineStageError):
        full_pipeline._whisper_clip_timestamps([{"start": 59.839, "end": 59.83925}], 957428)


@pytest.mark.parametrize("retry_recovers", [True, False])
@pytest.mark.parametrize("retry_cause", ["gap", "low_confidence", "alignment"])
def test_asr_prefers_original_and_retries_invalid_output_once_then_fails_closed(tmp_path, monkeypatch, retry_recovers, retry_cause):
    vocal, original = [0] * 160000, [1] * 160000
    calls = []
    def transcribe(audio, **options):
        calls.append(audio)
        recovered = audio is vocal and retry_recovers
        end = 2.0 if retry_cause == "gap" and not recovered else 9.8
        if retry_cause == "alignment" and not recovered:
            end = 10.5
        # Even fixed decoding produced a 40-ms false tail at avg_logprob=-1.24.
        score = -1.2414 if retry_cause == "low_confidence" and not recovered else -0.2
        words = [
            {"word": char, "start": i * end / 4, "end": (i + 1) * end / 4}
            for i, char in enumerate("真正的话")
        ]
        return {"segments": [{"start": 0.0, "end": end, "text": "真正的话",
                              "avg_logprob": score, "words": words}]}
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(load_audio=lambda p: original,
        load_model=lambda *a, **kw: SimpleNamespace(transcribe=transcribe)))
    windows = [{"start": 0.0, "end": 10.0}]
    monkeypatch.setattr(full_pipeline, "_asr_speech_audio", lambda *a: (
        vocal, {"speech_windows_seconds": windows, "track_windows_seconds": {"original": windows}}))
    monkeypatch.setattr(full_pipeline, "validate_asr_source_timeline", lambda *a: 10.0)
    srt = tmp_path / "retry.srt"
    if retry_recovers:
        result = full_pipeline.step3_asr_whisper("vocals", str(srt), reference_audio_path="original")
        assert result[-1]["end"] == 9.8
        assert "".join(cue["text"] for cue in result) == "真正的话"
    else:
        with pytest.raises(full_pipeline.PipelineStageError):
            full_pipeline.step3_asr_whisper("vocals", str(srt), reference_audio_path="original")
        assert not srt.exists()
    assert calls == [original, vocal]
    audit = json.loads(srt.with_suffix(".asr.json").read_text(encoding="utf-8"))
    assert audit["recognition_track"] == "vocals_retry"
    assert audit["initial_uncovered_speech"] == ([{"start": 2.0, "end": 10.0}] if retry_cause == "gap" else [])
    assert bool(audit["initial_low_confidence_segments"]) == (retry_cause == "low_confidence")
    assert bool(audit["initial_alignment_error"]) == (retry_cause == "alignment")
    assert bool(audit["uncovered_speech"] or audit["low_confidence_segments"] or audit["alignment_error"]) == (not retry_recovers)


def test_asr_does_not_decode_when_vad_finds_no_speech(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace())  # No model may be loaded.
    monkeypatch.setattr(full_pipeline, "_asr_speech_audio", lambda *a: (
        "silence", {"speech_windows_seconds": []}))
    monkeypatch.setattr(full_pipeline, "validate_asr_source_timeline", lambda *a: 32.64)
    srt = tmp_path / "silent.srt"
    assert full_pipeline.step3_asr_whisper("silence.wav", str(srt)) == []
    assert srt.read_text(encoding="utf-8") == ""


def test_cloud_ocr_locates_top_caption_before_any_band_classification(tmp_path, monkeypatch):
    """Raw EasyOCR rows must reach the real selector rather than be pre-excluded."""
    import numpy as np
    segments = [
        {"id": 1, "start": 0.0, "end": 1.2, "text": "包装好高级"},
        {"id": 2, "start": 1.2, "end": 2.4, "text": "还带一个切割器"},
        {"id": 3, "start": 2.4, "end": 3.6, "text": "用多少就裁多少"},
    ]
    frame = np.zeros((1280, 720, 3), dtype=np.uint8)
    position = [0.0]
    capture = SimpleNamespace(
        isOpened=lambda: True, get=lambda key: {1: 720, 2: 1280, 3: 30, 4: 108}[key],
        set=lambda key, value: position.__setitem__(0, value / 1000),
        read=lambda: (True, frame), release=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        VideoCapture=lambda _: capture, CAP_PROP_FRAME_WIDTH=1, CAP_PROP_FRAME_HEIGHT=2,
        CAP_PROP_FPS=3, CAP_PROP_FRAME_COUNT=4, CAP_PROP_POS_MSEC=5, COLOR_BGR2RGB=6,
        cvtColor=lambda image, _: image))
    def readtext(_):
        segment = next(s for s in segments if s["start"] <= position[0] < s["end"])
        return [([[140, 254], [580, 254], [580, 286], [140, 286]], segment["text"], 0.99)]
    monkeypatch.setitem(sys.modules, "easyocr", SimpleNamespace(
        Reader=lambda *a, **kw: SimpleNamespace(readtext=readtext)))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setattr(full_pipeline, "validate_asr_source_timeline", lambda *a: 3.6)
    result, y, _, _, _ = full_pipeline.step3b_ocr_and_fix_subtitles(
        "source.mp4", segments, str(tmp_path / "source.srt"))
    assert all(s["ocr_mask_status"] == "located" for s in result)
    assert 0.18 < y < 0.25
    assert all(s["best_block"]["text"] == s["text"] for s in result)


def test_translation_rejects_missing_duplicate_empty_and_untranslated_cues(tmp_path: Path, monkeypatch):
    segments = [
        {"id": 1, "start": 0.0, "end": 1.5, "text": "你好世界"},
        {"id": 2, "start": 1.6, "end": 3.0, "text": "欢迎来到这里"},
    ]

    # Direct validator failures
    with pytest.raises(full_pipeline.PipelineStageError) as exc_missing:
        full_pipeline.validate_translation_items(segments, [{"id": 1, "text": "Xin chào thế giới"}])
    assert exc_missing.value.error_code == "TRANSLATION_FAILED"

    with pytest.raises(full_pipeline.PipelineStageError) as exc_dup:
        full_pipeline.validate_translation_items(
            segments,
            [{"id": 1, "text": "Xin chào"}, {"id": 1, "text": "Xin chào lần hai"}],
        )
    assert exc_dup.value.error_code == "TRANSLATION_FAILED"

    with pytest.raises(full_pipeline.PipelineStageError) as exc_empty:
        full_pipeline.validate_translation_items(
            segments,
            [{"id": 1, "text": "Xin chào"}, {"id": 2, "text": "   "}],
        )
    assert exc_empty.value.error_code == "TRANSLATION_FAILED"

    with pytest.raises(full_pipeline.PipelineStageError) as exc_cjk:
        full_pipeline.validate_translation_items(
            segments,
            [{"id": 1, "text": "Xin chào"}, {"id": 2, "text": "欢迎来到这里"}],
        )
    assert exc_cjk.value.error_code == "TRANSLATION_FAILED"

    # Missing GEMINI_API_KEY must NOT fall back to original Chinese text
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(full_pipeline.PipelineStageError) as exc_key:
        full_pipeline.step4_translate_subtitles(segments, "", str(tmp_path / "vi.srt"))
    assert exc_key.value.error_code == "TRANSLATION_FAILED"

    # End-to-end step4 with fake Gemini client returning 1 missing cue -> exhausts retries & fails
    calls = {"count": 0}

    class FakeModels:
        def generate_content(self, **kwargs):
            calls["count"] += 1
            return SimpleNamespace(text=json.dumps([{"id": 1, "text": "Xin chào thế giới"}]))

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    fake_genai_mod = types.ModuleType("google.genai")
    fake_genai_mod.Client = FakeClient
    fake_types_mod = types.ModuleType("google.genai.types")
    fake_types_mod.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)
    fake_genai_mod.types = fake_types_mod
    fake_google_pkg = types.ModuleType("google")
    fake_google_pkg.genai = fake_genai_mod

    monkeypatch.setitem(sys.modules, "google", fake_google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_mod)
    monkeypatch.setattr(full_pipeline.time, "sleep", lambda _s: None)

    with pytest.raises(full_pipeline.PipelineStageError) as exc_retry:
        full_pipeline.step4_translate_subtitles(segments, "AIzaSyFakeKey_1234567890", str(tmp_path / "vi.srt"))
    assert exc_retry.value.error_code == "TRANSLATION_FAILED"
    assert calls["count"] == 6  # 3 candidate models * 2 bounded retries each


# ---------------------------------------------------------------------------
# 2. CapCut TTS Strict Enforcement, Retries & No-BGM-Only Guard
# ---------------------------------------------------------------------------


def test_capcut_tts_retries_transient_error_without_switching_to_edge(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(full_pipeline.time, "sleep", lambda _s: None)
    monkeypatch.setattr(full_pipeline, "validate_tts_audio_file", lambda _p, **kw: 1.25)

    attempts = {"create": 0}

    class FakeSession:
        def get(self, url, timeout=15):
            return SimpleNamespace(content=b"ID3" + b"\x00" * 300, raise_for_status=lambda: None)

    class FakeCapCutClient:
        def __init__(self):
            self.session = FakeSession()

        def create_tts_task(self, texts, voice):
            attempts["create"] += 1
            if attempts["create"] == 1:
                return {"data": {"tasks": []}}  # transient fail on 1st attempt
            return {"data": {"tasks": [{"id": "t1", "token": "tok1"}]}}

        def query_tts_task(self, task_id, token):
            return {
                "data": {
                    "tasks": [
                        {
                            "status": "success",
                            "payload": json.dumps({"audio_subtitles": [{"speech_url": "https://tts.local/1.mp3"}]}),
                        }
                    ]
                }
            }

    fake_capcut_mod = types.ModuleType("capcut_tts_api")
    fake_capcut_mod.CapCutClient = FakeCapCutClient
    monkeypatch.setitem(sys.modules, "capcut_tts_api", fake_capcut_mod)

    edge_called = {"called": False}

    async def forbidden_edge(*args, **kwargs):
        edge_called["called"] = True

    monkeypatch.setattr(full_pipeline, "generate_single_tts", forbidden_edge)

    segments = [
        {"id": 1, "start": 0.0, "end": 2.0, "text": "你好", "translated_text": "Xin chào"},
        {"id": 2, "start": 2.2, "end": 4.0, "text": "再见", "translated_text": "Tạm biệt"},
    ]
    tts_dir = tmp_path / "tts"
    summary = full_pipeline.step5_generate_tts(segments, "BV562_streaming", str(tts_dir))
    assert summary["tts_engine"] == "capcut_tts"
    assert summary["tts_voice"] == "BV562_streaming"
    assert summary["verified_count"] == 2
    assert summary["fallback_used"] is False
    assert edge_called["called"] is False
    assert attempts["create"] == 3  # 2 tries on seg 1 + 1 try on seg 2
    assert all(Path(s["tts_path"]).is_file() for s in segments)


def test_capcut_tts_blocks_last_cue_failure_and_blocks_bgm_only_export(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(full_pipeline.time, "sleep", lambda _s: None)
    monkeypatch.setattr(full_pipeline, "validate_tts_audio_file", lambda _p, **kw: 1.0)

    def fail_on_second(text, voice, out_file, **kwargs):
        if "cuối" in text:
            return False
        Path(out_file).write_bytes(b"ID3" + b"\x00" * 300)
        return True

    monkeypatch.setattr(full_pipeline, "generate_capcut_tts_single", fail_on_second)
    edge_called = {"called": False}

    async def spy_edge(*args, **kwargs):
        edge_called["called"] = True

    monkeypatch.setattr(full_pipeline, "generate_single_tts", spy_edge)

    segments = [
        {"id": 1, "start": 0.0, "end": 1.5, "text": "第一句", "translated_text": "Câu đầu"},
        {"id": 2, "start": 1.7, "end": 3.2, "text": "最后一句", "translated_text": "Câu cuối"},
    ]
    with pytest.raises(full_pipeline.PipelineStageError) as exc_tts:
        full_pipeline.step5_generate_tts(segments, "BV562_streaming", str(tmp_path / "tts"))
    assert exc_tts.value.error_code == "TTS_GENERATION_FAILED"
    assert edge_called["called"] is False

    # Empty segments blocked in step5 & step6 unless allow_no_speech=True
    bgm = tmp_path / "bgm.wav"
    bgm.write_bytes(b"RIFF" + b"\x00" * 2048)
    with pytest.raises(full_pipeline.PipelineStageError) as exc_empty:
        full_pipeline.step5_generate_tts([], "BV562_streaming", str(tmp_path / "tts2"))
    assert exc_empty.value.error_code == "NO_SPEECH_DETECTED"

    with pytest.raises(full_pipeline.PipelineStageError) as exc_bgm:
        full_pipeline.step6_mix_audio_ducking_ebu_r128(str(bgm), [], str(tmp_path / "out.wav"))
    assert exc_bgm.value.error_code == "NO_SPEECH_DETECTED"

    # With allow_no_speech=True, explicit metadata mode succeeds
    no_speech_res = full_pipeline.step5_generate_tts([], "BV562_streaming", str(tmp_path / "tts2"), allow_no_speech=True)
    assert no_speech_res["tts_engine"] == "none"
    full_pipeline.step6_mix_audio_ducking_ebu_r128(str(bgm), [], str(tmp_path / "out_ns.wav"), allow_no_speech=True)
    assert (tmp_path / "out_ns.wav").is_file()


def test_validate_tts_audio_file_rejects_empty_and_corrupt(tmp_path: Path, monkeypatch):
    bad = tmp_path / "corrupt.mp3"
    bad.write_bytes(b"not-valid-audio-bytes" * 15)
    monkeypatch.setattr(
        "backend.cloud.gpu_scripts.full_pipeline.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Invalid data found"),
    )
    with pytest.raises(ValueError):
        full_pipeline.validate_tts_audio_file(str(bad))


# ---------------------------------------------------------------------------
# 3. Orchestrator: Pipeline Error Codes, Staging Isolation, Local QC & Atomic Export
# ---------------------------------------------------------------------------


class _Phase5Adapter:
    def __init__(
        self,
        *,
        pipeline_result=(True, "PIPELINE_SUCCESS"),
        video_bytes: bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"dubbed-ok" * 8,
        metadata_dict: dict | None = None,
    ):
        self.pipeline_result = pipeline_result
        self.video_bytes = video_bytes
        self.metadata_dict = metadata_dict
        self.sessions_stopped = []
        self.created_sessions = []

    def get_usage(self):
        return {"ok": True, "balance": 50.0, "stale": False}

    def create_session(self, session_name, gpu="T4"):
        self.created_sessions.append(session_name)
        return True, "created"

    def stop_session(self, session_name):
        self.sessions_stopped.append(session_name)
        return True, "stopped"

    def get_sessions_status(self):
        return {"ok": True, "sessions": [{"name": s} for s in self.created_sessions if s not in self.sessions_stopped]}

    def upload_file(self, session_name, local_path, remote_filename=None):
        return True, "uploaded"

    def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None):
        if len(argv) >= 2 and argv[1] == "full_pipeline.py":
            return self.pipeline_result
        return True, "ok"

    def download_file(self, session_name, remote_filename, local_dir):
        dest = Path(local_dir) / Path(remote_filename).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if remote_filename == "output_video.mp4":
            dest.write_bytes(self.video_bytes)
            return True, str(dest)
        if remote_filename == "result_metadata.json":
            if self.metadata_dict is not None:
                dest.write_text(json.dumps(self.metadata_dict), encoding="utf-8")
            else:
                dest.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "segments_count": 2,
                            "translated_count": 2,
                            "tts_verified_count": 2,
                            "artifacts": {
                                "output_video.mp4": {
                                    "size_bytes": len(self.video_bytes),
                                    "sha256": _sha256_bytes(self.video_bytes),
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            return True, str(dest)
        dest.write_text("dummy", encoding="utf-8")
        return True, str(dest)


def test_orchestrator_propagates_pipeline_error_code_and_stops_session(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "deliverables"
    out_dir.mkdir(parents=True)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))

    src_vid = _make_sample_video(tmp_path / "in", "bad_trans.mp4")
    err_payload = json.dumps({"status": "error", "error_code": "TRANSLATION_FAILED", "error": "missing cue 2"})
    adapter = _Phase5Adapter(pipeline_result=(False, f"error\nPIPELINE_RESULT_JSON:{err_payload}"))
    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    orch = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])

    job = _enqueue_and_claim(store, src_vid)
    assert orch.process_job(job, owner_id="w1") is False

    saved = store.get_job(job.job_id)
    assert saved.state == "failed"
    assert saved.error_code == CloudErrorCode.TRANSLATION_FAILED.value
    assert saved.cleanup_status == "verified"
    assert list(out_dir.iterdir()) == []


def test_orchestrator_qc_blocks_truncated_no_audio_and_bad_duration_and_keeps_staging_evidence(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "deliverables"
    out_dir.mkdir(parents=True)
    results_dir = tmp_path / "results"
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(results_dir))

    src_vid = _make_sample_video(tmp_path / "in", "qc_target.mp4")
    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)

    # Case A: Truncated download (size_bytes mismatch vs artifact manifest)
    full_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"Z" * 100
    bad_meta_size = {
        "status": "completed",
        "segments_count": 2,
        "translated_count": 2,
        "tts_verified_count": 2,
        "artifacts": {
            "output_video.mp4": {
                "size_bytes": len(full_bytes) + 50,
                "sha256": _sha256_bytes(full_bytes),
            }
        },
    }
    orch_a = CloudOrchestrator(
        store=store,
        adapter=_Phase5Adapter(video_bytes=full_bytes, metadata_dict=bad_meta_size),
        allowed_roots=[tmp_path],
    )
    job_a = _enqueue_and_claim(store, src_vid)
    assert orch_a.process_job(job_a, owner_id="w1") is False
    saved_a = store.get_job(job_a.job_id)
    assert saved_a.state == "failed"
    assert saved_a.error_code == CloudErrorCode.DOWNLOAD_FAILED.value
    assert (results_dir / job_a.job_id / "attempt_1" / "qc_report.json").is_file()
    assert list(out_dir.iterdir()) == []

    # Case B: Missing audio stream detected via probe_video_metadata
    monkeypatch.setattr(
        "backend.cloud.cloud_orchestrator.probe_video_metadata",
        lambda _p: {
            "exists": True,
            "size_bytes": len(full_bytes),
            "header_valid": True,
            "probe_method": "ffprobe",
            "video_codec": "h264",
            "audio_codec": None,
            "duration_seconds": 15.0,
        },
    )
    orch_b = CloudOrchestrator(
        store=store,
        adapter=_Phase5Adapter(video_bytes=full_bytes),
        allowed_roots=[tmp_path],
    )
    job_b = _enqueue_and_claim(store, src_vid)
    assert orch_b.process_job(job_b, owner_id="w1") is False
    saved_b = store.get_job(job_b.job_id)
    assert saved_b.state == "failed"
    assert saved_b.error_code == CloudErrorCode.QC_FAILED.value
    assert (results_dir / job_b.job_id / "attempt_1" / "qc_report.json").is_file()
    assert list(out_dir.iterdir()) == []

    # Case C: Duration mismatch against remote input duration
    monkeypatch.setattr(
        "backend.cloud.cloud_orchestrator.probe_video_metadata",
        lambda _p: {
            "exists": True,
            "size_bytes": len(full_bytes),
            "header_valid": True,
            "probe_method": "ffprobe",
            "video_codec": "h264",
            "audio_codec": "aac",
            "duration_seconds": 4.0,
        },
    )
    bad_meta_dur = {
        "status": "completed",
        "input_duration_seconds": 30.0,
        "segments_count": 2,
        "translated_count": 2,
        "tts_verified_count": 2,
        "artifacts": {
            "output_video.mp4": {"size_bytes": len(full_bytes), "sha256": _sha256_bytes(full_bytes)}
        },
    }
    orch_c = CloudOrchestrator(
        store=store,
        adapter=_Phase5Adapter(video_bytes=full_bytes, metadata_dict=bad_meta_dur),
        allowed_roots=[tmp_path],
    )
    job_c = _enqueue_and_claim(store, src_vid)
    assert orch_c.process_job(job_c, owner_id="w1") is False
    saved_c = store.get_job(job_c.job_id)
    assert saved_c.state == "failed"
    assert saved_c.error_code == CloudErrorCode.QC_FAILED.value
    assert list(out_dir.iterdir()) == []


def test_atomic_export_unique_job_attempt_names_output_manifest_and_disk_full(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "deliverables"
    out_dir.mkdir(parents=True)
    results_dir = tmp_path / "results"
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(results_dir))

    in_a = _make_sample_video(tmp_path / "dir_a", "same_name.mp4")
    in_b = _make_sample_video(tmp_path / "dir_b", "same_name.mp4")

    bytes_1 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"video-one" * 8
    bytes_2 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"video-two" * 8

    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    orch1 = CloudOrchestrator(store=store, adapter=_Phase5Adapter(video_bytes=bytes_1), allowed_roots=[tmp_path])
    job1 = _enqueue_and_claim(store, in_a)
    assert orch1.process_job(job1, owner_id="w1") is True

    orch2 = CloudOrchestrator(store=store, adapter=_Phase5Adapter(video_bytes=bytes_2), allowed_roots=[tmp_path])
    job2 = _enqueue_and_claim(store, in_b)
    assert orch2.process_job(job2, owner_id="w1") is True

    s1 = store.get_job(job1.job_id)
    s2 = store.get_job(job2.job_id)
    assert s1.result_path != s2.result_path
    assert job1.job_id in Path(s1.result_path).name and "_a1" in Path(s1.result_path).name
    assert job2.job_id in Path(s2.result_path).name and "_a1" in Path(s2.result_path).name
    assert Path(s1.result_path).read_bytes() == bytes_1
    assert Path(s2.result_path).read_bytes() == bytes_2
    assert s1.output_manifest is not None and s1.output_manifest["qc_passed"] is True
    assert s1.output_manifest["sha256"] == _sha256_bytes(bytes_1)

    # Simulated disk full (ENOSPC) cleans up .part file & leaves existing deliverables intact
    staging_file = results_dir / job2.job_id / "attempt_1" / "output_video.mp4"

    def fail_replace(_src, _dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("backend.cloud.cloud_orchestrator.os.replace", fail_replace)
    with pytest.raises(Exception) as exc_full:
        orch2.export_deliverable_atomic(
            staging_file,
            out_dir,
            job_id=job2.job_id,
            attempt=2,
            video_name="same_name.mp4",
            expected_sha256=_sha256_bytes(bytes_2),
        )
    assert getattr(exc_full.value, "error_code", None) == CloudErrorCode.ENV_DISK_INSUFFICIENT.value
    assert [p.name for p in out_dir.glob("*.part")] == []
    assert Path(s1.result_path).read_bytes() == bytes_1
