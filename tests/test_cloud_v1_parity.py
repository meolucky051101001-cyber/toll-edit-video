"""Offline regression tests verifying 100% Tool V1 Local parity in the Cloud GPU pipeline."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tarfile
import types
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator, _resolve_gemini_key_and_secrets
from backend.cloud.cloud_security import ALLOWED_VOICES, redact_secrets, validate_voice
from backend.cloud.cloud_telegram_bot import CloudBotStateFiles, resolve_voice_choice
from backend.cloud.gpu_scripts import full_pipeline
from backend.cloud.gpu_scripts.subtitle_timing import (
    SubtitleTimingError,
    generate_cloud_ass,
    validate_cloud_ass,
)
from backend.subtitle_blur import ASS_BLUR_MARKER, read_ass_blur_metadata


def _write_harmonic_speech_wav(path: Path, f0_hz: float, duration_s: float = 1.2, sr: int = 16000) -> Path:
    t = np.linspace(0.0, duration_s, int(sr * duration_s), endpoint=False, dtype=np.float64)
    wave = (
        0.55 * np.sin(2.0 * np.pi * f0_hz * t)
        + 0.28 * np.sin(2.0 * np.pi * (2.0 * f0_hz) * t)
        + 0.14 * np.sin(2.0 * np.pi * (3.0 * f0_hz) * t)
    )
    sf.write(str(path), wave.astype(np.float32), sr)
    return path


def test_auto_voice_allowlist_bot_default_and_dashboard_ui(tmp_path: Path):
    assert "auto" in ALLOWED_VOICES
    assert validate_voice("auto") == "auto"
    assert resolve_voice_choice("auto") == "auto"
    assert resolve_voice_choice("tudong") == "auto"

    state = CloudBotStateFiles(tmp_path / "ws")
    assert state.get_prefs()["voice"] == "auto"

    html = (BACKEND_ROOT / "templates" / "cloud_dashboard.html").read_text(encoding="utf-8")
    assert '<option value="auto" selected>' in html


def test_auto_voice_locks_male_female_and_never_jumps_past_first_speaker(tmp_path: Path):
    sr = 16000
    t1 = np.linspace(0.0, 1.0, sr, endpoint=False)
    # Segment 1 (0..1s): Male 125 Hz; Segment 2 (1.2..2.2s): Female 235 Hz
    seg1_male = 0.6 * np.sin(2 * np.pi * 125.0 * t1) + 0.3 * np.sin(2 * np.pi * 250.0 * t1)
    gap = np.zeros(int(0.2 * sr), dtype=np.float64)
    seg2_female = 0.6 * np.sin(2 * np.pi * 235.0 * t1) + 0.3 * np.sin(2 * np.pi * 470.0 * t1)
    combined_male_first = np.concatenate([seg1_male, gap, seg2_female]).astype(np.float32)
    male_wav = tmp_path / "male_first.wav"
    sf.write(str(male_wav), combined_male_first, sr)

    segments = [
        {"id": 1, "start": 0.0, "end": 1.0, "text": "大家好今天看显卡"},
        {"id": 2, "start": 1.2, "end": 2.2, "text": "这个真的很好用"},
    ]
    lock_m = tmp_path / "lock_male.json"
    res_male = full_pipeline.resolve_cloud_voice_choice(
        "auto",
        segments,
        vocals_path=male_wav,
        lock_path=lock_m,
    )
    assert res_male["detected_gender"] == "male"
    assert 118.0 <= res_male["median_f0"] <= 132.0
    assert res_male["confidence"] >= 0.70
    assert res_male["resolved_voice"] == "BV075_streaming"
    assert json.loads(lock_m.read_text(encoding="utf-8"))["resolved_voice"] == "BV075_streaming"

    # Female first speaker (230 Hz) -> BV562_streaming
    female_wav = _write_harmonic_speech_wav(tmp_path / "female.wav", 230.0, duration_s=1.0, sr=sr)
    res_female = full_pipeline.resolve_cloud_voice_choice(
        "auto",
        [{"id": 1, "start": 0.0, "end": 1.0, "text": "今天分享美妆教程"}],
        vocals_path=female_wav,
        lock_path=tmp_path / "lock_female.json",
    )
    assert res_female["detected_gender"] == "female"
    assert 220.0 <= res_female["median_f0"] <= 240.0
    assert res_female["confidence"] >= 0.70
    assert res_female["resolved_voice"] == "BV562_streaming"

    # Unvoiced noise on segment 1 must NOT jump to segment 2 (female/male), falls back safely to BV562_streaming
    rng = np.random.default_rng(42)
    noise_then_male = np.concatenate([0.2 * rng.standard_normal(sr), gap, seg1_male]).astype(np.float32)
    noisy_wav = tmp_path / "noise_first.wav"
    sf.write(str(noisy_wav), noise_then_male, sr)
    res_fallback = full_pipeline.resolve_cloud_voice_choice(
        "auto",
        segments,
        vocals_path=noisy_wav,
        lock_path=tmp_path / "lock_fallback.json",
    )
    assert res_fallback["detected_gender"] == "unknown"
    assert res_fallback["resolved_voice"] == "BV562_streaming"
    assert res_fallback["rule"] == "fallback_default_female_unknown_first_speaker"


def test_localized_blur_ass_generation_and_tamper_detection(tmp_path: Path):
    segments = [
        {
            "id": 1,
            "start": 0.5,
            "end": 2.5,
            "text": "包装很精致",
            "translated_text": "Bao bì rất tinh tế",
            "tts_duration": 1.8,
            "ocr_mask_status": "located",
            "caption_geometry_verified": True,
            "tracking_blocks": [
                {
                    "start": 0.2,
                    "end": 2.9,  # Extends across speech gap independently of dialogue!
                    "x_pct": 0.22,
                    "max_x_pct": 0.78,
                    "y_pct": 0.76,
                    "max_y_pct": 0.82,
                    "text": "包装很精致",
                    "is_subtitle": True,
                    "in_subtitle_band": True,
                    "is_packaging": False,
                }
            ],
        },
        {
            "id": 2,
            "start": 3.2,
            "end": 4.8,
            "text": "没有字幕的一句",
            "translated_text": "Câu này không có chữ cứng trên hình",
            "tts_duration": 1.5,
            "ocr_mask_status": "verified_no_caption",
            "caption_geometry_verified": True,
            "tracking_blocks": [],
        },
    ]
    ass_file = tmp_path / "subtitles_vi.ass"
    audit = generate_cloud_ass(
        segments,
        ass_file,
        video_w=1080,
        video_h=1920,
        main_y_pct=0.78,
        video_duration=5.5,
        cover_style="localized_blur",
        blur_settings={"sub_blur_radius": 16, "sub_blur_padding_x": 0.015, "sub_blur_padding_y": 0.008},
    )
    assert audit["passed"] is True
    assert audit["cover_style"] == "localized_blur"
    assert audit["cue_count"] == 2
    assert audit["source_mask_count"] == 1
    assert audit["source_masks"][0]["start"] == pytest.approx(0.2)
    assert audit["source_masks"][0]["end"] == pytest.approx(2.9)

    raw_ass = ass_file.read_text(encoding="utf-8-sig")
    assert "CloudSourceMask" not in raw_ass
    assert "BgStyle," not in raw_ass.split("[Events]", 1)[1]
    assert "&H00FFFFFF" in raw_ass  # Crisp white text with black outline
    assert ASS_BLUR_MARKER in raw_ass

    blur_meta = read_ass_blur_metadata(raw_ass)
    assert blur_meta is not None
    assert len(blur_meta["boxes"]) == 1
    assert len(blur_meta["text_boxes"]) >= 2

    # Removing source blur box from ToolV1BlurData must fail validation!
    tampered_meta = {
        "version": 6,
        "settings": blur_meta["settings"],
        "boxes": [],
        "text_boxes": blur_meta["text_boxes"],
    }
    tampered_ass = (
        "\n".join(line for line in raw_ass.splitlines() if not line.startswith(ASS_BLUR_MARKER))
        + "\n"
        + ASS_BLUR_MARKER
        + json.dumps(tampered_meta, separators=(",", ":"))
        + "\n"
    )
    ass_file.write_text(tampered_ass, encoding="utf-8-sig")
    expected_cues = [
        {**c, "name": f"cloud_cue_{c['id']}", "text": segments[idx]["translated_text"]}
        for idx, c in enumerate(audit["cues"])
    ]
    with pytest.raises(SubtitleTimingError, match="not fully blurred"):
        validate_cloud_ass(
            ass_file,
            expected_cues,
            audit["source_masks"],
            video_duration=5.5,
            cover_style="localized_blur",
        )


def test_v1_render_filter_graph_9_16_and_16_9_blurred_canvas(tmp_path: Path):
    segments = [
        {
            "id": 1,
            "start": 0.0,
            "end": 2.0,
            "text": "横屏视频测试",
            "translated_text": "Kiểm tra chuyển khung dọc 9:16 nền mờ",
            "tts_duration": 1.6,
            "ocr_mask_status": "located",
            "caption_geometry_verified": True,
            "tracking_blocks": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "x_pct": 0.25,
                    "max_x_pct": 0.75,
                    "y_pct": 0.80,
                    "max_y_pct": 0.88,
                    "is_subtitle": True,
                    "in_subtitle_band": True,
                    "is_packaging": False,
                }
            ],
        }
    ]
    ass_landscape = tmp_path / "land.ass"
    generate_cloud_ass(
        segments,
        ass_landscape,
        video_w=1920,
        video_h=1080,
        main_y_pct=0.82,
        video_duration=3.0,
        cover_style="localized_blur",
    )

    # 1. Non-9:16 (1920x1080) -> converted to 1080x1920 with blurred background & localized blur on [v2]
    graph_16_9, out_w, out_h, style_used = full_pipeline._build_v1_video_filter_graph(
        1920,
        1080,
        ass_path=str(ass_landscape),
        main_y_pct=0.82,
        canvas_cfg={"aspect_ratio": "9:16", "sub_cover_style": "localized_blur", "delogo_mode": "off"},
    )
    assert (out_w, out_h) == (1080, 1920)
    assert style_used == "localized_blur"
    assert graph_16_9.startswith("format=yuv420p,split[v1][v2];")
    assert "boxblur=8:3,scale=1080:1920:flags=bilinear[bg];" in graph_16_9
    assert "boxblur=luma_radius=" in graph_16_9
    assert "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg];" in graph_16_9
    assert "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p" in graph_16_9

    # 2. Already 9:16 (1080x1920) -> keeps 1080x1920, applies localized blur + subtitles directly
    ass_portrait = tmp_path / "port.ass"
    generate_cloud_ass(
        segments,
        ass_portrait,
        video_w=1080,
        video_h=1920,
        main_y_pct=0.82,
        video_duration=3.0,
        cover_style="localized_blur",
    )
    graph_9_16, pw, ph, pstyle = full_pipeline._build_v1_video_filter_graph(
        1080,
        1920,
        ass_path=str(ass_portrait),
        main_y_pct=0.82,
        canvas_cfg={"aspect_ratio": "9:16", "sub_cover_style": "localized_blur"},
    )
    assert (pw, ph) == (1080, 1920)
    assert pstyle == "localized_blur"
    assert "boxblur=8:3" not in graph_9_16
    assert "boxblur=luma_radius=" in graph_9_16
    assert graph_9_16.endswith(",format=yuv420p")


def test_probe_main_video_excludes_cover_art_and_applies_rotation(monkeypatch):
    fake_probe = {
        "format": {"duration": "18.5"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "width": 600,
                "height": 600,
                "disposition": {"attached_pic": 1, "default": 0},
            },
            {
                "index": 1,
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "disposition": {"attached_pic": 0, "default": 1},
                "side_data_list": [{"rotation": -90}],
            },
        ],
    }
    monkeypatch.setattr(
        full_pipeline.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(fake_probe), stderr=""),
    )
    info = full_pipeline._probe_main_video_cloud("rotated_with_cover.mp4")
    assert info["input_map"] == "0:1"
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["duration"] == pytest.approx(18.5)


def test_orchestrator_bundles_live_v1_profile_and_resolves_multi_gemini_keys(tmp_path: Path, monkeypatch):
    ws = tmp_path / "workspace"
    (ws / "bot_system" / "control").mkdir(parents=True)
    (ws / "bot_system" / "canvas_settings.json").write_text(
        json.dumps({"aspect_ratio": "9:16", "sub_cover_style": "localized_blur", "sub_blur_radius": 18}),
        encoding="utf-8",
    )
    (ws / "bot_system" / "audio_settings.json").write_text(
        json.dumps({"bgm_volume_db": -3.5, "dubbing_volume_db": 1.5, "ducking_mode": "soft", "script_mode": "humorous"}),
        encoding="utf-8",
    )
    (ws / "bot_system" / "control" / "custom_glossary.json").write_text(
        json.dumps({"entries": {"三合院": "nhà tam hợp viện"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyFirstMultiKey1234567890, AIzaSySecondMultiKey0987654321")

    chosen_key, extra_secrets = _resolve_gemini_key_and_secrets()
    assert chosen_key == "AIzaSyFirstMultiKey1234567890"
    assert len(extra_secrets) == 2
    assert "AIzaSySecondMultiKey" not in redact_secrets("err=AIzaSySecondMultiKey0987654321", extra_secrets=extra_secrets)

    orch = CloudOrchestrator(
        store=CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False),
        allowed_roots=[tmp_path],
    )
    profile = orch._build_v1_runtime_profile()
    assert profile["canvas"]["sub_cover_style"] == "localized_blur"
    assert profile["canvas"]["sub_blur_radius"] == 18
    assert profile["audio"]["bgm_volume_db"] == -3.5
    assert profile["audio"]["dubbing_volume_db"] == 1.5
    assert profile["audio"]["ducking_mode"] == "soft"
    assert profile["audio"]["script_mode"] == "humorous"
    assert profile["glossary"] == {"三合院": "nhà tam hợp viện"}

    # Verify subtitle_blur.py is verified by local preflight and included in upload_targets
    sample_vid = tmp_path / "sample.mp4"
    sample_vid.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom_test")
    from backend.cloud.cloud_security import validate_video_input

    validated = validate_video_input(str(sample_vid), allowed_roots=[tmp_path])
    preflight = orch.verify_local_preflight(validated)
    target_names = {p.name for p in preflight["upload_targets"]}
    assert "subtitle_blur.py" in target_names


def test_translation_prompt_includes_speaker_glossary_and_humorous_style(tmp_path: Path, monkeypatch):
    captured_prompts: list[str] = []

    class FakeModels:
        def generate_content(self, **kwargs):
            captured_prompts.append(str(kwargs.get("contents")))
            return SimpleNamespace(text=json.dumps([{"id": 1, "text": "Ngôi nhà tam hợp viện này đẹp quá"}]))

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda api_key=None: SimpleNamespace(models=FakeModels())
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)
    fake_genai.types = fake_types
    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    segs = [{"id": 1, "start": 0.0, "end": 2.4, "text": "这个三合院太好看了"}]
    out = full_pipeline.step4_translate_subtitles(
        segs,
        "AIzaSyFakeKey_1234567890",
        str(tmp_path / "vi.srt"),
        speaker_gender="male",
        script_mode="humorous",
        glossary={"三合院": "nhà tam hợp viện"},
        video_duration=3.0,
    )
    assert out[0]["translated_text"] == "Ngôi nhà tam hợp viện này đẹp quá"
    assert len(captured_prompts) == 1
    assert "HÀI HƯỚC" in captured_prompts[0]
    assert "nam giới" in captured_prompts[0]
    assert "nhà tam hợp viện" in captured_prompts[0]


def test_linux_missing_arial_resolves_vietnamese_font_for_localized_blur(tmp_path: Path, monkeypatch):
    from PIL import ImageFont
    from backend import subtitle_layout
    from backend.cloud.gpu_scripts import subtitle_timing

    real_installed = list(subtitle_layout._installed_fonts())
    valid_entry = next(
        (
            item
            for item in real_installed
            if item[0] != "arial"
            and "symbol" not in item[0]
            and "wingding" not in item[0]
            and "webding" not in item[0]
            and subtitle_timing._ttf_supports_codepoints(item[2])
        ),
        None,
    )
    assert valid_entry is not None, "Expected at least one non-Arial Vietnamese-capable font installed on test host"
    valid_path = valid_entry[2]

    invalid_entry = next(
        (
            item
            for item in real_installed
            if not subtitle_timing._ttf_supports_codepoints(item[2])
        ),
        None,
    )
    if invalid_entry is not None:
        invalid_path = invalid_entry[2]
    else:
        invalid_path = str(tmp_path / "FakeSymbol.ttf")
        Path(invalid_path).write_bytes(Path(valid_path).read_bytes())
        orig_supports = subtitle_timing._ttf_supports_codepoints
        monkeypatch.setattr(
            subtitle_timing,
            "_ttf_supports_codepoints",
            lambda p, *a, **kw: False if str(p) == invalid_path else orig_supports(p, *a, **kw),
        )

    monkeypatch.setattr(
        subtitle_layout,
        "_installed_fonts",
        lambda: [
            ("symbol", "regular", invalid_path),
            (valid_entry[0], valid_entry[1], valid_path),
        ],
    )
    real_truetype = ImageFont.truetype

    def fake_truetype(font, size=10, *args, **kwargs):
        if str(font) in {
            "Arial",
            "arial.ttf",
            "arialbd.ttf",
            "DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf",
            "Symbol",
        }:
            raise OSError(f"Font missing on Linux VM: {font}")
        return real_truetype(font, size, *args, **kwargs)

    monkeypatch.setattr(subtitle_layout.ImageFont, "truetype", fake_truetype)
    subtitle_layout.subtitle_measure.cache_clear()
    try:
        resolved = subtitle_timing.resolve_cloud_subtitle_font("Arial", bold=True)
        assert resolved.casefold() != "symbol"

        segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 2.4,
                "text": "当我换个关键词买谷美",
                "translated_text": "Khi tôi đổi từ khóa mua đồ 9,9 tệ",
                "tts_duration": 1.8,
                "ocr_mask_status": "located",
                "caption_geometry_verified": True,
                "tracking_blocks": [
                    {
                        "start": 0.0,
                        "end": 2.4,
                        "x_pct": 0.18,
                        "max_x_pct": 0.82,
                        "y_pct": 0.72,
                        "max_y_pct": 0.79,
                        "is_subtitle": True,
                        "in_subtitle_band": True,
                        "is_packaging": False,
                    }
                ],
            }
        ]
        ass_file = tmp_path / "linux_blur.ass"
        audit = generate_cloud_ass(
            segments,
            ass_file,
            video_w=720,
            video_h=1280,
            main_y_pct=0.75,
            video_duration=3.0,
            cover_style="localized_blur",
            font_name="Arial",
        )
        assert audit["passed"] is True
        raw_ass = ass_file.read_text(encoding="utf-8-sig")
        assert ASS_BLUR_MARKER in raw_ass
    finally:
        subtitle_layout.subtitle_measure.cache_clear()


def test_shorten_translation_sets_bounded_http_options_and_demotes_503_model(monkeypatch):
    recorded_clients: list[dict] = []
    recorded_models_per_call: list[list[str]] = []
    current_round_models: list[str] = []

    class FakeModels:
        def generate_content(self, **kwargs):
            model = kwargs["model"]
            current_round_models.append(model)
            if model == "gemini-3-flash-preview":
                raise RuntimeError("503 UNAVAILABLE")
            return SimpleNamespace(text=json.dumps([{"id": 1, "text": "Ngắn gọn hơn"}]))

    class FakeClient:
        def __init__(self, api_key=None, http_options=None):
            recorded_clients.append({"api_key": api_key, "http_options": http_options})
            self.models = FakeModels()

    fake_types = types.ModuleType("google.genai.types")
    fake_types.HttpRetryOptions = lambda **kw: SimpleNamespace(**kw)
    fake_types.HttpOptions = lambda **kw: SimpleNamespace(**kw)
    fake_types.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_genai.types = fake_types
    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setattr(full_pipeline.time, "sleep", lambda *_: None)

    full_pipeline._SHORTEN_DEMOTED_MODELS.clear()
    try:
        seg = {"id": 1, "text": "个位数的价格非常划算", "translated_text": "Giá chỉ một chữ số rất hời luôn"}
        current_round_models = []
        res1 = full_pipeline._shorten_translation_for_timing(seg, 1.0, "AIzaSyTestKey1234567890")
        recorded_models_per_call.append(list(current_round_models))
        assert res1 == "Ngắn gọn hơn"
        assert "gemini-3-flash-preview" in full_pipeline._SHORTEN_DEMOTED_MODELS
        assert recorded_clients[0]["http_options"].timeout == 20000
        assert recorded_clients[0]["http_options"].retry_options.attempts == 1

        current_round_models = []
        res2 = full_pipeline._shorten_translation_for_timing(seg, 1.0, "AIzaSyTestKey1234567890")
        recorded_models_per_call.append(list(current_round_models))
        assert res2 == "Ngắn gọn hơn"
        assert recorded_models_per_call[1][0] == "gemini-3.1-flash-lite-preview"
    finally:
        full_pipeline._SHORTEN_DEMOTED_MODELS.clear()


def test_pytest_guard_never_leaks_deliverables_into_production_d_toolv1_cloud(tmp_path: Path, monkeypatch):
    from backend.cloud.cloud_config import DEFAULT_CONFIG
    from tests.test_cloud_phase5_quality_export import _Phase5Adapter, _make_sample_video

    out_dir = tmp_path / "isolated_deliverables"
    out_dir.mkdir(parents=True)
    src_vid = _make_sample_video(tmp_path / "in", "same_name.mp4")

    monkeypatch.setenv("AUTODUB_CLOUD_OUTPUT_DIR", r"D:\ToolV1_Cloud")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))

    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    orch = CloudOrchestrator(store=store, adapter=_Phase5Adapter(), allowed_roots=[tmp_path])
    job = store.enqueue(str(src_vid))
    claimed = store.claim_job(job.job_id, owner_id="w_guard", lease_seconds=300)
    assert claimed is not None

    assert orch.process_job(claimed, owner_id="w_guard") is True
    saved = store.get_job(job.job_id)
    assert saved is not None and saved.state == "completed"
    assert Path(saved.result_path).parent == out_dir
    assert Path(saved.result_path).is_file()
    prod_dir = Path(r"D:\ToolV1_Cloud")
    if prod_dir.is_dir():
        assert not any(job.job_id in p.name for p in prod_dir.iterdir())


def test_bundled_local_fonts_and_50_cue_localized_blur_ffmpeg_render(tmp_path: Path, monkeypatch):
    import shutil
    import subprocess
    from backend.cloud.cloud_config import DEFAULT_CONFIG
    from backend.cloud.cloud_orchestrator import _resolve_local_font_files_for_bundle
    from backend.cloud.gpu_scripts import setup_env
    from tests.test_cloud_phase4_env_preflight import _SpyCloudAdapter, _make_sample_video

    _resolve_local_font_files_for_bundle.cache_clear()
    win_arial = Path(r"C:\Windows\Fonts\arial.ttf")
    win_arialbd = Path(r"C:\Windows\Fonts\arialbd.ttf")
    bundled_fonts = dict(_resolve_local_font_files_for_bundle("Arial"))
    if win_arial.is_file():
        assert "fonts/arial.ttf" in bundled_fonts
    if win_arialbd.is_file():
        assert "fonts/arialbd.ttf" in bundled_fonts
    if not bundled_fonts:
        pytest.skip("No subtitle fonts were discoverable via _resolve_local_font_files_for_bundle('Arial')")

    # 1. Verify CloudOrchestrator packages host fonts inside cloud_bundle.tar.gz + bundle_manifest.json
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    src_vid = _make_sample_video(tmp_path / "input", "font_bundle_test.mp4")

    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    adapter = _SpyCloudAdapter()
    orch = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])
    job = store.enqueue(str(src_vid), video_name=src_vid.name, gpu_type="T4", cu_estimated=0.5)
    claimed = store.claim_job(job.job_id, owner_id="w_font")
    assert claimed is not None
    assert orch.process_job(claimed, owner_id="w_font") is True

    remote_workdir = tmp_path / "remote_workdir"
    remote_workdir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(adapter.uploaded["cloud_bundle.tar.gz"]), mode="r:gz") as tar:
        names = set(tar.getnames())
        for arc_rel in bundled_fonts:
            assert arc_rel in names
        bm = json.loads(tar.extractfile("bundle_manifest.json").read().decode("utf-8"))
        manifest_names = {entry["name"] for entry in bm["files"]}
        for arc_rel in bundled_fonts:
            assert arc_rel in manifest_names
            member = tar.getmember(arc_rel)
            out_font = remote_workdir / arc_rel
            out_font.parent.mkdir(parents=True, exist_ok=True)
            out_font.write_bytes(tar.extractfile(member).read())

    # 2. Verify setup_env stages bundled fonts & builds 50-cue localized_blur ASS + filter graph
    staged_count = setup_env._stage_bundled_subtitle_fonts(remote_workdir, allow_local_test=True)
    assert staged_count == len(bundled_fonts)
    for arc_rel in bundled_fonts:
        assert (remote_workdir / Path(arc_rel).name).exists()

    monkeypatch.chdir(remote_workdir)
    in_mp4 = remote_workdir / "input.mp4"
    in_wav = remote_workdir / "final_audio.wav"
    ass_out = remote_workdir / "subtitles_vi.ass"
    out_mp4 = remote_workdir / "output_video.mp4"

    segments = []
    for idx in range(50):
        st = round(idx * 0.24, 3)
        et = round(st + 0.22, 3)
        shift_x = (idx % 11) * 0.008
        shift_y = (idx % 13) * 0.006
        extra = (" cực kỳ nét đẹp tự nhiên cho mọi người" if idx % 3 == 0 else (" siêu rõ" if idx % 2 else ""))
        segments.append({
            "id": idx + 1,
            "start": st,
            "end": et,
            "text": f"测试第{idx + 1}句字幕",
            "translated_text": f"Câu {idx + 1}: Mua 9,9 tệ{extra}",
            "tts_duration": 0.20,
            "ocr_mask_status": "located",
            "caption_geometry_verified": True,
            "tracking_blocks": [{
                "start": st,
                "end": et,
                "x_pct": round(0.06 + shift_x, 4),
                "max_x_pct": round(0.94 - (idx % 7) * 0.01, 4),
                "y_pct": round(0.66 + shift_y, 4),
                "max_y_pct": round(0.75 + shift_y + (idx % 5) * 0.006, 4),
                "is_subtitle": True,
                "in_subtitle_band": True,
                "is_packaging": False,
            }],
        })

    blur_cfg = {"sub_blur_radius": 16, "sub_blur_padding_x": 0.015, "sub_blur_padding_y": 0.008}
    assert full_pipeline.build_ass_subtitles_v1(
        segments,
        str(ass_out),
        video_w=720,
        video_h=1280,
        main_y_pct=0.76,
        video_duration=12.0,
        cover_style="localized_blur",
        blur_settings=blur_cfg,
        font_name="Arial",
        font_weight=2,
    ) is True

    canvas_cfg = {
        "aspect_ratio": "9:16",
        "sub_cover_style": "localized_blur",
        **blur_cfg,
        "delogo_mode": "off",
    }
    filter_graph, out_w, out_h, active_cover = full_pipeline._build_v1_video_filter_graph(
        720, 1280, ass_path=str(ass_out), main_y_pct=0.76, canvas_cfg=canvas_cfg,
    )
    assert (out_w, out_h) == (720, 1280)
    assert active_cover == "localized_blur"
    assert ":fontsdir='" in filter_graph
    assert len(filter_graph) > 8000

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("FFmpeg/FFprobe binaries not installed on CI runner; font bundle and filter graph verified")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=navy:s=720x1280:r=25:d=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(in_mp4)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
         "-ac", "2", "-c:a", "pcm_s16le", str(in_wav)],
        check=True, capture_output=True, text=True, timeout=30,
    )

    full_pipeline.step7_render_video(
        str(in_mp4),
        str(in_wav),
        str(out_mp4),
        ass_path=str(ass_out),
        main_y_pct=0.76,
        canvas_cfg=canvas_cfg,
    )
    qc = full_pipeline.verify_rendered_video_qc(str(out_mp4), expected_duration=12.0)
    assert qc["width"] == 720 and qc["height"] == 1280
    assert qc["video_codec"] == "h264"
    assert qc["audio_codec"] == "aac"
    assert 11.0 <= qc["duration_seconds"] <= 13.0
    assert qc["size_bytes"] > 10000


def test_step5_parallel_tts_and_parallel_shortening_preserve_order_and_key_spread(tmp_path: Path, monkeypatch):
    import threading
    import time

    lock = threading.Lock()
    active_tts = [0]
    peak_tts = [0]
    seen_key_offsets: list[int] = []

    def fake_capcut_tts(text: str, voice: str, out_file: str, **kwargs) -> bool:
        with lock:
            active_tts[0] += 1
            peak_tts[0] = max(peak_tts[0], active_tts[0])
        try:
            time.sleep(0.04)
            Path(out_file).write_bytes(b"ID3" + (b"S" * 600 if "Gọn" in text else b"L" * 600))
            return True
        finally:
            with lock:
                active_tts[0] -= 1

    def fake_validate(path_str: str, **kwargs) -> float:
        data = Path(path_str).read_bytes()
        return 0.95 if b"S" in data else 2.10

    def fake_shorten(segment: dict, window_s: float, gemini_key: str, *, measured_duration_s=None, key_offset: int = 0) -> str:
        with lock:
            seen_key_offsets.append(int(key_offset))
        time.sleep(0.03)
        return f"Gọn câu {segment['id']}"

    monkeypatch.setenv("CLOUD_TTS_CONCURRENCY", "5")
    monkeypatch.setattr(full_pipeline, "generate_capcut_tts_single", fake_capcut_tts)
    monkeypatch.setattr(full_pipeline, "_trim_tts_silence_in_process", lambda p, **kw: p)
    monkeypatch.setattr(full_pipeline, "validate_tts_audio_file", fake_validate)
    monkeypatch.setattr(full_pipeline, "_shorten_translation_for_timing", fake_shorten)

    segments = [
        {
            "id": i + 1,
            "start": round(i * 1.2, 3),
            "end": round(i * 1.2 + 1.0, 3),
            "text": f"测试句子{i + 1}",
            "translated_text": f"Bản dịch ban đầu hơi dài câu số {i + 1}",
        }
        for i in range(8)
    ]
    summary = full_pipeline.step5_generate_tts(
        segments,
        "BV562_streaming",
        str(tmp_path / "tts_parallel"),
        video_duration=9.6,
        gemini_key="AIzaSySyntheticKey_1234567890",
    )
    assert peak_tts[0] >= 3, f"Expected concurrent CapCut TTS synthesis, got peak={peak_tts[0]}"
    assert sorted(seen_key_offsets) == list(range(8))
    assert [c["id"] for c in summary["cues"]] == list(range(1, 9))
    assert all(s["translated_text"] == f"Gọn câu {s['id']}" for s in segments)


def test_caption_geometry_sequential_grab_and_jitter_coalescing_preserves_coverage(tmp_path: Path, monkeypatch):
    import cv2
    import numpy as np
    from backend.cloud.gpu_scripts import caption_geometry as geometry

    # 30 frames @ 10fps = 3.0s:
    # frames 0..19: static caption wobbling across the old 8px bucket boundary (147.6px vs 148.4px on W=720)
    # frames 20..24: empty gap
    # frames 25..29: second cue caption
    pos = [0]
    seek_calls: list[int] = []
    grab_calls = [0]

    class FakeSeqCapture:
        def isOpened(self):
            return True

        def release(self):
            return None

        def set(self, key, val):
            if key == cv2.CAP_PROP_POS_FRAMES:
                seek_calls.append(int(val))
                pos[0] = int(val)
                return True
            return False

        def grab(self):
            grab_calls[0] += 1
            pos[0] += 1
            return pos[0] <= 30

        def read(self):
            idx = pos[0]
            pos[0] += 1
            return True, np.full((10, 10, 3), idx, dtype=np.uint8)

        def get(self, key):
            return {
                cv2.CAP_PROP_FRAME_WIDTH: 720.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 1280.0,
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_COUNT: 30.0,
                cv2.CAP_PROP_POS_FRAMES: float(pos[0]),
                cv2.CAP_PROP_POS_MSEC: float((pos[0] - 1) * 100.0),
            }[key]

    def fake_observe(_reader, frame, **_kw):
        idx = int(frame[0, 0, 0])
        if idx < 20:
            # Wobble across 148.0px (18.5 * 8) which previously triggered false bisections & split masks
            x1 = (147.6 if idx % 2 == 0 else 148.6) / 720.0
            x2 = (572.4 if idx % 2 == 0 else 571.4) / 720.0
            return [[x1, 0.750, x2, 0.790]]
        if idx < 25:
            return []
        return [[0.250, 0.750, 0.750, 0.790]]

    monkeypatch.setattr(cv2, "VideoCapture", lambda _p: FakeSeqCapture())
    monkeypatch.setattr(geometry, "observe_caption_boxes", fake_observe)

    segments = [
        {"id": 1, "start": 0.0, "end": 1.9, "text": "第一句", "translated_text": "Câu một", "tts_duration": 1.2},
        {"id": 2, "start": 2.5, "end": 2.95, "text": "第二句", "translated_text": "Câu hai", "tts_duration": 0.4},
    ]
    report = geometry.acquire_caption_geometry(
        "fake.mp4",
        segments,
        None,
        band_top=0.74,
        band_bottom=0.80,
        duration=3.0,
        audit_path=tmp_path / "geom.json",
        sample_step=0.4,
    )
    geometry.validate_caption_evidence(report, cue_count=2)
    assert seek_calls[0] == 0
    assert grab_calls[0] >= 15, "Coarse scan should step sequentially via grab() instead of random GOP seeking"
    # Cue 1's 20 frames of 1px wobble coalesce into 1 outer-union tracking block, preserving the [2.0, 2.5) gap
    assert len(segments[0]["tracking_blocks"]) == 1
    assert segments[0]["tracking_blocks"][0]["x_pct"] == pytest.approx(147.6 / 720.0)
    assert segments[0]["tracking_blocks"][0]["max_x_pct"] == pytest.approx(572.4 / 720.0)
    assert segments[0]["tracking_blocks"][0]["end"] <= 2.0 + 1e-6
    assert len(segments[1]["tracking_blocks"]) == 1
    assert segments[1]["tracking_blocks"][0]["start"] >= 2.5 - 1e-6
    assert report["source_mask_count"] == 2

    ass_out = tmp_path / "coalesced.ass"
    audit = generate_cloud_ass(
        segments,
        ass_out,
        video_w=720,
        video_h=1280,
        main_y_pct=0.75,
        video_duration=3.0,
        cover_style="localized_blur",
        font_name="Arial",
    )
    assert audit["passed"] is True
    assert audit["source_mask_count"] == 2


def test_setup_env_prefers_bootstrapped_uv_and_falls_back_to_pip(tmp_path: Path, monkeypatch):
    from backend.cloud.gpu_scripts import setup_env

    workdir = tmp_path / "content"
    uv_dir = workdir / ".autodub_bootstrap" / "bin"
    uv_dir.mkdir(parents=True)
    uv_bin = uv_dir / "uv"
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    req_file = workdir / "requirements_cloud.txt"
    req_file.write_text("requests==2.34.2\n", encoding="utf-8")
    manifest = {"requirements_file": "requirements_cloud.txt", "wheels": []}

    monkeypatch.setattr(setup_env, "os", SimpleNamespace(name="posix", environ={}))
    calls: list[list[str]] = []

    def fake_uv_ok(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="uv installed", stderr="")

    monkeypatch.setattr(setup_env.subprocess, "run", fake_uv_ok)
    ok, rc, _, msg = setup_env._install_dependencies(manifest, workdir, pip_timeout=60)
    assert ok is True and rc == 0 and "uv" in msg
    assert len(calls) == 1 and calls[0][0] == str(uv_bin)

    # Fallback to pip if uv returns non-zero
    calls.clear()

    def fake_uv_fail_then_pip_ok(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == str(uv_bin):
            return SimpleNamespace(returncode=1, stdout="", stderr="uv error")
        return SimpleNamespace(returncode=0, stdout="pip installed", stderr="")

    monkeypatch.setattr(setup_env.subprocess, "run", fake_uv_fail_then_pip_ok)
    ok2, rc2, _, _ = setup_env._install_dependencies(manifest, workdir, pip_timeout=60)
    assert ok2 is True and rc2 == 0
    assert len(calls) == 2 and calls[0][0] == str(uv_bin) and "-m" in calls[1]


def test_orchestrator_warm_session_handoff_across_batch_and_auto_release_on_finish(tmp_path: Path, monkeypatch):
    from backend.cloud.cloud_config import DEFAULT_CONFIG
    from tests.test_cloud_phase5_quality_export import _Phase5Adapter, _make_sample_video

    out_dir = tmp_path / "batch_out"
    out_dir.mkdir(parents=True)
    vid1 = _make_sample_video(tmp_path / "in", "batch_1.mp4")
    vid2 = _make_sample_video(tmp_path / "in", "batch_2.mp4")

    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_handoff_seconds", 60)
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_keepalive_seconds", 0)

    class _WarmTrackingAdapter(_Phase5Adapter):
        def __init__(self):
            super().__init__()
            self.reset_calls: list[str] = []

        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None):
            if len(argv) >= 3 and argv[0] == "python3" and argv[1] == "-c" and "keep=" in argv[2]:
                self.reset_calls.append(session_name)
                return True, "sanitized"
            return super().exec_argv(session_name, argv, cwd=cwd, env_vars=env_vars, timeout=timeout)

    store = CloudJobStore(db_path=str(tmp_path / "batch.db"), rollout_controlled=False)
    adapter = _WarmTrackingAdapter()
    orch = CloudOrchestrator(
        store=store,
        adapter=adapter,
        allowed_roots=[tmp_path],
        enable_warm_session_reuse=True,
    )

    j1 = store.enqueue(str(vid1), gpu_type="T4")
    j2 = store.enqueue(str(vid2), gpu_type="T4")

    c1 = store.claim_job(j1.job_id, owner_id="w_batch", lease_seconds=300)
    assert c1 is not None
    # While j1 runs, j2 is already queued -> j1 parks the warm T4 session instead of destroying it!
    assert orch.process_job(c1, owner_id="w_batch") is True
    s1 = store.get_job(j1.job_id)
    assert s1 is not None and s1.state == "completed" and s1.cleanup_status == "verified"
    assert store.get_cu_budget_info()["unreconciled_count"] == 0
    assert len(adapter.created_sessions) == 1
    assert adapter.sessions_stopped == []
    assert len(orch.get_warm_session_names()) == 1

    c2 = store.claim_job(j2.job_id, owner_id="w_batch", lease_seconds=300)
    assert c2 is not None
    # j2 adopts the warm session (0 new create_session calls!) and stops it cleanly because queue is now empty!
    assert orch.process_job(c2, owner_id="w_batch") is True
    s2 = store.get_job(j2.job_id)
    assert s2 is not None and s2.state == "completed" and s2.cleanup_status == "verified"
    assert s2.session_name == s1.session_name
    assert len(adapter.created_sessions) == 1
    assert adapter.sessions_stopped == [s1.session_name]
    assert orch.get_warm_session_names() == ()
    assert len(adapter.reset_calls) >= 2
    assert store.get_cu_budget_info()["unreconciled_count"] == 0


def test_cross_orchestrator_warm_handoff_and_crash_orphan_recovery(tmp_path: Path, monkeypatch):
    from backend.cloud.cloud_config import DEFAULT_CONFIG
    from backend.cloud.cloud_contract import get_cloud_status_snapshot
    from tests.test_cloud_phase5_quality_export import _Phase5Adapter, _make_sample_video

    out_dir = tmp_path / "shared_out"
    out_dir.mkdir(parents=True)
    vid1 = _make_sample_video(tmp_path / "in", "cross_1.mp4")
    vid2 = _make_sample_video(tmp_path / "in", "cross_2.mp4")

    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_handoff_seconds", 90)
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_keepalive_seconds", 0)

    class _SharedAdapter(_Phase5Adapter):
        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None):
            if len(argv) >= 3 and argv[0] == "python3" and argv[1] == "-c" and "keep=" in argv[2]:
                return True, "sanitized"
            return super().exec_argv(session_name, argv, cwd=cwd, env_vars=env_vars, timeout=timeout)

    db_file = tmp_path / "shared_cloud.db"
    store_bot = CloudJobStore(db_path=str(db_file), rollout_controlled=False)
    store_dash = CloudJobStore(db_path=str(db_file), rollout_controlled=False)
    adapter = _SharedAdapter()

    pending_bot = {"waiting": True}
    orch_dash = CloudOrchestrator(
        store=store_dash,
        adapter=adapter,
        allowed_roots=[tmp_path],
        enable_warm_session_reuse=True,
    )
    orch_bot = CloudOrchestrator(
        store=store_bot,
        adapter=adapter,
        allowed_roots=[tmp_path],
        enable_warm_session_reuse=True,
        has_pending_work_callback=lambda g: pending_bot["waiting"] and g == "T4",
    )

    # 1. Dashboard runs Job 1 while Telegram Bot has a pending pre-claim video -> parks warm VM in SQLite!
    j1 = store_dash.enqueue(str(vid1), gpu_type="T4")
    c1 = store_dash.claim_job(j1.job_id, owner_id="w_dash", lease_seconds=300)
    assert c1 is not None
    assert orch_dash.process_job(c1, owner_id="w_dash") is True
    warm_names = orch_bot.get_warm_session_names()
    assert len(warm_names) == 1
    snap = get_cloud_status_snapshot(
        store_dash,
        usage_fn=adapter.get_usage,
        sessions_fn=lambda: [{"name": s} for s in adapter.created_sessions if s not in adapter.sessions_stopped],
    )
    assert [w["session_name"] for w in snap["warm_sessions"]] == list(warm_names)

    # 2. Telegram Bot's separate orchestrator pops and reuses Dashboard's warm VM!
    pending_bot["waiting"] = False
    j2 = store_bot.enqueue(str(vid2), gpu_type="T4")
    c2 = store_bot.claim_job(j2.job_id, owner_id="w_bot", lease_seconds=300)
    assert c2 is not None
    assert orch_bot.process_job(c2, owner_id="w_bot") is True
    assert len(adapter.created_sessions) == 1
    assert orch_dash.get_warm_session_names() == ()

    # 3. Simulate a crashed host PID leaving an orphaned warm session in SQLite -> reconcile_expired_leases reaps it!
    adapter.created_sessions.append("cjobcrashwarm1")
    with store_dash._connect() as db:
        db.execute(
            """
            INSERT INTO cloud_warm_sessions (
                session_name, gpu_type, source_job_id, parked_at, expires_at, keepalive_mode, owner_pid
            ) VALUES ('cjobcrashwarm1', 'T4', 'job-crashed', ?, ?, 0, 99999999)
            """,
            (1000.0, 9999999999.0),
        )
    rec = store_dash.reconcile_expired_leases(adapter=adapter)
    assert any(r.get("session_name") == "cjobcrashwarm1" and r.get("state") == "warm_reclaimed" for r in rec)
    assert "cjobcrashwarm1" in adapter.sessions_stopped
    assert store_dash.list_warm_sessions() == []


def test_step0_download_fallback_rejects_html_and_invalid_headers(tmp_path: Path, monkeypatch):
    import requests as real_requests

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full_pipeline, "run_cmd", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("yt-dlp blocked")))

    class _FakeResp:
        def __init__(self, body: bytes, ctype: str = "video/mp4"):
            self._body = body
            self.headers = {"Content-Type": ctype}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=65536):
            yield self._body

    # 1. HTML login page disguised under video URL -> DOWNLOAD_FAILED & partial file deleted
    monkeypatch.setattr(real_requests, "get", lambda *a, **kw: _FakeResp(b"<html>login</html>" * 200, "text/html; charset=utf-8"))
    with pytest.raises(full_pipeline.PipelineStageError) as exc1:
        full_pipeline.step0_download_video_from_url("https://example.com/share/1", "dl_html.mp4")
    assert exc1.value.error_code == "DOWNLOAD_FAILED"
    assert not (tmp_path / "dl_html.mp4").exists()

    # 2. Binary garbage without container magic -> DOWNLOAD_FAILED
    monkeypatch.setattr(real_requests, "get", lambda *a, **kw: _FakeResp(b"NOT_A_VIDEO_HEADER" * 200, "application/octet-stream"))
    with pytest.raises(full_pipeline.PipelineStageError) as exc2:
        full_pipeline.step0_download_video_from_url("https://example.com/share/2", "dl_bad.mp4")
    assert exc2.value.error_code == "DOWNLOAD_FAILED"
    assert not (tmp_path / "dl_bad.mp4").exists()

    # 3. Valid MP4 ftyp header > 1024 bytes -> succeeds
    valid_mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"\x00" * 2048)
    monkeypatch.setattr(real_requests, "get", lambda *a, **kw: _FakeResp(valid_mp4, "video/mp4"))
    out = full_pipeline.step0_download_video_from_url("https://example.com/share/3", "dl_ok.mp4")
    assert out == "dl_ok.mp4" and (tmp_path / "dl_ok.mp4").is_file()


def test_step5_retry_keeps_best_text_when_intermediate_retry_is_longer(tmp_path: Path, monkeypatch):
    import struct
    import wave

    def write_wav_ms(path: str, dur_ms: int):
        sr = 16000
        n = int(sr * dur_ms / 1000.0)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(struct.pack(f"<{n}h", *([4000] * n)))

    seen_prev_texts: list[str] = []

    def fake_shorten(seg, window_s, gemini_key, **kwargs):
        seen_prev_texts.append(str(seg["translated_text"]))
        if len(seen_prev_texts) == 1:
            return "Bản rút gọn lần một bị dài hơn"
        return "Siêu gọn"

    def fake_capcut(text, voice, out_file, **kwargs):
        if text == "Câu gốc hơi dài một chút":
            write_wav_ms(out_file, 1300)
        elif text == "Bản rút gọn lần một bị dài hơn":
            write_wav_ms(out_file, 1500)  # Worse than 1300ms!
        else:
            write_wav_ms(out_file, 600)
        return True

    def measure_wav(path: str, **_kw):
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full_pipeline, "_shorten_translation_for_timing", fake_shorten)
    monkeypatch.setattr(full_pipeline, "generate_capcut_tts_single", fake_capcut)
    monkeypatch.setattr(full_pipeline, "validate_tts_audio_file", measure_wav)

    segs = [{"id": 1, "start": 0.0, "end": 0.70, "text": "长句子测试", "translated_text": "Câu gốc hơi dài một chút"}]
    res = full_pipeline.step5_generate_tts(segs, "BV562_streaming", str(tmp_path / "tts"), video_duration=0.80, gemini_key="AIzaTest12345678901234567890")
    assert res["verified_count"] == 1
    # Retry 2 MUST receive original best_text, NOT Retry 1's worse 1.50s candidate!
    assert seen_prev_texts == ["Câu gốc hơi dài một chút", "Câu gốc hơi dài một chút"]
    assert segs[0]["translated_text"] == "Siêu gọn"


def test_write_pipeline_progress_skips_git_repo_root_under_pytest_and_writes_in_tmp(tmp_path: Path, monkeypatch):
    # 1. Inside normal workdir without .git -> writes .pipeline_progress.json atomically with zero leftover .tmp
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    full_pipeline._write_pipeline_progress(75, 5, 8, "Đang tổng hợp TTS")
    assert (work / ".pipeline_progress.json").is_file()
    assert not list(work.glob("*.tmp"))

    # 2. Inside a directory containing .git while PYTEST_CURRENT_TEST is set -> skips polluting git root
    fake_repo = tmp_path / "repo"
    (fake_repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(fake_repo)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_case")
    monkeypatch.delenv("AUTODUB_ALLOW_TEST_PROGRESS_FILE", raising=False)
    full_pipeline._write_pipeline_progress(75, 5, 8, "Không ghi bẩn repo")
    assert not (fake_repo / ".pipeline_progress.json").exists()
