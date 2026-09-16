# -*- coding: utf-8 -*-
"""AI Scriptwriting engine based on social-media-skills patterns (reels-scripting & hook-generator)."""

from __future__ import annotations

import json
import logging
import os
import re
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests
import cv2

logger = logging.getLogger(__name__)

# .env được nạp bởi main.py khi khởi động server.
# Nếu chạy module standalone, cần đặt GEMINI_API_KEY trong môi trường.


def get_gemini_api_key(api_key: Optional[str] = None) -> str:
    return (api_key or os.getenv("GEMINI_API_KEY", "")).strip()


def call_gemini(
    prompt: Optional[str] = None,
    parts: Optional[List[Dict[str, Any]]] = None,
    api_key: Optional[str] = None,
    system_instruction: str = ""
) -> Optional[str]:
    """Gọi Google Gemini API (hỗ trợ cả văn bản thuần túy và Multimodal Vision)."""
    key = get_gemini_api_key(api_key)
    if not key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trong hệ thống!")

    preferred_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
    candidate_models = [
        preferred_model,
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-flash-latest",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
    ]
    models_to_try = []
    for m in candidate_models:
        if m and m not in models_to_try:
            models_to_try.append(m)

    content_parts = parts if parts is not None else [{"text": prompt or ""}]
    payload: Dict[str, Any] = {
        "contents": [{"parts": content_parts}],
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.95,
            "responseMimeType": "application/json",
        }
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": key,
    }
    last_error = None

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            logger.info(f"Đang gọi Gemini ({model}) cho kịch bản...")
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    return text
                except (KeyError, IndexError) as e:
                    last_error = f"Lỗi cấu trúc phản hồi từ {model}: {data}"
                    logger.warning(last_error)
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text}"
                logger.warning(f"Lỗi gọi Gemini {model}: {last_error}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Ngoại lệ kết nối {model}: {e}")

    raise RuntimeError(f"Không thể kết nối tới Google Gemini sau các model thử nghiệm. Lỗi: {last_error}")


HUMAN_VOICE_GUIDELINES = """
=== QUY TẮC BẮT BUỘC: LỜI VĂN 100% GẦN GŨI, TỰ NHIÊN NHƯ NGƯỜI THẬT (CHỐNG MÁY MÓC / LOẠI BỎ MÙI AI) ===

1. CÁCH NÓI CHUYỆN NGOÀI ĐỜI (CONVERSATIONAL & RELATABLE):
   - Đóng vai một người bạn thân ngoài đời đang trò chuyện hoặc một nhà sáng tạo nội dung TikTok/Reels dí dỏm, chân thành.
   - Xưng hô thân mật: Xưng "mình", gọi "bạn", "mọi người", "anh em", "mấy bà", "bác nào hay...".
   - Dùng các khẩu ngữ, trợ từ cảm thán tự nhiên của người Việt: "Trời ơi", "Ủa", "Nói thật là...", "Thề luôn...", "Nhìn mê chưa nè", "Bác nào hay bị...", "Cái này đỉnh ở chỗ...", "Tưởng không ngon mà ngon không tưởng...", "Ưng cái bụng liền", "Đỡ tốn bao nhiêu thời gian".

2. TUYỆT ĐỐI CẤM CÁC TỪ NGỮ VĂN BẢN / SÁCH VỞ / RẬP KHUÔN MÁY MÓC CỦA AI:
   - CẤM: "chúng ta", "quý vị", "người tiêu dùng", "khách hàng", "tôi xin giới thiệu", "hãy cùng tôi khám phá", "trong thời đại công nghệ số ngày nay", "như chúng ta đã biết", "mang lại giải pháp tối ưu", "hiệu quả vượt trội", "thiết kế tinh xảo", "đáp ứng mọi nhu cầu", "sự lựa chọn hoàn hảo", "vô cùng quan trọng".
   - CẤM các câu giảng đạo lý sáo rỗng. Hãy đi thẳng vào cảm xúc, cảm giác cầm nắm, trải nghiệm dùng thử hoặc sự việc cụ thể trước mắt!

3. NHỊP CÂU NGẮN - NGẮT NGHỈ TỰ NHIÊN CHO GIỌNG ĐỌC TTS:
   - Mỗi câu chỉ dài 5 đến 12 từ. Dùng dấu phẩy (,) và dấu chấm (.) để ngắt nhịp thở rõ ràng.
   - Tránh câu ghép dài ngoằng khiến công cụ Text-To-Speech (CapCut Mai, Nam Trầm, Thanh Niên) đọc bị hết hơi hoặc dồn dập.

4. KÊU GỌI HÀNH ĐỘNG (CTA) CHÂN THẬT, KHÔNG HÔ HÀO QUẢNG CÁO:
   - Thay vì "Hãy nhanh tay đặt mua ngay hôm nay để nhận ưu đãi", hãy dùng: "Bác nào cần thì link mình để góc dưới nha", "Bà nào thích thì cmt mình gửi chỗ mua", "Lưu lại liền kẻo lúc cần tìm lại không thấy nha!".
"""


