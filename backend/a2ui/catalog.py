"""
A2UI Component Catalog for Media & AI Video Dubbing.
Dinh nghia danh muc cac linh kien UI hop le ma Agent duoc phep sinh ra.
"""

from typing import Dict, Any

CATALOG_ID = "https://a2ui.org/catalogs/autodub-media-v1.json"

MEDIA_COMPONENT_CATALOG: Dict[str, Any] = {
    "catalogId": CATALOG_ID,
    "version": "1.0.0",
    "name": "AutoDub Media & AI Dubbing Catalog",
    "description": "Danh muc component UI an toan cho cong cu bien tap va long tieng video",
    "components": {
        "Card": {
            "description": "Khung chua noi dung co tieu de va vien",
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "variant": {"type": "string", "enum": ["default", "highlight", "warning", "success"]},
                "children": {"type": "array", "items": {"type": "string"}},
            }
        },
        "Text": {
            "description": "Doan van ban hien thi",
            "properties": {
                "text": {"type": ["string", "object"]},
                "variant": {"type": "string", "enum": ["h1", "h2", "h3", "body", "caption", "badge"]},
                "color": {"type": "string"},
            }
        },
        "Button": {
            "description": "Nut bam tuong tac",
            "properties": {
                "label": {"type": "string"},
                "variant": {"type": "string", "enum": ["primary", "secondary", "success", "danger", "outline"]},
                "icon": {"type": "string"},
                "disabled": {"type": ["boolean", "object"]},
                "action": {"type": "object"},
            }
        },
        "ProgressBar": {
            "description": "Thanh tien do hoat anh",
            "properties": {
                "value": {"type": ["number", "object"]},
                "max": {"type": "number", "default": 100},
                "label": {"type": "string"},
                "color": {"type": "string"},
            }
        },
        "VoiceSelectorCard": {
            "description": "The lua chon giong doc long tieng AI",
            "properties": {
                "selectedVoice": {"type": ["string", "object"]},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "recommended": {"type": "boolean"},
                        }
                    }
                },
                "speed": {"type": ["number", "object"]},
                "pitch": {"type": ["number", "object"]},
            }
        },
        "SubtitleReviewCard": {
            "description": "The kiem duyet va chinh sua phu de AI",
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "number"},
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "original": {"type": "string"},
                            "translated": {"type": "string"},
                            "confidence": {"type": "number"},
                        }
                    }
                }
            }
        },
        "AudioMixerCard": {
            "description": "Thanh truot can bang am luong nhac nen va giong doc",
            "properties": {
                "bgmVolumeDb": {"type": ["number", "object"]},
                "dubbingVolumeDb": {"type": ["number", "object"]},
                "duckingEnabled": {"type": ["boolean", "object"]},
            }
        },
        "VideoInspectorCard": {
            "description": "The thong tin video phoi kem nhan xet va de xuat tu AI",
            "properties": {
                "videoName": {"type": "string"},
                "fileSize": {"type": "string"},
                "duration": {"type": "string"},
                "ocrComplexity": {"type": "string", "enum": ["easy", "medium", "hard"]},
                "aiNotes": {"type": "string"},
            }
        },
        "ButtonGroup": {
            "description": "Nhom nut bam ngang",
            "properties": {
                "children": {"type": "array", "items": {"type": "string"}},
                "align": {"type": "string", "enum": ["left", "center", "right"]},
            }
        }
    }
}


def is_component_valid(component_name: str) -> bool:
    """Kiem tra xem component co nam trong Catalog duoc phep khong."""
    return component_name in MEDIA_COMPONENT_CATALOG["components"]
