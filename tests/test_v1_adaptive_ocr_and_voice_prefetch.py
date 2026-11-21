from datetime import timedelta
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest


@pytest.mark.parametrize("change,expected", [("steady", 2), ("glyph", 3), ("moved", 3),
                                              ("cut", 3), ("uncertain", 3)])
def test_coarse_skip_requires_consistent_text_pixels_and_endpoints(monkeypatch, change, expected):
    from backend import ocr_utils as ocr
    text = "这是测试字幕"
    seg = SimpleNamespace(content=text)
    batch = ocr._OcrFrameBatch(320, 240, [seg], {}, False, "full", 30, 1, {})
    frames = [np.zeros((240, 320, 3), np.uint8) for _ in range(3)]
    if change == "cut":
        frames[1][:] = 200
    if change == "glyph":
        frames[1][106, 64] = 255
    for i, frame in enumerate(frames):
        batch.captured_frames.append((i / 10, 1., frame, seg, 0, 0))
        batch.captured_occurrences.append(i)
    reads = []
    def read(images):
        out = []
        for frame in images:
            reads.append(id(frame))
            x = 60 if change != "moved" or frame is not frames[2] else 75
            out.append([([[x, 100], [190, 100], [190, 120], [x, 120]], text,
                         .72 if change == "uncertain" else .99)])
        return out
    monkeypatch.setattr(ocr, "_readtext_batch", read)
    rows = batch._read_coarse_batch()
    assert len(reads) == expected
    assert (rows[1] is None) == (change == "steady")


def test_completed_prefetch_only_serves_matching_final_text_and_voice(tmp_path, monkeypatch):
    import asyncio
    from backend.ai import v1_voice_prefetch as warm, voice_cloning as voice, v1_voice_cache as cache
    import shared_state
    monkeypatch.setattr(shared_state, "stop_requested", False)
    completed = threading.Event()
    text = "Xin chào."
    def prepare(spoken, path, voice_id, **kwargs):
        Path(path).write_bytes(b"prepared")
    monkeypatch.setattr(voice, "_run_capcut_tts_once", prepare)
    monkeypatch.setattr(cache, "is_valid_audio", lambda path: True)
    original_replace = warm.os.replace
    def publish(src, dst):
        original_replace(src, dst)
        completed.set()
    monkeypatch.setattr(warm.os, "replace", publish)
    worker = warm.CapCutPrefetch(tmp_path, "mai", 2, lambda: False)
    try:
        worker.submit([text, text])
        assert completed.wait(5)
        assert len(worker.submitted) == 1
    finally:
        worker.close()
    assert not warm.raw_voice_path(tmp_path, text, "other").exists()
    assert not warm.raw_voice_path(tmp_path, "Nội dung đã sửa.", "mai").exists()
    calls = []
    def ordinary(spoken, path, voice_id):
        calls.append((spoken, voice_id))
        Path(path).write_bytes(b"ordinary")
    monkeypatch.setattr(voice, "_run_capcut_tts", ordinary)
    monkeypatch.setattr(voice, "fit_audio_file", AsyncMock(return_value=(None, .7, 1.)))
    seg = SimpleNamespace(index=1, start=timedelta(), content=text)
    asyncio.run(voice.generate_single_tts(seg, tmp_path, "capcut", "mai", "", 1.))
    seg.content = "Nội dung đã sửa."
    asyncio.run(voice.generate_single_tts(seg, tmp_path, "capcut", "mai", "", 1.))
    assert calls == [(seg.content, "mai")]


def test_translation_releases_short_cue_while_it_fits_long_cue(monkeypatch):
    from backend.ai import translation
    events = []
    texts = ["Xin chào.", "Đây là một câu rất dài phải đọc trong thời gian cực kỳ ngắn."]
    def condense(items, **kwargs):
        events.append(("condense", [item["index"] for item in items]))
        return {}
    monkeypatch.setattr(translation, "condense_vietnamese_subtitles_batch", condense)
    result = translation._fit_translation_cues(
        ["你好", "很长的一句话"], texts, [{"max_sec": 3, "max_words": 8}, {"max_sec": .8, "max_words": 2}],
        api_key="", script_mode="default", max_speed=1.5, stop_checker=None, deadline=None,
        on_ready_texts=lambda group: events.append(("ready", group)))
    assert result == texts
    assert events == [("ready", texts[:1]), ("condense", [1])]
