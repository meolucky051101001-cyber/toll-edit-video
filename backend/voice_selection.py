"""Shared dashboard/bot voice choice, read at the start of voice generation."""
import json
import os
import uuid
from pathlib import Path

SUPPORTED_TTS_SOURCES = frozenset({"capcut", "vieneu", "vie-tts", "edge", "fpt"})


class UnsupportedTTSProviderError(ValueError):
    """An explicit provider cannot be replaced with another voice."""


def validate_tts_source(source):
    if not isinstance(source, str) or source not in SUPPORTED_TTS_SOURCES:
        raise UnsupportedTTSProviderError(
            f"Unsupported TTS provider: {source!r}. Choose CapCut, VieNeu, Edge or FPT."
        )


def _resolve_control_dir() -> Path:
    ws_env = os.getenv("AUTODUB_WORKSPACE")
    candidates = []
    if ws_env:
        candidates.extend([Path(ws_env) / "bot_system" / "control", Path(ws_env) / "control"])
    candidates.extend([Path(r"C:\tool v1\workspace\bot_system\control"), Path(r"C:\tool v1\workspace\control")])
    for c in candidates:
        if (c / "voice_catalog.json").is_file():
            return c
    for c in candidates:
        if c.is_dir():
            return c
    return Path(r"C:\tool v1\workspace\control")

BASE = _resolve_control_dir()
ALIASES = {
    'capcut-vi-VN-HoaiMyNeural': 'microsoft-hoaimy',
    'capcut-vi-VN-NamMinhNeural': 'microsoft-namminh',
}

