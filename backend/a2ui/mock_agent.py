"""
A2UI Mock Agent - Cac kich ban giao dien mau de thu nghiem ngay lap tuc.
Cho phep thu nghiem toan dien Client Renderer va 2-way RPC ma khong can goi API LLM.
"""

from typing import List, Dict, Any
from .protocol import A2UIMessage, CreateSurface, UpdateComponents, UpdateDataModel
from .catalog import CATALOG_ID


def get_scenario_multivoice() -> List[Dict[str, Any]]:
    """Kich ban 1: AI phat hien video co 2 nhan vat, sinh the lua chon 2 giong doc."""
    surface_id = "multivoice-surface"
    return [
        A2UIMessage(CreateSurface(surfaceId=surface_id, catalogId=CATALOG_ID)).to_dict(),
        A2UIMessage(UpdateComponents(
            surfaceId=surface_id,
            components=[
                {
                    "id": "card-root",
                    "component": "Card",
                    "title": "🎭 AI Phát Hiện 2 Nhân Vật (Nam & Nữ)",
                    "subtitle": "Đề xuất cấu hình lồng tiếng tối ưu theo ngữ cảnh",
                    "variant": "highlight",
                    "children": ["intro-text", "voice-male", "voice-female", "action-btns"]
                },
                {
                    "id": "intro-text",
                    "component": "Text",
                    "text": "Gemini 3.8 Flash nhận diện video này có 2 người đối thoại. Hãy chọn giọng phù hợp cho từng nhân vật:",
                    "variant": "body"
                },
                {
                    "id": "voice-male",
                    "component": "VoiceSelectorCard",
                    "selectedVoice": { "$bind": "/voice/male_id" },
                    "options": [
                        {"id": "rvc-chimai", "name": "Giọng Chí Mai (RVC Trầm Ấm)", "type": "rvc", "recommended": True},
                        {"id": "edge-nam", "name": "Nam Thần (Edge TTS)", "type": "edge", "recommended": False},
                    ],
                    "speed": 1.0,
                    "pitch": 0
                },
                {
                    "id": "voice-female",
                    "component": "VoiceSelectorCard",
                    "selectedVoice": { "$bind": "/voice/female_id" },
                    "options": [
                        {"id": "edge-hoaimy", "name": "Hoài My (Edge TTS Nữ Tự Nhiên)", "type": "edge", "recommended": True},
                        {"id": "rvc-nu-tre", "name": "Nữ Dễ Thương (RVC Clone)", "type": "rvc", "recommended": False},
                    ],
                    "speed": 1.0,
                    "pitch": 0
                },
                {
                    "id": "action-btns",
                    "component": "ButtonGroup",
                    "align": "right",
                    "children": ["btn-cancel", "btn-apply"]
                },
                {
                    "id": "btn-cancel",
                    "component": "Button",
                    "label": "Bỏ qua (Dùng mặc định)",
                    "variant": "outline",
                    "action": {
                        "callAgentFunction": {
                            "name": "dismiss_scenario",
                            "parameters": {"surfaceId": surface_id}
                        }
                    }
                },
                {
                    "id": "btn-apply",
                    "component": "Button",
                    "label": "Áp Dụng Lồng Tiếng Kép 🚀",
                    "variant": "primary",
                    "action": {
                        "callAgentFunction": {
                            "name": "apply_dual_voices",
                            "parameters": {
                                "male_voice": { "$bind": "/voice/male_id" },
                                "female_voice": { "$bind": "/voice/female_id" }
                            }
                        }
                    }
                }
            ]
        )).to_dict(),
        A2UIMessage(UpdateDataModel(
            surfaceId=surface_id,
            path="/voice",
            value={
                "male_id": "rvc-chimai",
                "female_id": "edge-hoaimy"
            }
        )).to_dict()
    ]


