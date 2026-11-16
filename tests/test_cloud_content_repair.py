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