def generate_viral_hooks(topic: str, api_key: Optional[str] = None, num_hooks: int = 6) -> List[Dict[str, str]]:
    """Tạo các biến thể hook giật tít theo công thức của hook-generator trong social-media-skills."""
    system_instruction = (
        "Bạn là một bậc thầy sáng tạo nội dung mạng xã hội (TikTok, Instagram Reels, YouTube Shorts). "
        "Nhiệm vụ của bạn là tạo các câu Hook (mở đầu video 0-3s) có sức hút cực cao, dừng ngón tay người xem ngay lập tức. "
        "Tuyệt đối KHÔNG mở đầu bằng 'Chào các bạn', 'Hôm nay mình sẽ...', 'Tôi là...'. "
        "Lời văn phải 100% tự nhiên như người thật nói chuyện ngoài đời, biểu cảm sống động, không máy móc. "
        + HUMAN_VOICE_GUIDELINES
    )

    prompt = f"""Hãy tạo {num_hooks} câu Hook khác nhau cho chủ đề sau:
Chủ đề: "{topic}"

Yêu cầu xuất ra đúng định dạng mảng JSON thuần túy (không markdown thừa):
[
  {{
    "type": "Nghịch lý (Contrarian)",
    "hook": "Câu mở đầu cực kỳ bất ngờ, đảo ngược quan niệm...",
    "overlay_text": "CHỮ TO HIỂN THỊ MÀN HÌNH (Dưới 6 từ)"
  }},
  ...
]

6 Loại Hook cụ thể:
1. Nghịch lý / Trái với lẽ thường (Contrarian)
2. Khoảng trống tò mò (Curiosity Gap)
3. Cảnh báo sai lầm tốn kém (Mistake / Warning)
4. Sức mạnh con số cụ thể (Specific Numbers)
5. Hướng dẫn tốc độ cao (Quick How-To)
6. Kết quả biến đổi bất ngờ (Transformation)

Chỉ trả về JSON hợp lệ."""

    raw_text = call_gemini(prompt, api_key=api_key, system_instruction=system_instruction)
    if not raw_text:
        return []

    match = re.search(r'\[.*\]', raw_text, re.DOTALL)
    if match:
        raw_text = match.group(0)

    try:
        return json.loads(raw_text)
    except Exception as e:
        logger.error(f"Lỗi parse JSON hooks: {e}, text: {raw_text}")
        return [{"type": "Mặc định", "hook": topic, "overlay_text": topic[:20]}]


