import json
import os
import logging
from pathlib import Path

try:
    from backend.config.paths import AppPaths
except ImportError:
    from config.paths import AppPaths

logger = logging.getLogger("audio_settings")

ROOT = Path(__file__).resolve().parent
WORKSPACE = AppPaths.from_environment(ROOT.parent).workspace
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
    "separation_mode": "roformer",
    "ducking_mode": "soft",
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
            sep_mode = str(data.get("separation_mode", "roformer")).strip().lower()
            if sep_mode not in ("roformer", "demucs", "bypass"):
                sep_mode = "roformer"
            duck_mode = str(data.get("ducking_mode", "soft")).strip().lower()
            if duck_mode not in ("soft", "medium", "off"):
                duck_mode = "soft"
            return {
                "bgm_volume_db": round(bgm, 1),
                "dubbing_volume_db": round(dub, 1),
                "separation_mode": sep_mode,
                "ducking_mode": duck_mode,
            }
    except Exception as e:
        logger.warning(f"Error reading audio settings: {e}")
    return dict(DEFAULT_SETTINGS)

def save_audio_settings(
    bgm_volume_db: float,
    dubbing_volume_db: float,
    separation_mode: str | None = None,
    ducking_mode: str | None = None,
) -> dict:
    try:
        bgm = max(-40.0, min(10.0, float(bgm_volume_db)))
        dub = max(-20.0, min(15.0, float(dubbing_volume_db)))
        current = get_audio_settings()
        sep = separation_mode if separation_mode in ("roformer", "demucs", "bypass") else current.get("separation_mode", "roformer")
        duck = ducking_mode if ducking_mode in ("soft", "medium", "off") else current.get("ducking_mode", "soft")
        payload = {
            "bgm_volume_db": round(bgm, 1),
            "dubbing_volume_db": round(dub, 1),
            "separation_mode": sep,
            "ducking_mode": duck,
        }
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(f"Saved audio settings: BGM={bgm}dB, Dubbing={dub}dB, Separation={sep}, Ducking={duck}")
        return {"status": "ok", **payload, "message": "Đã lưu cài đặt âm lượng thành công"}
    except Exception as e:
        logger.error(f"Error saving audio settings: {e}")
        return {"status": "error", "message": str(e)}