DEFAULT_CATALOG = [
    # --- CAPCUT TTS (Mặc định & Khuyên dùng) ---
    {"id": "capcut-BV562_streaming", "label": "CapCut · Mai (Nữ Chuẩn - Mặc định)", "source": "capcut", "param": "BV562_streaming"},
    {"id": "capcut-BV075_streaming", "label": "CapCut · Thanh Niên Tự Tin (Nam Trẻ)", "source": "capcut", "param": "BV075_streaming"},
    {"id": "capcut-multi_male_felipe_uranus_bigtts", "label": "CapCut · Nam Trầm (Chuyên Gia)", "source": "capcut", "param": "multi_male_felipe_uranus_bigtts"},
    {"id": "capcut-BV421_vivn_streaming", "label": "CapCut · Nhỏ Ngọt Ngào (Nữ Dễ Thương)", "source": "capcut", "param": "BV421_vivn_streaming"},
    {"id": "capcut-BV074_streaming", "label": "CapCut · Cô Gái Hoạt Ngôn (Nữ Năng Động)", "source": "capcut", "param": "BV074_streaming"},
    {"id": "capcut-multi_female_richgirl_uranus_bigtts", "label": "CapCut · Review Phim (Kịch Tính)", "source": "capcut", "param": "multi_female_richgirl_uranus_bigtts"},
    {"id": "capcut-vi_female_huong", "label": "CapCut · Hương (Nữ Phổ Thông)", "source": "capcut", "param": "vi_female_huong"},
    {"id": "capcut-multi_female_banmai", "label": "CapCut · Ban Mai (Tươi Sáng)", "source": "capcut", "param": "multi_female_yangguangnv_uranus_bigtts"},
    {"id": "capcut-BV007_streaming", "label": "CapCut · Nam Phổ Thông", "source": "capcut", "param": "BV007_streaming"},
    {"id": "capcut-BV008_streaming", "label": "CapCut · Nam Trầm Ấm", "source": "capcut", "param": "BV008_streaming"},
    {"id": "capcut-BV074_streaming_dsp", "label": "CapCut · Giọng Bé", "source": "capcut", "param": "BV074_streaming_dsp"},
    {"id": "capcut-BV075_streaming_vibrato_dsp", "label": "CapCut · Việt Méo (Hài Hước)", "source": "capcut", "param": "BV075_streaming_vibrato_dsp"},

    # --- VIENEU / VIE-TTS (48k Studio) ---
    {"id": "vieneu-haidang", "label": "Vie-TTS · Hải Đăng (Nam Bắc 48k)", "source": "vieneu", "param": "Hải Đăng"},
    {"id": "vieneu-maianh", "label": "Vie-TTS · Mai Anh (Nữ Bắc 48k)", "source": "vieneu", "param": "Mai Anh"},
    {"id": "vieneu-trucly", "label": "Vie-TTS · Trúc Ly (Nữ Bắc 48k)", "source": "vieneu", "param": "Trúc Ly"},
    {"id": "vieneu-thienminh", "label": "Vie-TTS · Thiện Minh (Nam Kể chuyện 48k)", "source": "vieneu", "param": "Thiện Minh"},
    {"id": "vieneu-thuydung", "label": "Vie-TTS · Thùy Dung (Nữ Nam 48k)", "source": "vieneu", "param": "Thùy Dung"},
    {"id": "vieneu-adambua", "label": "Vie-TTS · Adam Bựa (Nam Hài hước 48k)", "source": "vieneu", "param": "Adam bựa"},
    {"id": "vieneu-thaison", "label": "Vie-TTS · Thái Sơn (Nam Nam 48k)", "source": "vieneu", "param": "Thái Sơn"},
    {"id": "vieneu-phamtuyen", "label": "Vie-TTS · Phạm Tuyên (Nam Trầm ấm 48k)", "source": "vieneu", "param": "Phạm Tuyên"},
    {"id": "vieneu-ngochuyen", "label": "Vie-TTS · Ngọc Huyền (Nữ Tự nhiên 48k)", "source": "vieneu", "param": "Ngọc Huyền"},
    {"id": "vieneu-ngoctran", "label": "Vie-TTS · Ngọc Trân (Nữ Miền Trung 48k)", "source": "vieneu", "param": "Ngọc Trân"},
    {"id": "vieneu-quangson", "label": "Vie-TTS · Quang Sơn (Nam Miền Trung 48k)", "source": "vieneu", "param": "Quang Sơn"},
    {"id": "vieneu-myduyen", "label": "Vie-TTS · Mỹ Duyên (Nữ Nam Đọc truyện 48k)", "source": "vieneu", "param": "Mỹ Duyên"},
    {"id": "vieneu-quynhanh", "label": "Vie-TTS · Quỳnh Anh (Nữ Bắc Đọc truyện 48k)", "source": "vieneu", "param": "Quỳnh Anh"},
    {"id": "vieneu-thanhbinh", "label": "Vie-TTS · Thanh Bình (Nam Kể chuyện 48k)", "source": "vieneu", "param": "Thanh Bình"},

    # --- MICROSOFT EDGE TTS ---
    {"id": "microsoft-hoaimy", "label": "Microsoft · Hoài My (Nữ)", "source": "edge", "param": "vi-VN-HoaiMyNeural"},
    {"id": "microsoft-namminh", "label": "Microsoft · Nam Minh (Nam)", "source": "edge", "param": "vi-VN-NamMinhNeural"},
]


def catalog():
    cat_file = BASE / "voice_catalog.json"
    if cat_file.is_file():
        try:
            items = json.loads(cat_file.read_text(encoding="utf-8-sig"))
            if isinstance(items, list):
                return [v for v in items if isinstance(v, dict)
                        and v.get("source") in SUPPORTED_TTS_SOURCES]
        except Exception:
            pass
    return [dict(v) for v in DEFAULT_CATALOG]


def selected():
    try:
        value = json.loads((BASE / "voice_selection.json").read_text(encoding="utf-8"))["id"]
    except Exception:
        value = "capcut-BV562_streaming"
    value = ALIASES.get(value, value)
    cat = catalog()
    for voice in cat:
        if voice["id"] == value:
            return voice
    return cat[0] if cat else DEFAULT_CATALOG[0]


def save(voice_id):
    voice_id = ALIASES.get(voice_id, voice_id)
    if not any(v['id']==voice_id for v in catalog()):raise ValueError('Mã giọng không hợp lệ.')
    BASE.mkdir(parents=True, exist_ok=True)
    tmp=BASE/f'voice_selection.json.tmp.{uuid.uuid4().hex}'
    tmp.write_text(json.dumps({'id':voice_id}),encoding='utf-8')
    os.replace(tmp,BASE/'voice_selection.json')
