"""
A2UI Agent Generator.
Tao ra luong thong diep A2UI JSON dua tren video phoi hoac yeu cau nguoi dung.
"""

from pathlib import Path
from typing import List, Dict, Any, Optional
import os
from .protocol import A2UIMessage, CreateSurface, UpdateComponents, UpdateDataModel
from .catalog import CATALOG_ID


def generate_a2ui_for_video(video_path: str, ai_analysis: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Sinh giao dien A2UI dong cho 1 video phoi cu the.
    """
    file_name = os.path.basename(video_path)
    stem = os.path.splitext(file_name)[0]
    surface_id = f"surface-{abs(hash(stem)) % 100000}"

    size_mb = 0.0
    if os.path.exists(video_path):
        try:
            size_mb = round(os.path.getsize(video_path) / (1024 * 1024), 2)
        except OSError:
            pass

    notes = "Video da san sang de render." if not ai_analysis else ai_analysis.get("notes", "")

    messages = [
        A2UIMessage(CreateSurface(surfaceId=surface_id, catalogId=CATALOG_ID)).to_dict(),
        A2UIMessage(UpdateComponents(
            surfaceId=surface_id,
            components=[
                {
                    "id": "card-main",
                    "component": "Card",
                    "title": f"🎬 Phân Tích Thông Minh: {file_name}",
                    "subtitle": "Giao diện A2UI tùy biến theo ngữ cảnh video",
                    "variant": "highlight",
                    "children": ["inspector", "voice-select", "action-row"]
                },
                {
                    "id": "inspector",
                    "component": "VideoInspectorCard",
                    "videoName": file_name,
                    "fileSize": f"{size_mb} MB",
                    "duration": "Tự động phát hiện",
                    "ocrComplexity": "easy",
                    "aiNotes": notes or "Đã quét phụ đề Trung Quốc và nhận dạng khẩu hình phù hợp."
                },
                {
                    "id": "voice-select",
                    "component": "VoiceSelectorCard",
                    "selectedVoice": { "$bind": "/config/voice" },
                    "options": [
                        {"id": "rvc-chimai", "name": "RVC Chí Mai (Đề xuất)", "type": "rvc", "recommended": True},
                        {"id": "edge-hoaimy", "name": "Hoài My (Edge TTS Nữ)", "type": "edge", "recommended": False},
                        {"id": "edge-namthan", "name": "Nam Thần (Edge TTS Nam)", "type": "edge", "recommended": False}
                    ],
                    "speed": 1.0,
                    "pitch": 0
                },
                {
                    "id": "action-row",
                    "component": "ButtonGroup",
                    "align": "right",
                    "children": ["btn-dismiss", "btn-confirm"]
                },
                {
                    "id": "btn-dismiss",
                    "component": "Button",
                    "label": "Đóng",
                    "variant": "outline",
                    "action": {
                        "callAgentFunction": {
                            "name": "close_surface",
                            "parameters": {"surfaceId": surface_id}
                        }
                    }
                },
                {
                    "id": "btn-confirm",
                    "component": "Button",
                    "label": "Bắt Đầu Render Với Cấu Hình Này 🚀",
                    "variant": "success",
                    "action": {
                        "callAgentFunction": {
                            "name": "confirm_render",
                            "parameters": {
                                "video_name": file_name,
                                "voice": { "$bind": "/config/voice" }
                            }
                        }
                    }
                }
            ]
        )).to_dict(),
        A2UIMessage(UpdateDataModel(
            surfaceId=surface_id,
            path="/config",
            value={"voice": "rvc-chimai"}
        )).to_dict()
    ]

    return messages
