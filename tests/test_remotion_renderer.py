# -*- coding: utf-8 -*-
"""Smoke + fail-open tests cho Remotion renderer (Node sidecar).

Test render thật được bỏ qua mặc định (tốn ~20-30s mỗi lần); bật bằng biến môi trường
REMOTION_RENDER_TEST=1 để chạy kiểm tra đầu-cuối.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.ai import remotion_renderer as rr


def test_remotion_flag_default_off(monkeypatch):
    monkeypatch.delenv("SCRIPT_REMOTION_ENABLED", raising=False)
    assert rr.remotion_enabled() is False
    monkeypatch.setenv("SCRIPT_REMOTION_ENABLED", "1")
    assert rr.remotion_enabled() is True


def test_build_scene_props_mapping():
    scene = {
        "narration": "  Hãy tưởng tượng một chú mèo.  ",
        "doodle_label": " ĐUA XE ",
        "visual_type": "cat",
    }
    props = rr.build_scene_props("tại sao mèo thích đua xe?", "đua xe", scene, duration_sec=5.2)
    assert props["title"] == "TẠI SAO MÈO THÍCH ĐUA XE?"
    assert props["keywordAccent"] == "ĐUA XE"
    assert props["narration"] == "Hãy tưởng tượng một chú mèo."
    assert props["doodleLabel"] == "ĐUA XE"
    assert props["durationSec"] == 5.2
    assert props["fps"] == 24
    # Nâng cấp 2026-10-18: art + camera + số liệu được phát hiện tự động theo ngữ nghĩa cảnh
    assert props["actionType"] == "race"
    assert isinstance(props["cameraMotion"], str) and props["cameraMotion"]
    assert isinstance(props["numbers"], list)


def test_build_scene_props_detects_chart_numbers():
    scene = {
        "narration": "Có tới 80% người mới bỏ cuộc chỉ sau 30 ngày đầu tiên.",
        "doodle_label": "TỶ LỆ",
        "visual_type": "chart",
    }
    props = rr.build_scene_props("80% BỎ CUỘC", "80%", scene, duration_sec=4.0)
    assert props["actionType"] == "chart_pie"
    assert 80 in props["numbers"]


def test_render_raises_when_cli_missing(monkeypatch, tmp_path):
    """Thiếu CLI -> RuntimeError rõ ràng (caller lùi về PIL), không treo tiến trình."""
    monkeypatch.setattr(rr, "REMOTION_CLI", tmp_path / "khong-ton-tai.js")
    with pytest.raises(RuntimeError):
        rr.render_doodle_scene({"title": "X"}, tmp_path / "out.mp4")


def test_is_available_does_not_throw():
    # Máy đã cài sidecar -> True; máy khác -> False. Chỉ cần không ném lỗi.
    assert rr.is_available() in (True, False)


@pytest.mark.skipif(
    os.getenv("REMOTION_RENDER_TEST", "0") != "1",
    reason="Test render thật tốn ~20-30s; bật bằng REMOTION_RENDER_TEST=1",
)
def test_render_doodle_scene_real(tmp_path):
    if not rr.is_available():
        pytest.skip("Chưa cài Remotion sidecar (cần node + npm install trong tools/render_node)")
    props = rr.build_scene_props(
        title="KIỂM TRA RENDER REMOTION",
        keyword_accent="RENDER",
        scene={"narration": "Đây là cảnh kiểm tra tự động qa.", "doodle_label": "KIỂM TRA"},
        duration_sec=2.0,
    )
    out = rr.render_doodle_scene(props, tmp_path / "test_scene.mp4")
    assert out.exists() and out.stat().st_size > 10_000


@pytest.mark.skipif(
    os.getenv("REMOTION_PIPELINE_TEST", "0") != "1",
    reason="E2E pipeline tốn ~1 phút; bật bằng REMOTION_PIPELINE_TEST=1",
)
def test_render_doodle_video_remotion_pipeline(monkeypatch, tmp_path):
    """E2E: render_doodle_video(whiteboard_mode='remotion') với TTS giả (sine 1.2s).

    - Chặn generate_single_tts để không gọi mạng (sinh mp3 bằng ffmpeg lavfi).
    - Theo dõi đúng số lần engine Remotion được gọi (không âm thầm lùi về PIL).
    """
    if not rr.is_available():
        pytest.skip("Chưa cài Remotion sidecar (cần node + npm install trong tools/render_node)")

    from backend.ai import doodle_engine

    fake_audio = tmp_path / "fake_tts.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", "-q:a", "9", str(fake_audio)],
        check=True,
        capture_output=True,
    )

    def fake_tts(narration, audio_file, voice=None):
        shutil.copyfile(fake_audio, audio_file)

    monkeypatch.setattr(doodle_engine, "generate_single_tts", fake_tts)

    calls = {"count": 0}
    real_render = rr.render_doodle_scene

    def spy_render(props, output_path, **kwargs):
        calls["count"] += 1
        return real_render(props, output_path, **kwargs)

    monkeypatch.setattr(rr, "render_doodle_scene", spy_render)

    # Gương lưu trữ: trỏ về tmp_path để kiểm tra cơ chế copy (không ghi ra ổ D thật khi test).
    mirror_dir = tmp_path / "mirror"
    monkeypatch.setenv("SCRIPT_OUTPUT_COPY_DIR", str(mirror_dir))

    script_data = {
        "title": "KIỂM TRA PIPELINE REMOTION",
        "keyword_accent": "REMOTION",
        "scenes": [
            {"narration": "Cảnh kiểm tra đầu tiên với biểu đồ.", "doodle_label": "TỶ LỆ 50-30-20", "visual_type": "chart"},
            {"narration": "Cảnh kiểm tra thứ hai về ý tưởng.", "doodle_label": "Ý TƯỞNG", "visual_type": "idea"},
        ],
    }
    out_video = tmp_path / "pipeline_out.mp4"
    res = doodle_engine.render_doodle_video(
        script_data,
        output_video_path=out_video,
        whiteboard_mode="remotion",
        fps=24,
    )
    assert res["status"] == "success"
    assert calls["count"] == 2, "Cả 2 cảnh phải render bằng engine Remotion (không lùi về PIL)"
    assert out_video.is_file()
    assert out_video.stat().st_size > 50_000

    # Copy gương phải tồn tại và trùng byte với file gốc.
    copied = res.get("copied_to")
    assert copied, "SCRIPT_OUTPUT_COPY_DIR được đặt thì phải copy gương"
    assert Path(copied).is_file()
    assert Path(copied).read_bytes() == out_video.read_bytes()
