"""Shared dashboard/bot voice choice, read at the start of voice generation."""
import json
import os
import uuid
from pathlib import Path
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
BASE.mkdir(parents=True, exist_ok=True)
ALIASES = {'capcut-vi-VN-HoaiMyNeural': 'microsoft-hoaimy', 'capcut-vi-VN-NamMinhNeural': 'microsoft-namminh'}

DEFAULT_CATALOG = [
    {"id": "chi-mai", "label": "Chí Mai · RVC (mặc định)", "source": "rvc", "param": ""},
    {"id": "microsoft-hoaimy", "label": "Microsoft · Hoài My (nữ)", "source": "edge", "param": "vi-VN-HoaiMyNeural"},
    {"id": "microsoft-namminh", "label": "Microsoft · Nam Minh (nam)", "source": "edge", "param": "vi-VN-NamMinhNeural"},
    {"id": "capcut-BV421_vivn_streaming", "label": "CapCut · Nhỏ Ngọt Ngào", "source": "capcut", "param": "BV421_vivn_streaming"},
    {"id": "capcut-vi_female_huong", "label": "CapCut · Giọng Nữ Phổ Thông", "source": "capcut", "param": "vi_female_huong"},
    {"id": "capcut-BV074_streaming_dsp", "label": "CapCut · Giọng Bé", "source": "capcut", "param": "BV074_streaming_dsp"},
    {"id": "capcut-BV074_streaming", "label": "CapCut · Cô Gái Hoạt Ngôn", "source": "capcut", "param": "BV074_streaming"},
    {"id": "capcut-BV075_streaming_vibrato_dsp", "label": "CapCut · Việt Méo", "source": "capcut", "param": "BV075_streaming_vibrato_dsp"},
    {"id": "capcut-BV562_streaming", "label": "CapCut · Mai", "source": "capcut", "param": "BV562_streaming"},
    {"id": "capcut-BV075_streaming", "label": "CapCut · Thanh Niên Tự Tin", "source": "capcut", "param": "BV075_streaming"},
    {"id": "capcut-multi_male_felipe_uranus_bigtts", "label": "CapCut · Giọng Nam Trầm", "source": "capcut", "param": "multi_male_felipe_uranus_bigtts"},
    {"id": "capcut-multi_female_banmai", "label": "CapCut · Ban Mai", "source": "capcut", "param": "multi_female_yangguangnv_uranus_bigtts"}
]


def catalog():
    cat_file = BASE / "voice_catalog.json"
    if cat_file.is_file():
        try:
            return json.loads(cat_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        cat_file.write_text(json.dumps(DEFAULT_CATALOG, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return list(DEFAULT_CATALOG)


def selected():
    try:
        value = json.loads((BASE / "voice_selection.json").read_text(encoding="utf-8"))["id"]
    except Exception:
        value = "chi-mai"
    value = ALIASES.get(value, value)
    for voice in catalog():
        if voice["id"] == value:
            return voice
    cat = catalog()
    return cat[0] if cat else {"id": "chi-mai", "label": "Chí Mai · RVC (mặc định)", "source": "rvc", "param": ""}


def save(voice_id):
    voice_id = ALIASES.get(voice_id, voice_id)
    if not any(v['id']==voice_id for v in catalog()):raise ValueError('Mã giọng không hợp lệ.')
    tmp=BASE/f'voice_selection.json.tmp.{uuid.uuid4().hex}'
    tmp.write_text(json.dumps({'id':voice_id}),encoding='utf-8')
    os.replace(tmp,BASE/'voice_selection.json')
def resolve_voice(default_source, default_param):
    voice = selected()
    if voice["id"] == "chi-mai":
        param = default_param
        if not param or default_source != "rvc":
            try:
                from pipeline_v2.content import discover_rvc_model
                param = discover_rvc_model(BASE)
            except Exception:
                param = None
        if not param:
            return "edge", "vi-VN-HoaiMyNeural", "Microsoft · Hoài My (nữ)"
        return "rvc", str(param), voice["label"]
    return voice["source"], voice["param"], voice["label"]


AUTO_VOICE_CONFIG_FILE = BASE / "v2_auto_voice.json"


def get_auto_voice_config() -> dict:
    """Đọc cấu hình Auto Voice & Dual Voice cho Tool V2. Mặc định dual_voice luôn TẮT (False)."""
    if AUTO_VOICE_CONFIG_FILE.is_file():
        try:
            data = json.loads(AUTO_VOICE_CONFIG_FILE.read_text(encoding="utf-8"))
            return {
                "enabled": bool(data.get("enabled", True)),
                "dual_voice": bool(data.get("dual_voice", False)),  # LUÔN TẮT MẶC ĐỊNH
                "female_voice_id": str(data.get("female_voice_id") or "chi-mai"),
                "male_voice_id": str(data.get("male_voice_id") or "capcut-BV075_streaming"),
                "updated_at": str(data.get("updated_at", "")),
                "updated_by": str(data.get("updated_by", "default")),
            }
        except Exception:
            pass
    return {
        "enabled": True,
        "dual_voice": False,
        "female_voice_id": "chi-mai",
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
            if v.get("source") == "rvc" or v.get("id") == "chi-mai":
                return "BV562_streaming"
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


def get_speaker_voice_map() -> dict:
    svm_file = BASE / "speaker_voice_map.json"
    if svm_file.is_file():
        try:
            return json.loads(svm_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg = get_auto_voice_config()
    f_prm = resolve_voice_param(cfg.get("female_voice_id", "chi-mai"))
    m_prm = resolve_voice_param(cfg.get("male_voice_id", "capcut-BV075_streaming"))
    return {
        "male": m_prm,
        "female": f_prm,
        "SPEAKER_MALE_0": m_prm,
        "SPEAKER_MALE_1": m_prm,
        "SPEAKER_FEMALE_0": f_prm,
        "SPEAKER_FEMALE_1": f_prm,
    }


def get_speaker_map() -> dict:
    sm_file = BASE / "speaker_map.json"
    if sm_file.is_file():
        try:
            return json.loads(sm_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(DEFAULT_SPEAKER_MAP)

