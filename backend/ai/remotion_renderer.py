# -*- coding: utf-8 -*-
"""
Remotion Renderer (Node sidecar) cho Studio Kịch Bản — đường nâng cấp engine doodle (PoC).
=========================================================================================
Render 1 phân cảnh 3 dải 720x1280 bằng composition `DoodleScenePoC`:
    props (dict) -> JSON tạm -> `node remotion-cli.js render remotion/index.tsx DoodleScenePoC out.mp4 --props=...`

Đặc điểm an toàn:
- Mặc định TẮT ở tầng orchestrator (bật bằng `SCRIPT_REMOTION_ENABLED=1`), engine PIL vẫn là chính.
- `is_available()` kiểm tra: node trên PATH + @remotion/cli đã cài + entry file tồn tại.
- Lỗi render đều ném `RuntimeError` kèm log cuối của CLI; caller tự quyết định lùi về PIL.

Đo lường PoC (RTX 4050, 720x1280@24fps, concurrency mặc định 6x):
- Cảnh 5 giây (120 frames): render + encode ≈ 25–35 giây, file ~300 KB => tương đương tốc độ engine PIL.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Thư mục sidecar Node (C:\tool v1\tools\render_node)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_SIDECAR_DIR = PROJECT_ROOT / "tools" / "render_node"
REMOTION_CLI = NODE_SIDECAR_DIR / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
REMOTION_ENTRY = NODE_SIDECAR_DIR / "remotion" / "index.tsx"
COMPOSITION_ID = "DoodleScenePoC"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_DEFAULT_TIMEOUT = 900.0
DEFAULT_BADGE_TEXT = "★ KHÁM PHÁ BÍ MẬT"


def remotion_enabled() -> bool:
    """Cờ bật/tắt qua biến môi trường (mặc định TẮT để bảo vệ production)."""
    return os.getenv("SCRIPT_REMOTION_ENABLED", "0").strip().lower() in ("1", "true", "on", "yes")


def is_available() -> bool:
    """Kiểm tra kỹ thuật: node + @remotion/cli đã `npm install` + entry composition tồn tại."""
    if shutil.which("node") is None:
        return False
    return REMOTION_CLI.is_file() and REMOTION_ENTRY.is_file()


def build_scene_props(
    title: str,
    keyword_accent: str,
    scene: Dict[str, Any],
    duration_sec: float,
    fps: int = 24,
    accent_color: str = "#000000",
    badge_text: str = DEFAULT_BADGE_TEXT,
) -> Dict[str, Any]:
    """
    Chuyển dữ liệu cảnh nội bộ (schema của prompt_to_script) sang props của DoodleScenePoC.
    Tái sử dụng `detect_scene_action_spec` của kinetic_scene_animator để chọn art + camera + số liệu
    (race / chart_bar / chart_pie / brain_think / showcase). Lỗi phát hiện -> mặc định showcase.
    """
    action_type = "showcase"
    camera_motion = "zoom_in"
    numbers: list = []
    try:
        from ai.kinetic_scene_animator import detect_scene_action_spec

        spec = detect_scene_action_spec(scene) or {}
        action_type = str(spec.get("action_type") or "showcase")
        camera_motion = str(spec.get("camera_motion") or "zoom_in")
        numbers = [int(n) for n in (spec.get("numbers") or []) if isinstance(n, (int, float))][:5]
    except Exception as exc:  # noqa: BLE001 — thiếu module/khác biệt schema thì dùng mặc định
        logger.debug("Không chạy được detect_scene_action_spec, dùng mặc định showcase: %s", exc)

    return {
        "title": (title or "").strip().upper(),
        "keywordAccent": (keyword_accent or "").strip().upper(),
        "accentColor": accent_color,
        "badgeText": badge_text,
        "narration": str(scene.get("narration", "")).strip(),
        "doodleLabel": str(scene.get("doodle_label") or "").strip(),
        "durationSec": float(duration_sec),
        "fps": int(fps),
        "actionType": action_type,
        "cameraMotion": camera_motion,
        "numbers": numbers,
    }


def render_doodle_scene(
    props: Dict[str, Any],
    output_path: Path,
    timeout: float = _DEFAULT_TIMEOUT,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """
    Render 1 phân cảnh bằng Remotion. Ném RuntimeError nếu thất bại (caller tự fallback về PIL).
    `log_callback` nhận 600 ký tự log cuối của CLI (dùng để đẩy tiến độ lên dashboard nếu cần).
    """
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Remotion renderer: không tìm thấy node trên PATH")
    if not REMOTION_CLI.is_file():
        raise RuntimeError(
            f"Remotion renderer: chưa cài @remotion/cli. Chạy `npm install` trong {NODE_SIDECAR_DIR}"
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="remotion_props_") as tmp_dir:
        props_file = Path(tmp_dir) / "props.json"
        props_file.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

        cmd = [
            node,
            str(REMOTION_CLI),
            "render",
            str(REMOTION_ENTRY),
            COMPOSITION_ID,
            str(out),
            f"--props={props_file}",
        ]
        logger.info("Remotion render bắt đầu -> %s", out.name)
        proc = subprocess.run(
            cmd,
            cwd=str(NODE_SIDECAR_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
        tail = proc.stdout.decode("utf-8", "replace")[-600:] if proc.stdout else ""
        if log_callback is not None:
            try:
                log_callback(tail)
            except Exception:  # noqa: BLE001 — callback không được làm hỏng render
                pass

        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"Remotion render thất bại (exit {proc.returncode}) cho {out.name}. Log cuối:\n{tail}"
            )

    logger.info("Remotion render hoàn tất: %s", out)
    return out
