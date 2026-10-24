# -*- coding: utf-8 -*-
"""
AI Engine: Biến Prompt ý tưởng hoặc Kịch bản có sẵn thành danh sách phân cảnh (Scenes)
cho video Doodle / Whiteboard 3-Band (9:16).
Sử dụng Google Gemini với công thức Storytelling 9-Move Arc cuốn hút.
Mỗi phân cảnh tự động tạo visual_type, icon_keywords (tìm kiếm vector icon) và layout phù hợp 100% với ý tưởng.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from ai.scriptwriting import call_gemini, GEMINI_SCRIPT_MODEL
from ai.muse_spark_service import is_muse_available, call_muse_spark

logger = logging.getLogger(__name__)


class DoodleScene(BaseModel):
    scene_idx: int = Field(..., description="Thứ tự phân cảnh (1, 2, 3...)")
    narration: str = Field(..., description="Lời thoại ngắn gọn cần đọc cho cảnh này (12-18 từ, tiếng Việt tự nhiên)")
    doodle_label: str = Field(..., description="Nhãn chữ in hoa ngắn gọn (2-4 từ) xuất hiện trên hình, ví dụ: 40 FEET LONG, ZERO ATTACKS")
    visual_concept: str = Field(..., description="Ý tưởng hình vẽ minh họa trực quan cho cảnh")
    image_prompt: Optional[str] = Field(
        default="",
        description="Mô tả tiếng Anh chi tiết để AI vẽ tranh phác thảo đúng 100% nội dung cảnh, VD: 'two cute cartoon cats wearing racing helmets driving fast sports race cars competing on a racetrack with checkered flag and speed lines'"
    )
    visual_type: str = Field(
        default="generic",
        description="Loại đối tượng hình vẽ chính: cat, dog, fight, whale, shark, ocean, brain, sonar, zero_record, chart, clock, space, money, heart, microscope, danger, people_talk, car, doctor, book, generic"
    )
    icon_keywords: List[str] = Field(
        default_factory=list,
        description="1-3 từ khóa tiếng Anh tìm kiếm vector icon minh họa trên Iconify, VD: ['cat', 'cat', 'flash'], ['cat', 'boxing'], ['whale', 'water']"
    )
    layout: str = Field(
        default="dual_confrontation",
        description="Bố cục hiển thị: dual_confrontation (2 đối tượng 2 bên đối đầu), single_centered (1 đối tượng lớn ở giữa), action_duo (đối tượng + hành động)"
    )
    action_type: Optional[str] = Field(
        default="showcase",
        description="Kiểu chuyển động hoạt họa chính: race (đua xe/tốc độ), chart_pie (biểu đồ tròn 50-30-20), chart_bar (biểu đồ tăng trưởng), wallet_squeeze (vắt ví/rỗng túi), brain_think (não bộ bánh răng), fight (hỗn chiến/va chạm), confrontation (đối đầu), showcase (trung tâm)"
    )
    camera_motion: Optional[str] = Field(
        default="zoom_in",
        description="Chuyển động máy quay điện ảnh: zoom_in, pan_right, shake, pulse"
    )
    mood_color: str = Field(default="#FFFFFF", description="Màu nền dải giữa: #FFFFFF")


class DoodleScriptResponse(BaseModel):
    title: str = Field(..., description="Tiêu đề video giật gân, cuốn hút đặt ở Banner trên cùng (viết hoa)")
    keyword_accent: str = Field(..., description="Từ khóa nguy hiểm / nhấn mạnh trong tiêu đề (sẽ tô màu đỏ)")
    scenes: List[DoodleScene] = Field(..., description="Danh sách các phân cảnh")


SYSTEM_INSTRUCTION_PROMPT_TO_SCRIPT = """Bạn là chuyên gia biên kịch hàng đầu cho các video ngắn triệu view (TikTok, YouTube Shorts, Reels) theo phong cách Whiteboard / Doodle Explainer cuốn hút.

Nhiệm vụ của bạn:
1. Nhận ý tưởng (Prompt) hoặc kịch bản thô từ người dùng.
2. Viết một kịch bản hoàn chỉnh theo cấu trúc tâm lý học 9 bước cuốn hút (9-Move Arc):
   - Mở đầu bằng ngôi thứ hai trực tiếp ("Hãy tưởng tượng...")
   - Dồn dập số liệu thực tế gây bất ngờ
   - Nêu nghịch lý logic khó tin
   - Bác bỏ suy đoán thông thường và hứa hẹn tiết lộ sự thật ở cuối
   - Đập tan hiểu lầm và đưa bằng chứng khoa học cụ thể
   - Giải thích bản chất cốt lõi
   - Kết bài đắt giá, đánh trúng tâm lý người xem