def generate_video_script(
    topic: str,
    platform: str = "tiktok",
    duration_target: int = 45,
    style: str = "engaging",
    hook_text: Optional[str] = None,
    custom_instruction: str = "",
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sinh kịch bản video ngắn hoàn chỉnh kế thừa kỹ năng reels-scripting từ social-media-skills.
    Lời văn gần gũi, tự nhiên như người thật trò chuyện, không sách vở hay máy móc.
    """

    system_instruction = (
        "Bạn là giám đốc sáng tạo nội dung video ngắn hàng đầu cho TikTok và Reels. "
        "Bạn luôn viết kịch bản bằng VĂN PHONG NGƯỜI THẬT, gần gũi, dí dỏm và chân thành. "
        "Tuyệt đối không dùng văn mẫu AI khô cứng, không dùng từ sáo rỗng. "
        + HUMAN_VOICE_GUIDELINES
    )

    hook_prompt = f'Bắt buộc dùng câu Hook này làm mở đầu: "{hook_text}"' if hook_text else "Hãy tự sáng tạo một Hook gây sốc, dừng ngón tay người xem trong 3 giây đầu."

    prompt = f"""Hãy viết một kịch bản video ngắn hoàn chỉnh cho chủ đề sau:
- Chủ đề: "{topic}"
- Nền tảng đích: {platform} (TikTok / Instagram Reels / Shorts)
- Thời lượng mục tiêu: khoảng {duration_target} giây (tổng số từ khoảng {int(duration_target * 2.4)} từ)
- Phong cách: {style}
- {hook_prompt}
{f"- Ghi chú bổ sung: {custom_instruction}" if custom_instruction else ""}

{HUMAN_VOICE_GUIDELINES}

BẮT BUỘC trả về đúng định dạng JSON như sau:
{{
  "title": "Tiêu đề video cuốn hút",
  "topic": "{topic}",
  "estimated_total_seconds": {duration_target},
  "scenes": [
    {{
      "index": 1,
      "section": "hook",
      "time_range": "00:00 - 00:03",
      "speaker_text": "Lời thoại nhân vật nói (ngắn gọn, dưới 10 từ, cực kỳ tự nhiên và cuốn hút)...",
      "visual_cue": "Gợi ý cảnh quay / B-roll...",
      "text_overlay": "CHỮ NỔI TRÊN MÀN HÌNH"
    }},
    {{
      "index": 2,
      "section": "point_1",
      "time_range": "00:03 - 00:15",
      "speaker_text": "Lời thoại phần đặt vấn đề hoặc nghịch lý...",
      "visual_cue": "Cảnh quay minh họa...",
      "text_overlay": "TỪ KHÓA ĐIỂM 1"
    }},
    {{
      "index": 3,
      "section": "point_2",
      "time_range": "00:15 - 00:35",
      "speaker_text": "Lời thoại phần giải pháp thực chiến...",
      "visual_cue": "Cảnh quay thực hành hoặc biểu đồ kết quả...",
      "text_overlay": "TỪ KHÓA ĐIỂM 2"
    }},
    {{
      "index": 4,
      "section": "cta",
      "time_range": "00:35 - {duration_target:02d}",
      "speaker_text": "Lời kêu gọi hành động cụ thể (Follow, bình luận từ khóa để nhận tài liệu...)...",
      "visual_cue": "Chỉ tay vào nút theo dõi hoặc hiện giao diện bình luận...",
      "text_overlay": "BÌNH LUẬN NGAY"
    }}
  ]
}}

Lưu ý: Có thể chia thành 4 đến 6 scenes tùy theo nhịp nội dung sao cho tổng thời lượng đúng xấp xỉ {duration_target} giây.
Chỉ trả về JSON thuần túy, không có text markdown mở đầu hay kết thúc."""

    raw_text = call_gemini(prompt, api_key=api_key, system_instruction=system_instruction)
    if not raw_text:
        raise RuntimeError("Gemini không trả về kết quả kịch bản.")

    match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if match:
        raw_text = match.group(0)

    try:
        script_data = json.loads(raw_text)
        for sc in script_data.get("scenes", []):
            words = len(str(sc.get("speaker_text", "")).split())
            sc["word_count"] = words
            sc["duration_seconds"] = round(max(words / 2.4, 2.0), 1)
        return script_data
    except Exception as e:
        logger.error(f"Lỗi parse JSON script: {e}, text: {raw_text}")
        raise RuntimeError(f"Không thể phân tích dữ liệu kịch bản từ AI: {e}")


def extract_smart_video_frames(
    video_path: str | Path,
    max_frames: int = 8,
    target_width: int = 640
) -> Tuple[List[Dict[str, Any]], float, Dict[str, Any]]:
    """
    Trích xuất các khung hình đại diện theo dòng thời gian của video để AI Vision phân tích.
    Tự động đo độ dài, FPS, kích thước và nén ảnh JPEG base64 tối ưu băng thông.
    """
    path_str = str(video_path).replace("local:///", "").replace("/", "\\")
    if not os.path.exists(path_str):
        raise FileNotFoundError(f"Không tìm thấy file video: {path_str}")

    cap = cv2.VideoCapture(path_str)
    try:
        if not cap.isOpened():
            raise ValueError(f"Không thể mở video qua OpenCV: {path_str}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_seconds = total_frames / fps if fps > 0 else 0.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if duration_seconds <= 0:
            raise ValueError("Không xác định được thời lượng video hoặc video bị lỗi.")

        num_samples = min(max(int(duration_seconds // 4), 4), max_frames)
        step = duration_seconds / (num_samples + 1)
        sample_times = [round(step * (i + 1), 2) for i in range(num_samples)]

        frames_data = []
        for ts in sample_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
            ret, frame = cap.read()
            if ret and frame is not None:
                h, w = frame.shape[:2]
                scale = float(target_width) / max(w, h)
                if scale < 1.0:
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
                
                _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                b64_str = base64.b64encode(buffer).decode('utf-8')
                mm = int(ts // 60)
                ss = int(ts % 60)
                frames_data.append({
                    "timestamp_sec": ts,
                    "timestamp_str": f"{mm:02d}:{ss:02d}",
                    "b64": b64_str
                })
    finally:
        cap.release()
    metadata = {
        "duration_seconds": round(duration_seconds, 2),
        "fps": round(fps, 2),
        "width": width,
        "height": height,
        "sampled_frames": len(frames_data)
    }
    return frames_data, duration_seconds, metadata


def analyze_video_and_generate_script(
    video_path: str | Path,
    genre: str = "review",
    style: str = "engaging",
    custom_instruction: str = "",
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    AI xem video qua các khung hình thời gian thực (Gemini Vision),
    hiểu nội dung đang diễn ra (sản phẩm gì, hành động gì) và
    dựng kịch bản chuẩn viral khớp chính xác với thời lượng video gốc.
    """
    frames_data, duration_seconds, metadata = extract_smart_video_frames(video_path, max_frames=8)
    
    target_duration = int(round(duration_seconds))
    target_words = int(round(duration_seconds * 2.4))
    
    genre_guidelines = {
        "review": (
            "Thể loại: REVIEW SẢN PHẨM NGOÀI ĐỜI THỰC. "
            "Cách nói chuyện: Giống như bạn vừa mua được món đồ này về dùng thử và quá ưng ý nên quay video chia sẻ thật lòng với bạn bè. "
            "Quan sát kỹ từng khung hình: Nhìn rõ món đồ là gì, kiểu dáng, màu sắc, cách cầm nắm hay thao tác sử dụng thực tế. "
            "Lời thoại phải trầm trồ, chân thật, khen điểm mạnh nhất và chỉ ra sự tiện lợi thực tế, không tâng bốc sách vở."
        ),
        "documentary": (
            "Thể loại: THUYẾT MINH / KỂ CHUYỆN CUỐN HÚT. "
            "Cách nói chuyện: Giọng điệu của một người kể chuyện cuốn hút, mở đầu bằng sự tò mò ('Ủa có bao giờ bạn thắc mắc...', 'Nhiều người tưởng là... nhưng thật ra...'). "
            "Dẫn dắt tự nhiên, giải thích đúng từng chi tiết xuất hiện trên màn hình, không giảng bài khô khan."
        ),
        "story": (
            "Thể loại: TÂM SỰ / ĐỜI SỐNG GẦN GŨI. "
            "Cách nói chuyện: Nhẹ nhàng, chân thành, như đang chia sẻ câu chuyện trải nghiệm của chính mình."
        ),
        "auto": (
            "Thể loại: TỰ ĐỘNG NHẬN DIỆN VÀ NÓI CHUYỆN TỰ NHIÊN. "
            "Tự nhận diện bối cảnh video và nói chuyện hoàn toàn bằng giọng điệu con người ngoài đời, sống động, chân thật."
        )
    }
    selected_genre_desc = genre_guidelines.get(genre, genre_guidelines["review"])

    system_instruction = (
        "Bạn là một đạo diễn và biên kịch video ngắn hàng đầu cho TikTok, Reels và YouTube Shorts. "
        "Bạn có năng lực đọc hiểu thị giác (Visual Understanding) xuất sắc. "
        "Bạn luôn viết kịch bản bằng VĂN PHONG NGƯỜI THẬT NGOÀI ĐỜI: gần gũi, dí dỏm, tự nhiên, không sách vở hay máy móc. "
        "Kịch bản đọc thoại PHẢI KHỚP CHÍNH XÁC VỚI THỜI LƯỢNG VIDEO. "
        + HUMAN_VOICE_GUIDELINES
    )

    parts: List[Dict[str, Any]] = []
    
    prompt_header = f"""Tôi cung cấp cho bạn {len(frames_data)} khung hình được trích xuất theo dòng thời gian từ một video thực tế dài {round(duration_seconds, 1)} giây ({target_duration}s).

{selected_genre_desc}
Phong cách yêu cầu: {style}.
{f"Yêu cầu bổ sung: {custom_instruction}" if custom_instruction else ""}

{HUMAN_VOICE_GUIDELINES}

YÊU CẦU TỐI QUAN TRỌNG VỀ THỜI LƯỢNG & KHỚP HÌNH:
1. Tổng thời lượng kịch bản PHẢI KHỚP VỚI VIDEO: khoảng {target_duration} giây.
2. Nhịp đọc tiếng Việt tự nhiên là ~2.3 đến 2.5 từ/giây. Do đó tổng số từ của toàn bộ lời thoại PHẢI XẤP XỈ: {target_words} từ.
3. Chia thành 3 đến 5 phân cảnh (scenes).
4. Mỗi phân cảnh có mốc thời gian (start_seconds -> end_seconds). Lời thoại của từng phân cảnh PHẢI MÔ TẢ VÀ DẪN DẮT ĐÚNG NHỮNG GÌ ĐANG XUẤT HIỆN TRÊN MÀN HÌNH ở khoảng thời gian đó.
5. Cấu trúc chuẩn viral:
   - Scene 1 (0-3s): HOOK mở đầu giật tít, tò mò, khớp với cảnh đầu, không chào hỏi lê thê.
   - Các scene thân bài: Trực tiếp bám sát diễn biến hình ảnh (review sản phẩm hoặc thuyết minh).
   - Scene kết: Kêu gọi hành động (CTA) ngắn gọn, tự nhiên.

Dưới đây là các khung hình video theo từng mốc thời gian:"""

    parts.append({"text": prompt_header})

    for f in frames_data:
        parts.append({"text": f"\n[Khung hình tại thời điểm {f['timestamp_str']} - giây thứ {f['timestamp_sec']}s]:"})
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": f["b64"]
            }
        })

    prompt_footer = f"""\nBẮT BUỘC trả về đúng định dạng JSON như sau (không có markdown thừa):
{{
  "title": "Tiêu đề video cuốn hút",
  "detected_subject": "Tên sản phẩm hoặc chủ đề cụ thể AI nhận diện được trong các khung hình",
  "genre": "{genre}",
  "video_duration": {round(duration_seconds, 1)},
  "summary": "Tóm tắt ngắn gọn 1-2 câu về nội dung video",
  "scenes": [
    {{
      "index": 1,
      "section": "hook",
      "time_range": "00:00 - 00:03",
      "start_seconds": 0.0,
      "end_seconds": 3.0,
      "speaker_text": "Lời thoại mở đầu (dưới 10 từ cho 3 giây đầu)...",
      "visual_cue": "Mô tả hình ảnh AI thấy ở khung hình đầu",
      "text_overlay": "CHỮ NỔI GÂY TÒ MÒ"
    }},
    {{
      "index": 2,
      "section": "point_1",
      "time_range": "00:03 - ...",
      "start_seconds": 3.0,
      "end_seconds": 15.0,
      "speaker_text": "Lời thoại khớp với hành động ở khung hình tiếp theo...",
      "visual_cue": "Mô tả hành động/tính năng...",
      "text_overlay": "TỪ KHÓA ĐIỂM 1"
    }},
    {{
      "index": 3,
      "section": "cta",
      "time_range": "15.0 - {target_duration:02d}:00",
      "start_seconds": 15.0,
      "end_seconds": {round(duration_seconds, 1)},
      "speaker_text": "Lời kêu gọi hành động kết thúc...",
      "visual_cue": "Cảnh kết thúc...",
      "text_overlay": "CHỮ NỔI KÊU GỌI"
    }}
  ]
}}

Chỉ trả về JSON thuần túy."""

    parts.append({"text": prompt_footer})

    raw_text = call_gemini(parts=parts, api_key=api_key, system_instruction=system_instruction)
    if not raw_text:
        raise RuntimeError("Gemini Vision không trả về kết quả phân tích video.")

    match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if match:
        raw_text = match.group(0)

    try:
        script_data = json.loads(raw_text)
        script_data["video_metadata"] = metadata
        for sc in script_data.get("scenes", []):
            words = len(str(sc.get("speaker_text", "")).split())
            sc["word_count"] = words
            sc["duration_seconds"] = round(max(words / 2.4, 2.0), 1)
        return script_data
    except Exception as e:
        logger.error(f"Lỗi parse JSON script từ video: {e}, text: {raw_text}")
        raise RuntimeError(f"Không thể phân tích dữ liệu kịch bản từ video: {e}")

