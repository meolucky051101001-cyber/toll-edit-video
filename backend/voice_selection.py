"""Shared dashboard/bot voice choice, read at the start of voice generation."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = Path(os.getenv("TOOL_V2_CONTROL_DIR", str(ROOT.parent / "workspace" / "control")))
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
    if not any(v["id"] == voice_id for v in catalog()):
        raise ValueError("Mã giọng không hợp lệ.")
    tmp = BASE / "voice_selection.tmp"
    tmp.write_text(json.dumps({"id": voice_id}), encoding="utf-8")
    os.replace(tmp, BASE / "voice_selection.json")


def resolve_voice(default_source, default_param):
    voice = selected()
    if voice["id"] == "chi-mai":
        if default_source != "rvc" or not default_param:
            return "edge", "vi-VN-HoaiMyNeural", "Microsoft · Hoài My (nữ)"
        return default_source, str(default_param), voice["label"]
    return voice["source"], voice["param"], voice["label"]
