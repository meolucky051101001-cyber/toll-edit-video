from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from backend.cloud.gpu_scripts import full_pipeline as pipeline


@pytest.mark.parametrize("source,translation", [
    ("个位数的价格", "Giá chỉ vài nghìn"),
    ("只要三元", "Chỉ ba nghìn đồng"),
    ("这个很便宜", "Món này chỉ năm tệ"),
    ("价格为10元", "Giá mười đô la"),
])
def test_translation_cannot_invent_currency_or_monetary_scale(source, translation):
    with pytest.raises(pipeline.PipelineStageError, match="không có trong nguồn"):
        pipeline.validate_translation_items([{"id": 1, "text": source}], [{"id": 1, "text": translation}])


@pytest.mark.parametrize("source,translation", [
    ("个位数的价格", "Giá chỉ một chữ số"),
    ("只要三元", "Chỉ ba tệ"),
    ("价格为1000元", "Giá một nghìn tệ"),
    ("同时按下", "Đồng thời ấn xuống"),
    ("两层要一样厚", "Hai lớp phải dày đồng đều"),
])
def test_monetary_guard_keeps_supported_units_and_nonmonetary_words(source, translation):
    assert pipeline.validate_translation_items([{"id": 1, "text": source}], [{"id": 1, "text": translation}])[1] == translation


def test_repeated_caption_small_substitution_requires_independent_frames():
    source = "这个盒子比较旧的"
    caption = "这个盒子比较大的"
    rows = [{"text": caption, "prob": p, "sample_time": t}
            for t, p in [(1.1, .78), (1.5, .65), (1.9, .7)]]
    seg = {"text": source}
    assert pipeline._maybe_correct_homophone_cloud(seg, rows, None, seg_start=1, seg_end=2)
    assert seg["text"] == caption
    assert seg["ocr_correction"]["method"] == "three_frame_small_substitution"
    for evidence in [rows[:2], [dict(r, sample_time=1.1) for r in rows],
                     [dict(r, text="这是别的包装说明") for r in rows]]:
        candidate = {"text": source}
        assert not pipeline._maybe_correct_homophone_cloud(candidate, evidence, None, seg_start=1, seg_end=2)
        assert candidate["text"] == source


def test_high_confidence_unrelated_ocr_cannot_replace_spoken_content():
    seg = {"text": "今天我们学习折纸"}
    assert not pipeline._maybe_correct_homophone_cloud(seg,
        [{"text": "立即购买包邮优惠", "prob": .999, "sample_time": 1}], None)
    assert seg["text"] == "今天我们学习折纸"


def test_late_caption_cannot_delete_spoken_prefix_of_coalesced_cue():
    seg = {"text": "拉直 对进去再拉直"}
    assert not pipeline._maybe_correct_homophone_cloud(seg,
        [{"text": "怼进去再拉直", "prob": .98, "sample_time": 1.0}], None)
    assert seg["text"] == "拉直 对进去再拉直"


