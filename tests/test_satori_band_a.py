# -*- coding: utf-8 -*-
"""Smoke + fail-open tests cho Satori Band A renderer (Node sidecar).

Tự động skip khi máy thiếu node hoặc sidecar chưa `npm install` trong `tools/render_node`.
"""
import pytest

from backend.ai import satori_renderer as sr


def test_satori_flag_default_off(monkeypatch):
    monkeypatch.delenv("SCRIPT_SATORI_BAND_A", raising=False)
    assert sr.satori_enabled() is False
    monkeypatch.setenv("SCRIPT_SATORI_BAND_A", "1")
    assert sr.satori_enabled() is True


def test_render_band_a_vietnamese():
    if not sr.is_available():
        pytest.skip("Node sidecar Satori chưa cài (cần node + npm install trong tools/render_node)")
    img = sr.render_band_a_png(
        title="TẠI SAO MÈO LẠI THÍCH ĐUA XE ĐIỆN?",
        keyword_accent="ĐUA XE",
        accent_color="#FF1E1E",
    )
    assert img is not None
    assert img.size == (720, 420)
    # Phải có hơn 2 màu (nền trắng + chữ) => có nội dung vẽ thật
    colors = img.convert("RGB").getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) > 2


def test_render_band_a_fail_open(monkeypatch):
    """Lỗi node -> trả None để engine lùi về PIL, KHÔNG ném exception."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("node chết")

    monkeypatch.setattr(sr, "_run_node_svg", _boom)
    assert sr.render_band_a_png(title="X") is None


def test_compose_doodle_frame_both_modes(monkeypatch, tmp_path):
    """Smoke: compose frame chạy được cả khi cờ Satori TẮT và BẬT."""
    from backend.ai.doodle_engine import compose_doodle_frame

    scene = {
        "scene_idx": 1,
        "narration": "Kiểm tra hiển thị phụ đề tiếng Việt có dấu.",
        "doodle_label": "THỬ NGHIỆM",
        "visual_type": "generic",
        "icon_keywords": [],
        "layout": "single_centered",
        "image_prompt": "test sketch",
    }

    monkeypatch.setenv("SCRIPT_SATORI_BAND_A", "0")
    out_pil = tmp_path / "frame_pil.png"
    compose_doodle_frame("TIÊU ĐỀ THỬ NGHIỆM", "THỬ NGHIỆM", scene, out_pil)
    assert out_pil.exists() and out_pil.stat().st_size > 0

    monkeypatch.setenv("SCRIPT_SATORI_BAND_A", "1")
    out_satori = tmp_path / "frame_satori.png"
    compose_doodle_frame("TIÊU ĐỀ THỬ NGHIỆM", "THỬ NGHIỆM", scene, out_satori)
    assert out_satori.exists() and out_satori.stat().st_size > 0


def test_compose_prefer_satori_explicit_override(monkeypatch, tmp_path):
    """Tham số prefer_satori cụ thể phải THẮNG biến môi trường (đường UI toggle)."""
    from backend.ai.doodle_engine import compose_doodle_frame

    scene = {
        "scene_idx": 1,
        "narration": "Kiểm tra ghi đè tham số ưu tiên Satori.",
        "doodle_label": "THỬ NGHIỆM",
        "visual_type": "generic",
        "icon_keywords": [],
        "layout": "single_centered",
        "image_prompt": "test sketch",
    }

    # Môi trường TẮT hoàn toàn -> chuẩn PIL
    monkeypatch.setenv("SCRIPT_SATORI_BAND_A", "0")
    out_pil = tmp_path / "override_pil.png"
    compose_doodle_frame("TIÊU ĐỀ THỬ NGHIỆM", "THỬ NGHIỆM", scene, out_pil)

    # Môi trường BẬT nhưng ép prefer_satori=False -> phải cho ra ảnh giống hệt PIL
    monkeypatch.setenv("SCRIPT_SATORI_BAND_A", "1")
    out_forced_pil = tmp_path / "override_forced_pil.png"
    compose_doodle_frame("TIÊU ĐỀ THỬ NGHIỆM", "THỬ NGHIỆM", scene, out_forced_pil, prefer_satori=False)

    assert out_forced_pil.exists() and out_forced_pil.stat().st_size > 0
    assert out_pil.read_bytes() == out_forced_pil.read_bytes()