3. Chia kịch bản thành các phân cảnh ngắn (Scenes):
   - Mỗi phân cảnh CHỈ chứa từ 12 đến 18 từ (thời lượng đọc khoảng 3 - 5 giây). Tuyệt đối không viết câu quá dài.
   - Lời thoại (narration) bằng Tiếng Việt chuẩn, nhịp điệu nhanh, sắc bén, không rườm rà.
   - 'doodle_label': Nhãn chữ in hoa ngắn (2-4 từ) để vẽ lên tranh (VD: 'ĐỐI ĐẦU', 'HỖN CHIẾN', '100 METERS', 'BÍ MẬT NÃO BỘ').
   - 'visual_concept': Miêu tả ngắn gọn hình ảnh trực quan tương ứng chính xác với nội dung câu thoại (VD: 'Hai chú mèo lái xe đua F1 lao vút trên đường đua').
   - 'image_prompt': Câu lệnh tiếng Anh chi tiết để AI vẽ tranh phác thảo Whiteboard Line-art mô tả ĐÚNG 100% cảnh này (VD: 'two cute cats wearing racing helmets driving fast formula race cars on a racetrack with speed lines and checkered flag, clean black line art'). TUYỆT ĐỐI không dùng ảnh tượng trưng chung chung mà phải vẽ đúng hành động và đối tượng cụ thể mà người dùng yêu cầu!
   - 'visual_type': Loại đối tượng cụ thể (cat, dog, fight, whale, shark, ocean, brain, sonar, zero_record, chart, clock, space, money, heart, microscope, danger, people_talk, car, doctor, book, generic).
   - 'icon_keywords': 1 đến 3 từ khóa tiếng Anh tìm kiếm vector icon minh họa chính xác cho cảnh.
   - 'layout': Chọn 1 trong 3 bố cục:
     * 'dual_confrontation': 2 đối tượng 2 bên đối đầu.
     * 'single_centered': 1 biểu tượng lớn ở giữa.
     * 'action_duo': 1 chủ thể bên trái + 1 hành động/vật thể bên phải.
   - 'mood_color': Chọn '#FFFFFF' (trắng trung tính).

4. 'title': Tiêu đề dạng câu hỏi gây tò mò, 4-7 từ, viết hoa toàn bộ (VD: 'TẠI SAO HAI CHÚ MÈO LẠI ĐUA XE?').
5. 'keyword_accent': 1 từ duy nhất trong tiêu đề đại diện cho mối nguy hiểm/tò mò (sẽ tô màu đỏ, VD: 'ĐUA XE').

QUAN TRỌNG VỀ HÌNH ẢNH:
Mỗi phân cảnh PHẢI CÓ HÌNH MINH HỌA TRỰC QUAN KHÁC NHAU theo đúng nội dung câu thoại, TUYỆT ĐỐI KHÔNG dùng hình trừu tượng hay người que chung chung.