def get_scenario_subtitlereview() -> List[Dict[str, Any]]:
    """Kich ban 2: AI de xuat kiem duyet cau dich co do tu tin thap."""
    surface_id = "subreview-surface"
    return [
        A2UIMessage(CreateSurface(surfaceId=surface_id, catalogId=CATALOG_ID)).to_dict(),
        A2UIMessage(UpdateComponents(
            surfaceId=surface_id,
            components=[
                {
                    "id": "card-sub",
                    "component": "Card",
                    "title": "📝 Kiểm Duyệt Phụ Đề Nhanh (1-Chạm)",
                    "subtitle": "AI phát hiện 2 câu có thành ngữ tiếng Trung cần bạn duyệt",
                    "variant": "warning",
                    "children": ["sub-table", "btn-confirm-subs"]
                },
                {
                    "id": "sub-table",
                    "component": "SubtitleReviewCard",
                    "segments": [
                        {
                            "index": 1,
                            "start": "00:00:01",
                            "end": "00:00:03",
                            "original": "不满意自己身材的bjd猛男不要焦虑",
                            "translated": "Các anh em vạm vỡ đừng quá lo lắng về vóc dáng",
                            "confidence": 0.98
                        },
                        {
                            "index": 2,
                            "start": "00:00:04",
                            "end": "00:00:07",
                            "original": "咱们今天速捏一个超级好看的粘土雕塑",
                            "translated": "Hôm nay chúng ta nặn nhanh một bức tượng đất sét cực đẹp",
                            "confidence": 0.85
                        }
                    ]
                },
                {
                    "id": "btn-confirm-subs",
                    "component": "Button",
                    "label": "Đồng Ý Bản Dịch Này & Render Ngay ✅",
                    "variant": "success",
                    "action": {
                        "callAgentFunction": {
                            "name": "approve_subtitles",
                            "parameters": {"status": "approved"}
                        }
                    }
                }
            ]
        )).to_dict()
    ]


def get_scenario_videorater() -> List[Dict[str, Any]]:
    """Kich ban 3: Danh gia do kho cua video phoi va dieu chinh am luong."""
    surface_id = "rater-surface"
    return [
        A2UIMessage(CreateSurface(surfaceId=surface_id, catalogId=CATALOG_ID)).to_dict(),
        A2UIMessage(UpdateComponents(
            surfaceId=surface_id,
            components=[
                {
                    "id": "card-inspect",
                    "component": "Card",
                    "title": "🔍 Đánh Giá Video Phôi & Tùy Chỉnh Mixer",
                    "subtitle": "Phân tích tự động trước khi nạp vào quy trình render",
                    "variant": "default",
                    "children": ["inspector", "mixer", "btn-start-tuned"]
                },
                {
                    "id": "inspector",
                    "component": "VideoInspectorCard",
                    "videoName": "1788783054_050209dd_粘土教程.mp4",
                    "fileSize": "22.4 MB",
                    "duration": "00:48",
                    "ocrComplexity": "easy",
                    "aiNotes": "Video sạch, logo mờ ở góc dưới, âm thanh gốc rõ ràng, không bị tạp âm."
                },
                {
                    "id": "mixer",
                    "component": "AudioMixerCard",
                    "bgmVolumeDb": { "$bind": "/audio/bgm" },
                    "dubbingVolumeDb": { "$bind": "/audio/dub" },
                    "duckingEnabled": True
                },
                {
                    "id": "btn-start-tuned",
                    "component": "Button",
                    "label": "Bắt Đầu Render Với Cấu Hình Này ⚡",
                    "variant": "primary",
                    "action": {
                        "callAgentFunction": {
                            "name": "render_with_custom_mix",
                            "parameters": {
                                "bgm": { "$bind": "/audio/bgm" },
                                "dub": { "$bind": "/audio/dub" }
                            }
                        }
                    }
                }
            ]
        )).to_dict(),
        A2UIMessage(UpdateDataModel(
            surfaceId=surface_id,
            path="/audio",
            value={"bgm": -2, "dub": 2}
        )).to_dict()
    ]


def get_preset_scenario(name: str) -> List[Dict[str, Any]]:
    """Lay kich ban A2UI theo ten."""
    if name == "multivoice":
        return get_scenario_multivoice()
    elif name == "subtitles":
        return get_scenario_subtitlereview()
    else:
        return get_scenario_videorater()