def resolve_voice(default_source, default_param):
    voice = selected()
    validate_tts_source(voice.get("source"))
    return voice["source"], voice["param"], voice["label"]


AUTO_VOICE_CONFIG_FILE = BASE / "v2_auto_voice.json"


def get_auto_voice_config() -> dict:
    """Đọc cấu hình Auto Voice & Dual Voice cho Tool V1. Mặc định dual_voice luôn TẮT (False)."""
    if AUTO_VOICE_CONFIG_FILE.is_file():
        try:
            data = json.loads(AUTO_VOICE_CONFIG_FILE.read_text(encoding="utf-8"))
            f_id = str(data.get("female_voice_id") or "capcut-BV562_streaming")
            f_id = ALIASES.get(f_id, f_id)
            return {
                "enabled": bool(data.get("enabled", True)),
                "dual_voice": bool(data.get("dual_voice", False)),  # LUÔN TẮT MẶC ĐỊNH
                "female_voice_id": f_id,
                "male_voice_id": str(data.get("male_voice_id") or "capcut-BV075_streaming"),
                "updated_at": str(data.get("updated_at", "")),
                "updated_by": str(data.get("updated_by", "default")),
            }
        except Exception:
            pass
    return {
        "enabled": True,
        "dual_voice": False,
        "female_voice_id": "capcut-BV562_streaming",
        "male_voice_id": "capcut-BV075_streaming",
        "updated_at": "",
        "updated_by": "default",
    }


def is_dual_voice_enabled() -> bool:
    """Kiểm tra chế độ dùng cả 2 giọng Nam & Nữ trong cùng video (Mặc định: False)."""
    return bool(get_auto_voice_config().get("dual_voice", False))


def resolve_voice_param(voice_id: str) -> str:
    """Lấy param tương ứng với voice_id cho speaker_voice_map."""
    for v in catalog():
        if v.get("id") == voice_id:
            return v.get("param") or "BV562_streaming"
    return "BV075_streaming" if "male" in voice_id.lower() or "075" in voice_id else "BV562_streaming"


def set_auto_voice_config(
    enabled: bool = None,
    female_voice_id: str = None,
    male_voice_id: str = None,
    dual_voice: bool = None,
    updated_by: str = "system"
) -> dict:
    """Ghi cấu hình Auto Voice & Dual Voice cho Tool V2."""
    from datetime import datetime, timezone
    current = get_auto_voice_config()
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if female_voice_id is not None:
        current["female_voice_id"] = str(female_voice_id)
    if male_voice_id is not None:
        current["male_voice_id"] = str(male_voice_id)
    if dual_voice is not None:
        current["dual_voice"] = bool(dual_voice)
    current.setdefault("dual_voice", False)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    current["updated_by"] = str(updated_by)

    tmp = AUTO_VOICE_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, AUTO_VOICE_CONFIG_FILE)

    # Cập nhật đồng thời speaker_voice_map.json
    f_prm = resolve_voice_param(current["female_voice_id"])
    m_prm = resolve_voice_param(current["male_voice_id"])
    svm = {
        "male": m_prm,
        "female": f_prm,
        "SPEAKER_MALE_0": m_prm,
        "SPEAKER_MALE_1": m_prm,
        "SPEAKER_FEMALE_0": f_prm,
        "SPEAKER_FEMALE_1": f_prm,
    }
    try:
        (BASE / "speaker_voice_map.json").write_text(json.dumps(svm, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    return current


DEFAULT_SPEAKER_VOICE_MAP = {
    "male": "BV075_streaming",
    "female": "BV562_streaming",
    "SPEAKER_MALE_0": "BV075_streaming",
    "SPEAKER_MALE_1": "BV075_streaming",
    "SPEAKER_FEMALE_0": "BV562_streaming",
    "SPEAKER_FEMALE_1": "BV562_streaming",
}

DEFAULT_SPEAKER_MAP = {
    "SPEAKER_MALE_0": "Nhân vật nam chính",
    "SPEAKER_MALE_1": "Nhân vật nam phụ",
    "SPEAKER_FEMALE_0": "Nhân vật nữ chính",
    "SPEAKER_FEMALE_1": "Nhân vật nữ phụ",
}