BẮT BUỘC trả về dữ liệu chuẩn JSON tuân thủ schema:
{
  "title": "TIÊU ĐỀ VIDEO",
  "keyword_accent": "TỪ KHÓA ĐỎ",
  "scenes": [
    {
      "scene_idx": 1,
      "narration": "Hãy tưởng tượng bạn đang chứng kiến một cuộc đua nghẹt thở giữa hai chú mèo siêu tốc.",
      "doodle_label": "ĐUA XE NGHẸT THỞ",
      "visual_concept": "Hai chú mèo đội mũ bảo hiểm đang lái hai chiếc xe đua F1 lao vút trên đường đua",
      "image_prompt": "two cute cats wearing racing helmets driving fast formula race cars on a racetrack with speed lines and checkered flag, minimalist black ink line art on pure white background",
      "visual_type": "car",
      "icon_keywords": ["cat", "car"],
      "layout": "dual_confrontation",
      "mood_color": "#FFFFFF"
    }
  ]
}
"""


def infer_visual_metadata(narration: str, visual_concept: str, visual_type: str = "") -> Dict[str, Any]:
    """Tự động suy luận visual_type, icon_keywords và layout nếu AI trả về thiếu."""
    combined = f"{narration} {visual_concept} {visual_type}".lower()

    # Mèo / Động vật
    if any(w in combined for w in ["mèo", "cat", "miêu", "móng vuốt"]):
        if any(w in combined for w in ["đánh", "cào", "tát", "chiến", "hỗn chiến", "fight", "bất ngờ"]):
            return {"visual_type": "fight", "icon_keywords": ["cat", "boxing"], "layout": "action_duo"}
        return {"visual_type": "cat", "icon_keywords": ["cat", "cat", "flash"], "layout": "dual_confrontation"}

    if any(w in combined for w in ["chó", "dog", "cẩu"]):
        return {"visual_type": "dog", "icon_keywords": ["dog", "bone"], "layout": "action_duo"}

    if any(w in combined for w in ["cá voi", "orca", "whale"]):
        return {"visual_type": "whale", "icon_keywords": ["whale", "water"], "layout": "single_centered"}

    if any(w in combined for w in ["cá mập", "shark"]):
        return {"visual_type": "shark", "icon_keywords": ["shark", "person"], "layout": "dual_confrontation"}

    if any(w in combined for w in ["não", "trí tuệ", "tâm lý", "nghĩ", "brain", "ý thức"]):
        return {"visual_type": "brain", "icon_keywords": ["brain", "idea"], "layout": "single_centered"}

    if any(w in combined for w in ["sóng âm", "siêu âm", "sonar", "định vị", "radar"]):
        return {"visual_type": "sonar", "icon_keywords": ["radar", "sound"], "layout": "single_centered"}

    if any(w in combined for w in ["tiền", "tài sản", "triệu phú", "đô la", "money", "giàu"]):
        return {"visual_type": "money", "icon_keywords": ["money", "chart"], "layout": "single_centered"}

    if any(w in combined for w in ["thời gian", "đồng hồ", "năm", "phút", "clock", "giây"]):
        return {"visual_type": "clock", "icon_keywords": ["clock", "hourglass"], "layout": "single_centered"}

    if any(w in combined for w in ["vũ trụ", "hố đen", "sao", "space", "thiên hà"]):
        return {"visual_type": "space", "icon_keywords": ["space", "planet"], "layout": "single_centered"}

    if any(w in combined for w in ["xe", "ô tô", "tốc độ", "car"]):
        return {"visual_type": "car", "icon_keywords": ["car", "speed"], "layout": "single_centered"}

    if any(w in combined for w in ["bác sĩ", "y tế", "thuốc", "bệnh", "virus", "sức khỏe"]):
        return {"visual_type": "doctor", "icon_keywords": ["doctor", "virus"], "layout": "action_duo"}

    if any(w in combined for w in ["sách", "học", "trường", "đọc", "book"]):
        return {"visual_type": "book", "icon_keywords": ["book", "pencil"], "layout": "single_centered"}

    if any(w in combined for w in ["nguy hiểm", "chết", "tử thần", "cảnh báo", "danger"]):
        return {"visual_type": "danger", "icon_keywords": ["danger", "skull"], "layout": "single_centered"}

    if any(w in combined for w in ["đối thoại", "nói chuyện", "tranh luận", "hỏi", "talk"]):
        return {"visual_type": "people_talk", "icon_keywords": ["person", "chat"], "layout": "dual_confrontation"}

    return {"visual_type": "generic", "icon_keywords": ["lightbulb", "star"], "layout": "single_centered"}


def generate_doodle_script_from_ai(
    prompt: Optional[str] = None,
    raw_script: Optional[str] = None,
    target_scene_count: int = 8,
    ai_engine: str = "gemini"
) -> Dict[str, Any]:
    """
    Sinh kịch bản phân cảnh thông minh từ Prompt hoặc Kịch bản có sẵn.
    Hỗ trợ cả Meta Muse Spark AI và Google Gemini.
    """
    if not prompt and not raw_script:
        raise ValueError("Vui lòng nhập ít nhất một Ý tưởng (Prompt) hoặc Kịch bản có sẵn!")

    if raw_script and raw_script.strip():
        user_input_content = f"""Người dùng đã có sẵn kịch bản dưới đây. Hãy giữ nguyên ý tứ và lời thoại, chia thành các phân cảnh chuẩn 12-18 từ mỗi cảnh, bổ sung tiêu đề banner, doodle label, visual_concept, visual_type, icon_keywords và layout tương ứng:

KỊCH BẢN CỦA NGƯỜI DÙNG:
\"\"\"
{raw_script.strip()}
\"\"\"
"""
    else:
        user_input_content = f"""Hãy viết kịch bản hoàn chỉnh khoảng {target_scene_count} phân cảnh dựa trên ý tưởng/chủ đề sau:

