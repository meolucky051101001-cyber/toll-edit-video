"""Test nhẹ module telegram_script_bot: các hàm thuần, không gọi mạng/Telegram."""
import importlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

mod = importlib.import_module("telegram_script_bot")


def test_parse_allowlist():
    assert mod.parse_allowlist("") == set()
    assert mod.parse_allowlist("123456789, 987 ; 5 x 66") == {123456789, 987, 5, 66}
    assert mod.parse_allowlist("abc, -1001234") == {-1001234}


def test_engine_mapping_giong_ui_studio():
    # 18/10/2026: đã bỏ bàn tay vẽ — mọi engine đều không tay
    assert mod.engine_params("kinetic") == (False, "kinetic")
    assert mod.engine_params("remotion") == (False, "remotion")
    # Alias cũ ("bàn tay vẽ thật", "3band", "hand") tự chuyển sang Kinetic Reveal
    assert mod.engine_params("3band") == (False, "kinetic")
    assert mod.engine_params("bàn tay") == (False, "kinetic")
    # Lạ / trống -> mặc định Kinetic Reveal
    assert mod.engine_params("bogus") == (False, "kinetic")
    assert mod.engine_key("Kinetic") in mod.ENGINES
    assert "3band" not in mod.ENGINES


def test_mac_dinh_moi_bo_tay_ve_nhieu_canh_giong_bv075():
    assert mod.DEFAULT_VOICE == "BV075_streaming"
    mod.CHAT_SETTINGS.clear()
    settings = mod.get_settings(123456789)
    assert settings["engine"] == "kinetic"
    assert settings["scene_count"] == 12
    assert settings["voice"] == "BV075_streaming"
    assert mod.voice_name("BV075_streaming", voices=[]) == "Thanh Niên Tự Tin (Nam Trẻ)"


def test_one_line_va_clip():
    assert mod.one_line("  a\n\tb   c ", 50) == "a b c"
    assert mod.one_line("abcdefgh", 5).endswith("…")
    assert "\n" in mod.clip("dòng 1\ndòng 2", 50)
    assert len(mod.clip("x" * 100, 30)) == 30


def test_format_script_preview_gioi_han_do_dai():
    script = {
        "title": "Tiêu đề thử nghiệm",
        "keyword_accent": "THỬ",
        "scenes": [
            {"narration": "n" * 400, "doodle_label": "Nhãn " + str(i)}
            for i in range(30)
        ],
    }
    text = mod.format_script_preview(script, "remotion", "Giọng Test")
    assert len(text) <= mod.PREVIEW_MAX_CHARS
    assert "Tiêu đề thử nghiệm" in text
    assert "🎬 Remotion" in text
    assert "😴" not in text  # sanity
    assert "30." in text or "đã rút gọn" in text

    empty = mod.format_script_preview({"scenes": []}, "3band", "X")
    assert "(không có tiêu đề)" in empty


def test_voice_name_khong_can_mang():
    voices = [{"id": "BV562_streaming", "name": "Mai (Nữ Chuẩn)"}]
    assert mod.voice_name("BV562_streaming", voices=voices) == "Mai (Nữ Chuẩn)"
    # Fallback cứng khi không có danh mục
    assert mod.voice_name("multi_male_felipe_uranus_bigtts", voices=[]) == "Nam Trầm (Chuyên Gia)"
    assert mod.voice_name("khong_ton_tai_xyz", voices=[]) == "khong_ton_tai_xyz"


def test_settings_keyboard_dung_cache_khong_goi_mang(monkeypatch):
    monkeypatch.setitem(
        mod._VOICE_CACHE, "items", [{"id": "v1", "name": "Giọng 1"}, {"id": "v2", "name": "Giọng 2"}]
    )
    monkeypatch.setitem(mod._VOICE_CACHE, "at", time.time())
    settings = {"engine": "kinetic", "scene_count": 6, "voice": "v1"}
    kb = mod.settings_keyboard(settings)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "eng:kinetic" in datas
    assert "scenes:6" in datas
    assert "voice:v1" in datas
    assert any(d.startswith("voice:") for d in datas)