def test_shortening_retries_transient_failure_and_preserves_translation_checks(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def generate(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise RuntimeError("503 UNAVAILABLE")
        return SimpleNamespace(text='[{"id": 1, "text": "Giá một chữ số"}]')
    monkeypatch.setitem(sys.modules, "google.genai", SimpleNamespace(
        Client=lambda **kw: SimpleNamespace(models=SimpleNamespace(generate_content=generate)),
        types=SimpleNamespace(GenerateContentConfig=lambda **kw: kw)))
    import google
    monkeypatch.setattr(google, "genai", sys.modules["google.genai"], raising=False)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    result = pipeline._shorten_translation_for_timing(
        {"id": 1, "text": "个位数的价格", "translated_text": "Giá chỉ một chữ số thôi"}, 1.5, "synthetic")
    assert result == "Giá một chữ số" and len(calls) == 2


def test_tts_overrun_is_rephrased_then_fails_without_trimming(tmp_path, monkeypatch):
    calls = []
    def generate(text, voice, path, **kwargs):
        Path(path).write_bytes(b"ID3" + b"x" * 200)
        calls.append(text)
        return True
    monkeypatch.setattr(pipeline, "generate_capcut_tts_single", generate)
    monkeypatch.setattr(pipeline, "validate_tts_audio_file", lambda *a, **k: 4.0)
    monkeypatch.setattr(pipeline, "_shorten_translation_for_timing", lambda s, *a: "Vẫn đầy đủ ý.")
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: pytest.fail("Must not trim/cut or mix an overrun"))
    segments = [{"id": 1, "start": 0, "end": 1, "text": "内容", "translated_text": "Bản dịch dài quá khung."},
                {"id": 2, "start": 1.2, "end": 2, "text": "第二句", "translated_text": "Câu tiếp."}]
    with pytest.raises(pipeline.PipelineStageError, match="chặn cắt/đè lời"):
        pipeline.step5_generate_tts(segments, "BV562_streaming", str(tmp_path), video_duration=2.2)
    assert "Vẫn đầy đủ ý." in calls
    assert not (tmp_path / "seg_0001.mp3.fit.mp3").exists()


@pytest.mark.parametrize("source,translation", [
    ("只要几块", "Chỉ vài tệ"),
    ("三块五一个", "Ba tệ rưỡi một cái"),
    ("才十来块", "Chỉ mười mấy tệ"),
    ("只要几米", "Chỉ vài tệ"),
    ("才9.9r", "Chỉ 9,9 tệ"),
    ("¥15包邮", "15 tệ bao ship"),
    ("这样放很稳", "Để thế này rất yên tâm"),
    ("立在桌上不动", "Đứng yên trên bàn"),
    ("质量还不错", "Chất lượng không tệ"),
    ("买了两个表", "Mua hai đồng hồ"),
    ("跟朋友一起", "Cùng một đồng nghiệp"),
])
def test_spoken_rmb_slang_and_vietnamese_homonyms_accepted(source, translation):
    assert pipeline.validate_translation_items([{"id": 1, "text": source}], [{"id": 1, "text": translation}])[1] == translation


def test_step4_repairs_single_cue_monetary_hallucination_without_failing_batch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    prompts = []

    def fake_generate(**kwargs):
        prompts.append(str(kwargs.get("contents")))
        if len(prompts) == 1:
            return SimpleNamespace(
                text='[{"id": 7, "text": "Tìm cốc mỹ tốn mấy chục tệ"}, {"id": 8, "text": "Đổi từ khóa chỉ còn vài tệ"}]'
            )
        return SimpleNamespace(text='[{"id": 8, "text": "Đổi từ khóa chỉ còn giá một chữ số"}]')

    monkeypatch.setitem(
        sys.modules,
        "google.genai",
        SimpleNamespace(
            Client=lambda **kw: SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)),
            types=SimpleNamespace(GenerateContentConfig=lambda **kw: kw),
        ),
    )
    import google

    monkeypatch.setattr(google, "genai", sys.modules["google.genai"], raising=False)
    segs = [
        {"id": 7, "start": 8.0, "end": 10.0, "text": "搜谷美要几十块"},
        {"id": 8, "start": 10.0, "end": 12.0, "text": "换个关键词只要个位数"},
    ]
    out_srt = tmp_path / "vi.srt"
    res = pipeline.step4_translate_subtitles(segs, "synthetic-key", vi_srt_path=str(out_srt))
    assert len(prompts) == 2
    assert res[0]["translated_text"] == "Tìm cốc mỹ tốn mấy chục tệ"
    assert res[1]["translated_text"] == "Đổi từ khóa chỉ còn giá một chữ số"


def test_step4_rotates_across_multiple_gemini_api_keys(tmp_path, monkeypatch):
    from types import SimpleNamespace

    used_keys = []

    def make_client(**kw):
        key_val = kw.get("api_key")

        def gen(**gkw):
            used_keys.append(key_val)
            if key_val == "synthetic-key-alpha-1234":
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return SimpleNamespace(text='[{"id": 1, "text": "Câu dịch xoay vòng key thành công"}]')

        return SimpleNamespace(models=SimpleNamespace(generate_content=gen))

    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key-alpha-1234")
    monkeypatch.setenv("GEMINI_API_KEYS", "synthetic-key-alpha-1234,synthetic-key-beta-5678")
    monkeypatch.setitem(
        sys.modules,
        "google.genai",
        SimpleNamespace(
            Client=make_client,
            types=SimpleNamespace(GenerateContentConfig=lambda **kw: kw),
        ),
    )
    import google

    monkeypatch.setattr(google, "genai", sys.modules["google.genai"], raising=False)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    segs = [{"id": 1, "start": 0.0, "end": 1.5, "text": "你好世界"}]
    res = pipeline.step4_translate_subtitles(segs, "", vi_srt_path=str(tmp_path / "vi.srt"))
    assert used_keys == ["synthetic-key-alpha-1234", "synthetic-key-beta-5678"]
    assert res[0]["translated_text"] == "Câu dịch xoay vòng key thành công"