CHỦ ĐỀ:
\"\"\"
{prompt.strip()}
\"\"\"
"""

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "keyword_accent": {"type": "STRING"},
            "scenes": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "scene_idx": {"type": "INTEGER"},
                        "narration": {"type": "STRING"},
                        "doodle_label": {"type": "STRING"},
                        "visual_concept": {"type": "STRING"},
                        "visual_type": {"type": "STRING"},
                        "icon_keywords": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        },
                        "layout": {"type": "STRING"},
                        "mood_color": {"type": "STRING"}
                    },
                    "required": ["scene_idx", "narration", "doodle_label", "visual_concept", "visual_type", "icon_keywords", "layout", "mood_color"]
                }
            }
        },
        "required": ["title", "keyword_accent", "scenes"]
    }

    raw_json_str = None
    engine_name = "gemini"

    # 1. Ưu tiên gọi Meta Muse Spark nếu được chọn hoặc ở chế độ tự động
    if ai_engine in ("muse", "auto", None, "") and is_muse_available():
        for attempt in range(2):
            try:
                logger.info(f"Đang kết nối Meta Muse Spark AI (muse-spark-1.3-contributor) để biên soạn kịch bản [lần {attempt+1}]...")
                raw_json_str = call_muse_spark(
                    prompt=user_input_content,
                    system_instruction=SYSTEM_INSTRUCTION_PROMPT_TO_SCRIPT,
                )
                if raw_json_str and raw_json_str.strip():
                    engine_name = "muse-spark-1.3"
                    break
            except Exception as e:
                logger.warning(f"Lỗi gọi Meta Muse Spark (lần {attempt+1}): {e}")
                if attempt == 0:
                    time.sleep(1.0)

    # 2. Gọi Google Gemini nếu Muse không có hoặc được chỉ định dùng Gemini
    if not raw_json_str:
        logger.info("Đang gọi Google Gemini AI sinh kịch bản phân cảnh Doodle...")
        raw_json_str = call_gemini(
            prompt=user_input_content,
            system_instruction=SYSTEM_INSTRUCTION_PROMPT_TO_SCRIPT,
            response_schema=response_schema,
            structured_output=True,
            preferred_model=GEMINI_SCRIPT_MODEL
        )
        engine_name = "gemini"

    if not raw_json_str:
        raise RuntimeError("Không nhận được phản hồi từ AI.")

    # Trích xuất JSON siêu bền bỉ (hỗ trợ cả Markdown codeblock, văn bản kèm theo, và JSON thô)
    clean_json = raw_json_str.strip()
    data = None

    # Thử 1: json.loads trực tiếp
    try:
        data = json.loads(clean_json)
    except Exception:
        pass

    # Thử 2: Bóc tách từ khối ```json ... ``` hoặc ``` ... ```
    if not data and "```" in clean_json:
        code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", clean_json)
        for block in code_blocks:
            try:
                data = json.loads(block.strip())
                if data and isinstance(data, dict):
                    break
            except Exception:
                continue

    # Thử 3: Tìm khối ngoặc nhọn { ... } đầu tiên đến cuối cùng
    if not data or not isinstance(data, dict):
        match_brace = re.search(r"(\{[\s\S]*\})", clean_json)
        if match_brace:
            try:
                data = json.loads(match_brace.group(1).strip())
            except Exception:
                pass

    if not data or not isinstance(data, dict):
        logger.error(f"Lỗi parse JSON kịch bản: {clean_json[:500]}")
        raise RuntimeError(f"Lỗi phân tích cú pháp kịch bản AI: Không thể nhận diện cấu trúc JSON.")

    # Đảm bảo có ít nhất 1 cảnh
    scenes = data.get("scenes", [])
    if not scenes:
        raise RuntimeError("AI không tạo được phân cảnh nào.")

    # Đánh số thứ tự lại và đảm bảo visual_type, icon_keywords, layout hợp lệ
    for i, sc in enumerate(scenes):
        sc["scene_idx"] = i + 1
        if "mood_color" not in sc or not sc["mood_color"]:
            sc["mood_color"] = "#FFFFFF"

        # Tự động bù khuyết metadata hình ảnh nếu AI trả về thiếu
        curr_icons = sc.get("icon_keywords")
        curr_layout = sc.get("layout")
        curr_vtype = sc.get("visual_type")
        if not curr_icons or not curr_layout or not curr_vtype or curr_vtype == "generic":
            inferred = infer_visual_metadata(
                sc.get("narration", ""),
                sc.get("visual_concept", ""),
                curr_vtype or ""
            )
            if not curr_icons:
                sc["icon_keywords"] = inferred["icon_keywords"]
            if not curr_layout:
                sc["layout"] = inferred["layout"]
            if not curr_vtype or curr_vtype == "generic":
                sc["visual_type"] = inferred["visual_type"]

        # Đảm bảo có image_prompt chi tiết cho bộ vẽ AI Whiteboard Line Art
        if not sc.get("image_prompt"):
            v_concept = sc.get("visual_concept") or sc.get("narration") or "whiteboard line art"
            sc["image_prompt"] = f"{v_concept}, clean black line art sketch on pure white background"

        # Tự động gán action_type & camera_motion từ Kinetic Scene Animator
        try:
            from ai.kinetic_scene_animator import detect_scene_action_spec
            spec = detect_scene_action_spec(sc)
            sc["action_type"] = sc.get("action_type") or spec.get("action_type", "showcase")
            sc["camera_motion"] = sc.get("camera_motion") or spec.get("camera_motion", "zoom_in")
        except Exception:
            sc.setdefault("action_type", "showcase")
            sc.setdefault("camera_motion", "zoom_in")

    data["ai_engine_used"] = engine_name
    return data
