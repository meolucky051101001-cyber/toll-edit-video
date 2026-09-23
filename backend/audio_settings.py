import json
import os
import logging
from pathlib import Path

logger = logging.getLogger("audio_settings")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
def _resolve_settings_file() -> Path:
    bot_system = WORKSPACE / "bot_system"
    target = bot_system / "audio_settings.json"
    if target.exists() or bot_system.is_dir():
        return target
    return WORKSPACE / "audio_settings.json"

SETTINGS_FILE = _resolve_settings_file()

DEFAULT_SETTINGS = {
    "bgm_volume_db": -2.0,
    "dubbing_volume_db": 1.0,
}

def get_audio_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            bgm = float(data.get("bgm_volume_db", -2.0))
            dub = float(data.get("dubbing_volume_db", 1.0))
            # Clamp to safe ranges
            bgm = max(-40.0, min(10.0, bgm))
            dub = max(-20.0, min(15.0, dub))
            return {
                "bgm_volume_db": round(bgm, 1),
                "dubbing_volume_db": round(dub, 1),
            }
    except Exception as e:
        logger.warning(f"Error reading audio settings: {e}")
    return dict(DEFAULT_SETTINGS)

def save_audio_settings(bgm_volume_db: float, dubbing_volume_db: float) -> dict:
    try:
        bgm = max(-40.0, min(10.0, float(bgm_volume_db)))
        dub = max(-20.0, min(15.0, float(dubbing_volume_db)))
        payload = {
            "bgm_volume_db": round(bgm, 1),
            "dubbing_volume_db": round(dub, 1),
        }
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(f"Saved audio settings: BGM={bgm}dB, Dubbing={dub}dB")
        return {"status": "ok", **payload, "message": "Đã lưu cài đặt âm lượng thành công"}
    except Exception as e:
        logger.error(f"Error saving audio settings: {e}")
        return {"status": "error", "message": str(e)}