def test_run_argv_monitored_survives_connection_lost_and_streams_live_progress(monkeypatch):
    import json
    from backend.cloud import colab_bridge

    progress_events: list[tuple[int, str]] = []
    colab_calls: list[list[str]] = []

    monkeypatch.setattr(colab_bridge, "_write_wsl_temp_script", lambda *_a, **_k: None)
    monkeypatch.setattr(colab_bridge, "_remove_wsl_temp_file", lambda *_a, **_k: None)
    monkeypatch.setattr(colab_bridge, "upload_file", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(colab_bridge, "_wait_interruptible", lambda *_a, **_k: False)

    poll_step = {"n": 0}

    def fake_run_colab(colab_args, **_kwargs):
        args_list = list(colab_args)
        colab_calls.append(args_list)
        script_arg = args_list[args_list.index("-f") + 1]
        if "colab_launch_" in script_arg:
            return 0, 'AUTODUB_LAUNCH_JSON:{"launched": true, "sup_pid": 4321}', ""
        poll_step["n"] += 1
        if poll_step["n"] == 1:
            payload = {
                "done": False,
                "returncode": None,
                "progress": {"percent": 68, "message": "[4/8] Dịch phụ đề sang tiếng Việt..."},
                "log_tail": "",
            }
            return 0, "AUTODUB_POLL_JSON:" + json.dumps(payload, ensure_ascii=False), ""
        if poll_step["n"] == 2:
            # Exact Colab CLI IOPub WebSocket drop experienced in production
            return 1, "", 'colab_cli/runtime.py:305 in next_message\nRuntimeError: Connection was lost.'
        payload = {
            "done": True,
            "returncode": 0,
            "progress": {"percent": 80, "message": "[5/8] Tổng hợp giọng đọc (49/49 câu)..."},
            "log_tail": '[8/8] Hoàn tất!\nPIPELINE_RESULT_JSON:{"status": "completed", "segments_count": 49}',
        }
        return 0, "AUTODUB_POLL_JSON:" + json.dumps(payload, ensure_ascii=False), ""

    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", fake_run_colab)

    ok, output = colab_bridge.run_argv_monitored(
        "cjob4620779da1",
        ["/content/.autodub_venv/bin/python", "full_pipeline.py", "--input", "/content/in.mp4"],
        env_vars={"GEMINI_API_KEY": "synthetic-secret-token-xyz"},
        timeout=120,
        progress_callback=lambda pct, msg: progress_events.append((pct, msg)),
        poll_interval_seconds=0.5,
    )
    assert ok is True
    assert "PIPELINE_RESULT_JSON:" in output
    assert "synthetic-secret-token-xyz" not in output
    assert progress_events == [
        (68, "[4/8] Dịch phụ đề sang tiếng Việt..."),
        (80, "[5/8] Tổng hợp giọng đọc (49/49 câu)..."),
    ]
    assert poll_step["n"] == 3


def test_step5_trims_capcut_silence_and_uses_full_gap_without_overrun(tmp_path, monkeypatch):
    import math
    import struct
    import wave

    def write_padded_speech_wav(path: str, lead_ms: int = 240, speech_ms: int = 620, trail_ms: int = 240):
        sr = 16000
        samples = []
        samples.extend([0] * int(sr * lead_ms / 1000.0))
        n_speech = int(sr * speech_ms / 1000.0)
        for i in range(n_speech):
            samples.append(int(12000 * math.sin(2.0 * math.pi * 220.0 * i / sr)))
        samples.extend([0] * int(sr * trail_ms / 1000.0))
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    def fake_capcut(text, voice, out_file, **kwargs):
        write_padded_speech_wav(out_file)  # 1.10s untrimmed, ~0.69s after silence trim
        return True

    def measure_wav_dur(path: str, **_kw):
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())

    monkeypatch.setattr(pipeline, "generate_capcut_tts_single", fake_capcut)
    monkeypatch.setattr(pipeline, "validate_tts_audio_file", measure_wav_dur)
    # Cue 1 ASR span is 0.35s (10.00 -> 10.35), next cue starts at 10.80s (0.76s boundary).
    # Untrimmed 1.10s / 0.75s = 1.47x, or on old +0.5 cap with 240ms x2 dead air could exceed 1.5x;
    # after silence trimming (~0.69s <= 0.75s), speed is 1.0x with zero ffmpeg re-encode needed!
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: pytest.fail("Should fit naturally at 1.0x after silence trim"))

    segs = [
        {"id": 1, "start": 10.0, "end": 10.35, "text": "换个词", "translated_text": "Đổi từ khóa"},
        {"id": 2, "start": 10.80, "end": 12.00, "text": "超便宜", "translated_text": "Siêu rẻ"},
    ]
    summary = pipeline.step5_generate_tts(segs, "BV562_streaming", str(tmp_path), video_duration=12.5)
    assert summary["verified_count"] == 2
    assert segs[0]["tts_duration"] < 0.74
    assert segs[0]["tts_speed"] == 1.0



