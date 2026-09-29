import pytest
from backend.ai.v1_tech_pronunciation import normalize_text_for_tts
from backend.ai.translation import _contains_cjk


def test_gpu_normalization():
    assert "R T X 40 70" in normalize_text_for_tts("Card RTX4070 rất mạnh")
    assert "R T X 30 70 T i" in normalize_text_for_tts("So sánh RTX3070Ti")
    assert "R T X 50 90" in normalize_text_for_tts("RTX5090 ra mắt")
    assert "G T X 10 80" in normalize_text_for_tts("Huyền thoại GTX1080")
    assert "R X 6600 X T" in normalize_text_for_tts("Radeon RX6600XT giá tốt")
    assert "Su pơ" in normalize_text_for_tts("RTX 4060 Super")


def test_cpu_normalization():
    assert "Core ai 5" in normalize_text_for_tts("Intel Core i5")
    assert "ai 5 13400 F" in normalize_text_for_tts("CPU i5 13400F")
    assert "ai 7 14700 K" in normalize_text_for_tts("CPU i7-14700K")
    assert "Rai zen 7" in normalize_text_for_tts("AMD Ryzen 7")
    assert "X 3 D" in normalize_text_for_tts("Ryzen 7 7800X3D")


def test_units_and_terms():
    assert "2 ghi" in normalize_text_for_tts("Bộ nhớ 2GB")
    assert "8 ghi" in normalize_text_for_tts("VRAM 8 GB")
    assert "16 ghi" in normalize_text_for_tts("16GB RAM")
    assert "vram" in normalize_text_for_tts("Dung lượng VRAM")
    assert "vram" in normalize_text_for_tts("Card 24GB vram")
    assert "vy mạch" in normalize_text_for_tts("chế tạo vi mạch")
    assert "tinh vy" in normalize_text_for_tts("cực kỳ tinh vi")
    assert "vy mô" in normalize_text_for_tts("thế giới vi mô")
    assert "C P U" in normalize_text_for_tts("Nhiệt độ CPU")
    assert "G P U" in normalize_text_for_tts("Tải GPU 100%")
    assert "F P S" in normalize_text_for_tts("Đạt 144 FPS")
    assert "C S 2" in normalize_text_for_tts("Chơi CS2 mượt")
    assert "2 ca" in normalize_text_for_tts("Màn hình 2K")
    assert "4 ca" in normalize_text_for_tts("Độ phân giải 4K")


def test_non_cjk_detection():
    assert not _contains_cjk("2GB")
    assert not _contains_cjk("RTX3070")
    assert not _contains_cjk("CS2")
    assert not _contains_cjk("6GB")
    assert not _contains_cjk("RTX5090")
    assert _contains_cjk("显卡")
    assert _contains_cjk("这是一款2GB显存")
