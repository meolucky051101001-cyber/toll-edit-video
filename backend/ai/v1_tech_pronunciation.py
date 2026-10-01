"""
v1_tech_pronunciation.py - Chuẩn hóa phát âm thuật ngữ công nghệ (Card màn hình, CPU, GPU, VRAM, đơn vị) cho AI TTS (CapCut / Edge).
Giữ nguyên phụ đề hiển thị (ASS / Subtitle) nhưng tối ưu văn bản gửi cho AI đọc tự nhiên, chuẩn văn nói công nghệ Việt Nam.
"""

import os
import re
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

GLOSSARY_FILE = Path(os.getenv("AUTODUB_WORKSPACE", r"C:\tool v1\workspace")) / "bot_system" / "tech_glossary.json"

DEFAULT_CUSTOM_REPLACEMENTS = {
    "VRAM": "vram",
    "vram": "vram",
    "V ram": "vram",
    "v ram": "vram",
    "vi ram": "vram",
    "VI RAM": "vram",
    "vi": "vy",
    "VI": "vy",
    "GPU": "G P U",
    "gpu": "G P U",
    "CPU": "C P U",
    "cpu": "C P U",
    "FPS": "F P S",
    "fps": "F P S",
    "DLSS": "D L S S",
    "dlss": "D L S S",
    "FSR": "F S R",
    "fsr": "F S R",
    "CS2": "C S 2",
    "cs2": "C S 2",
    "CS:GO": "C S GO",
    "PUBG": "P U B G",
    "SSD": "S S D",
    "HDD": "H D D",
    "Ray Tracing": "Rây Trây xing",
    "ray tracing": "Rây Trây xing",
}


def load_tech_glossary() -> dict:
    glossary = dict(DEFAULT_CUSTOM_REPLACEMENTS)
    if GLOSSARY_FILE.is_file():
        try:
            custom = json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
            if isinstance(custom, dict):
                glossary.update(custom)
        except Exception as e:
            logger.warning("Lỗi đọc tech_glossary.json: %s", e)
    return glossary


def normalize_text_for_tts(text: str) -> str:
    """
    Chuẩn hóa văn bản trước khi đưa sang TTS để đọc chuẩn xác các tên card đồ họa, CPU, RAM, đơn vị:
    - RTX4070 -> R T X 40 70
    - RTX3070Ti -> R T X 30 70 T i
    - RX6600XT -> R X 6600 X T
    - 2GB / 8GB / 16GB -> 2 ghi / 8 ghi / 16 ghi
    - Core i5 13400F -> Core ai 5 13400 F
    - Ryzen 7 7800X3D -> Rai zen 7 7800 X 3 D
    """
    if not text or not isinstance(text, str):
        return text or ""

    res = text

    # 1. Tách chữ dính liền với số trong tên phần cứng phổ biến
    # Ví dụ: RTX4070 -> RTX 4070, RX6600XT -> RX 6600 XT, ArcA750 -> Arc A 750
    res = re.sub(r'\b(RTX|GTX|RX|Arc)(\d{3,4})([A-Za-z]*)\b', r'\1 \2 \3', res, flags=re.I)
    res = re.sub(r'\bi([3579])[- ]?(\d{4,5})([A-Za-z]*)\b', r'ai \1 \2 \3', res, flags=re.I)

    # 2. Tách số hiệu card đồ họa thành 2 cặp số (4070 -> 40 70, 3080 -> 30 80, 5090 -> 50 90, 1080 -> 10 80)
    # Giúp AI đọc thành "bốn mươi bảy mươi" thay vì đọc "bốn nghìn không trăm bảy mươi"
    res = re.sub(r'\b(RTX|GTX)\s+(\d{2})(\d{2})\b', r'\1 \2 \3', res, flags=re.I)

    # 3. Phân tách rõ các tiền tố và hậu tố card màn hình
    res = re.sub(r'\bRTX\b', 'R T X', res, flags=re.I)
    res = re.sub(r'\bGTX\b', 'G T X', res, flags=re.I)
    res = re.sub(r'\bRX\b', 'R X', res, flags=re.I)
    res = re.sub(r'\bTi\b', 'T i', res)
    res = re.sub(r'\bXT\b', 'X T', res)
    res = re.sub(r'\bSuper\b', 'Su pơ', res, flags=re.I)

    # 4. Vi xử lý (CPU)
    res = re.sub(r'\bCore\s*i([3579])\b', r'Core ai \1', res, flags=re.I)
    res = re.sub(r'\bRyzen\s*([3579])\s*(\d{4})([A-Za-z0-9]*)\b', r'Rai zen \1 \2 \3', res, flags=re.I)
    res = re.sub(r'\bRyzen\s*([3579])\b', r'Rai zen \1', res, flags=re.I)
    res = re.sub(r'(?:(?<=\d)|(?<=\s)|\b)X3D\b', ' X 3 D', res, flags=re.I)

    # 5. Đơn vị dung lượng và tần số
    # GB -> ghi (chuẩn văn nói công nghệ Việt Nam, ngắn gọn, không bị đè sub)
    res = re.sub(r'(\d+)\s*(?:GB|gb|Gb)\b', r'\1 ghi', res)
    res = re.sub(r'(\d+)\s*(?:TB|tb|Tb)\b', r'\1 tê', res)
    res = re.sub(r'(\d+)\s*(?:MB|mb|Mb)\b', r'\1 mê', res)
    res = re.sub(r'(\d+)\s*(?:GHz|ghz)\b', r'\1 ghi ga héc', res)
    res = re.sub(r'(\d+)\s*(?:MHz|mhz)\b', r'\1 mê ga héc', res)
    res = re.sub(r'(\d+)\s*(?:Hz|hz)\b', r'\1 héc', res)

    # Độ phân giải
    res = re.sub(r'\b2K\b', '2 ca', res, flags=re.I)
    res = re.sub(r'\b4K\b', '4 ca', res, flags=re.I)
    res = re.sub(r'\b8K\b', '8 ca', res, flags=re.I)

    # 6. Chuẩn hóa VRAM thành 'vram' và chữ vi thành 'vy' (tránh CapCut đọc nhầm thành số La Mã 5 hoặc 6)
    res = re.sub(r'\b(?:VRAM|vram|V-RAM|v-ram|V\s+ram|v\s+ram|vi\s+ram|VI\s+RAM)\b', 'vram', res, flags=re.I)
    res = re.sub(r'\bvi\b', 'vy', res)
    res = re.sub(r'\bVi\b', 'Vy', res)
    res = re.sub(r'\bVI\b', 'vy', res)

    # 7. Thay thế từ điển mở rộng
    custom_map = load_tech_glossary()
    for word, replacement in custom_map.items():
        pattern = r'\b' + re.escape(word) + r'\b'
        res = re.sub(pattern, replacement, res)

    # 7. Dọn dẹp khoảng trắng thừa
    res = re.sub(r'\s+', ' ', res).strip()
    return res
