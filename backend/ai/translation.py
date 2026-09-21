import os
import sys
import io

# Fix Windows console UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import json
import time
import requests
import base64
import re
from deep_translator import GoogleTranslator, MyMemoryTranslator
import logging

from .model_policy import current_model_policy, ordered_unique

try:
    from ..subtitle_text import clean_incomplete_segment_stops, normalize_subtitle_text
except ImportError:
    from subtitle_text import clean_incomplete_segment_stops, normalize_subtitle_text

logger = logging.getLogger(__name__)


def _contains_cjk(text):
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))


def _is_translation_error_payload(value):
    text = str(value or "").strip()
    lowered = text.lower()
    return (
        not text
        or lowered.startswith("error")
        or "error 500" in lowered
        or "server error" in lowered
    )


def _subdivide_and_retry(translate_fn, texts, *, target_lang, api_key,
                          prior_context, model, provider_label="LLM", **kwargs):
    """Chia batch làm đôi khi LLM trả về sai số lượng câu, dịch từng nửa rồi ghép lại.

    ``translate_fn`` phải có signature ``(texts, target_lang=, api_key=, prior_context=, model=, **kwargs)``
    và trả về ``list[str] | None``.
    """
    mid = len(texts) // 2
    left_kwargs = dict(kwargs)
    right_kwargs = dict(kwargs)
    budgets = kwargs.get("duration_budgets")
    if budgets and len(budgets) == len(texts):
        left_kwargs["duration_budgets"] = budgets[:mid]
        right_kwargs["duration_budgets"] = budgets[mid:]
    if "context_end_seconds" in left_kwargs:
        left_kwargs.pop("context_end_seconds", None)
    if "context_start_seconds" in right_kwargs:
        right_kwargs.pop("context_start_seconds", None)
    left_res = translate_fn(
        texts[:mid], target_lang=target_lang, api_key=api_key,
        prior_context=prior_context, model=model, **left_kwargs
    )
    if left_res and len(left_res) == mid:
        combined_prior = list(prior_context or [])
        for s, t in zip(texts[:mid], left_res):
            combined_prior.append({"source": s, "translated": t})
        # Cập nhật prior_context cho nửa phải
        right_kwargs.pop("prior_context", None)
        right_res = translate_fn(
            texts[mid:], target_lang=target_lang, api_key=api_key,
            prior_context=combined_prior[-4:], model=model, **right_kwargs
        )
        if right_res and len(right_res) == len(texts) - mid:
            logger.info("Ghép nối thành công %d câu từ hai nửa batch %s!", len(texts), provider_label)
            return left_res + right_res
    return None


def _parse_json_array(raw_text):
    """Robustly extract and parse a JSON array of strings from LLM text output."""
    if not raw_text or not isinstance(raw_text, str):
        return None
    text = raw_text.strip()

    # Strip markdown code blocks ```json ... ``` or ``` ... ```
    if "```" in text:
        m = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', text, re.IGNORECASE)
        if m:
            text = m.group(1).strip()
        else:
            text = re.sub(r'```(?:json)?', '', text, flags=re.IGNORECASE)
            text = text.replace('```', '').strip()

    # Find the outermost array brackets [...]
    start_idx = text.find('[')
    end_idx = text.rfind(']')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx:end_idx + 1]

    # Try standard json.loads first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    # Clean trailing commas: [ "a", "b", ] -> [ "a", "b" ]
    cleaned = re.sub(r',\s*([\]\}])', r'\1', text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    # Fallback to ast.literal_eval for single-quoted Python-style lists
    try:
        import ast
        parsed = ast.literal_eval(cleaned)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass

    return None


def _validate_fallback_translation(source_text, translated_text, provider):
    if _is_translation_error_payload(translated_text):
        raise RuntimeError(
            "{} returned an error payload: {}".format(
                provider, str(translated_text or "")[:120]
            )
        )
    import unicodedata
    translated_text = unicodedata.normalize("NFC", str(translated_text or ""))
    try:
        from mojibake_repair import repair_vietnamese_mojibake
        translated_text = repair_vietnamese_mojibake(translated_text)
    except Exception:
        pass
    translated = normalize_subtitle_text(translated_text)
    if not translated:
        raise RuntimeError("{} returned no subtitle text after cleanup".format(provider))
    if _contains_cjk(source_text) and translated == normalize_subtitle_text(source_text):
        raise RuntimeError("{} left Chinese source text untranslated".format(provider))
    return translated


def _translate_with_resilient_fallback(source_text, target_lang):
    """Translate one segment without accepting provider error pages as text."""

    source_text = str(source_text)
    failures = []
    google_sources = ("zh-CN", "auto") if _contains_cjk(source_text) else ("auto",)
    for source_lang in google_sources:
        try:
            translated = GoogleTranslator(
                source=source_lang, target=target_lang
            ).translate(source_text)
            return _validate_fallback_translation(
                source_text, translated, "Google Translate ({})".format(source_lang)
            )
        except Exception as exc:
            failures.append("Google {}: {}".format(source_lang, exc))

    memory_source = "zh-CN" if _contains_cjk(source_text) else "auto"
    memory_target = "vi-VN" if target_lang.lower().startswith("vi") else target_lang
    try:
        translated = MyMemoryTranslator(
            source=memory_source, target=memory_target
        ).translate(source_text)
        return _validate_fallback_translation(
            source_text, translated, "MyMemory Translate"
        )
    except Exception as exc:
        failures.append("MyMemory: {}".format(exc))

    raise RuntimeError("; ".join(failures[-3:]))

def build_translation_prompt(
    texts,
    target_lang="vi",
    prior_context=None,
    with_vision=True,
    duration_budgets=None,
    glossary=None,
    entity_map=None,
    speaker_map=None,
):
    lang_name = "Tiếng Việt" if target_lang == "vi" else target_lang
    prompt = f"""Bạn là một chuyên gia dịch thuật nội dung mạng xã hội (Tiktok, Douyin).
Nhiệm vụ: Dịch mảng JSON chứa các câu phụ đề dưới đây sang {lang_name}.
Yêu cầu TỐI QUAN TRỌNG:
1. BẮT BUỘC giữ nguyên số lượng phần tử của mảng JSON. Mỗi câu gốc tương ứng đúng 1 câu dịch. Không tự ý gộp câu hay tách câu để đảm bảo khớp thời gian hiển thị (timing).
2. DỊCH CHUẨN XÁC, TỰ NHIÊN, HẤP DẪN: Ưu tiên dịch đúng nghĩa đen và bóng của câu chữ. Giữ văn phong tự nhiên, cuốn hút, có chút thiên hướng mạng xã hội để đăng video. Viết hoa chữ cái đầu câu hoặc sau dấu kết câu theo đúng ngữ pháp tiếng Việt.
3. XỬ LÝ TỪ NGỮ VĂN HOA/THƠ CA: Các video Douyin thường dùng câu chữ hoa mỹ. Ví dụ '懒春秋' mang ý nghĩa 'thư thái, nhàn hạ' chứ KHÔNG PHẢI là 'lười biếng'. Hãy dịch thoát ý, sang trọng.
4. TUYỆT ĐỐI KHÔNG lạm dụng từ tiếng Anh. Ưu tiên tiếng Việt thuần túy.
5. KHỚP KHẨU HÌNH & THỜI LƯỢNG (LIP-SYNC): Văn bản dịch dùng để lồng tiếng (TTS), độ dài âm tiết của câu tiếng Việt PHẢI TƯƠNG ĐƯƠNG VỚI CÂU GỐC để khớp hoàn hảo khẩu hình miệng của nhân vật (không được dịch quá dài khiến AI phải đọc quá nhanh, và không được dịch quá cụt khiến AI đọc xong trước khi nhân vật khép miệng).
6. ĐẶT DẤU NGẮT NGHỈ (DẤU PHẨY, DẤU CHẤM) CHUẨN XÁC THEO LỜI NÓI GỐC:
   Văn bản dịch dùng để lồng tiếng (TTS) và hiển thị phụ đề. Giọng đọc TTS chỉ ngắt nghỉ, lấy hơi khi gặp dấu phẩy (,) hoặc dấu chấm kết câu (. ! ?).
   - BẮT BUỘC viết hoa chữ cái đầu tiên của mỗi câu.
   - BẮT BUỘC đặt dấu phẩy (,) phân tách các vế trong câu ghép, câu có trạng ngữ, liên từ (như 'khi...', 'nếu...', 'nhưng...', 'lúc thì...'):
     * CÂU DÀI NỐI NHIỀU Ý BẮT BUỘC PHẢI CÓ DẤU PHẨY ĐỂ AI THỞ TỰ NHIÊN (ví dụ: 'Một trận rung chấn kinh hoàng, chia cắt nó khỏi đàn đang di cư, nó rơi xuống khe đá núi lửa.', 'Lúc thì cào lại đống đất bị dẫm loạn, hơn mười ngày sau, mảng bùn ẩm nứt ra một đường nhỏ.').
   - Nếu câu nói ngắn, liền một hơi không có ngắt nghỉ thì KHÔNG tự ý chèn dấu ngắt nghỉ ("nếu không thì thôi").
   - Kết thúc một câu hoàn chỉnh trọn vẹn ý nghĩa: BẮT BUỘC có dấu kết câu (. ! ?). Tuyệt đối không để câu hoàn chỉnh kết thúc cụt ngủn không dấu.
   - TUYỆT ĐỐI KHÔNG dùng dấu ba chấm (... hoặc …).
7. NGỮ CẢNH NỐI TIẾP: Vì phụ đề thường bị ngắt giữa chừng, hãy đọc cả đoạn để dịch sao cho ý nối liền mạch trơn tru.
    BẮT BUỘC giữ nguyên cách viết tên riêng, loài vật và đại từ đã xuất hiện trong phần ngữ cảnh batch trước, không dịch lại theo nghĩa.
    Tên nhân vật chính là 'A Thích' (阿特): BẮT BUỘC giữ nguyên xuyên suốt là 'A Thích', TUYỆT ĐỐI KHÔNG đổi thành 'Assassin', 'Assin', 'Sát thủ' hay bất kỳ tên nào khác!
    KHÔNG dùng dấu ba chấm (... hoặc …). Hệ thống sẽ chuyển phụ đề sang câu kế tiếp tại dấu kết câu; vẫn giữ đúng số phần tử JSON theo đầu vào.
8. BẮT LỖI ĐỒNG ÂM ASR DO NHẬN DẠNG GIỌNG NÓI (WHISPER): Phụ đề tiếng Trung gốc được trích xuất bằng ASR nên thường xuất hiện các từ đồng âm/gần âm sai trong video review, handmade, đồ gia dụng. Hãy dùng ngữ cảnh sản phẩm để tự động sửa:
   - '天手章' hoặc '手张' -> hiểu đúng là '贴手帐' hoặc '手帐' (dán sổ tay / chơi sổ Bullet Journal / planner); tuyệt đối KHÔNG dịch thành 'chương tay' hay 'quả trứng'.
   - '怪蛋' -> hiểu đúng là '怪诞' (kỳ ảo, kỳ thú, độc lạ); KHÔNG dịch thành 'quả trứng quái'.
   - '风味感' trong ngữ cảnh đồ dùng/thủ công -> hiểu đúng là '氛围感' (cảm giác không gian chill / vibe nghệ thuật); KHÔNG dịch thành 'hương vị ẩm thực' hay 'phong vị'.
   - '苏打' khi nói về keo dán/băng keo -> hiểu đúng là '胶带' / '调色板贴纸' (băng dính Washi Tape / sticker bảng màu); KHÔNG dịch thành nước sô-đa.
   - '叶芝麦' -> hiểu đúng là '叶之脉' (gân của chiếc lá); dịch thoát ý tự nhiên.
   - '可思线' -> hiểu đúng là '可撕线' (đường răng cưa dễ xé).
   - '比耶' -> tạo dáng chữ V / giơ tay chữ V (tạo dáng khi chụp ảnh, tuyệt đối KHÔNG dịch thành bia hay 'Bière').
   - '出片' -> chụp ảnh đẹp / lên hình đẹp / có ảnh ưng ý (tuyệt đối KHÔNG dịch thành 'ra khỏi bộ phim').
   - '修图' / '修好' -> chỉnh sửa ảnh / sửa ảnh xong (tuyệt đối KHÔNG dịch thành 'khắc phục' hay 'sửa chữa đồ đạc').
   - '去路人' -> xóa người qua đường / xóa người lạ khỏi ảnh (tính năng xóa người trong app ảnh, KHÔNG dịch thành 'đặt hàng người qua đường').
   - '小腿' -> bắp chân / cẳng chân (KHÔNG dịch thành 'bê').
   - '顶前面' / '往后面推' -> đẩy về phía trước / lùi về phía sau một chút.
"""
    if with_vision:
        prompt += "9. TRỰC QUAN: Hãy kết hợp các bức ảnh đính kèm từ video để chọn đại từ nhân xưng và danh từ chính xác tuyệt đối với ngữ cảnh.\n"
    prompt += "10. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
    if duration_budgets and len(duration_budgets) == len(texts):
        prompt += (
            "10. NGÂN SÁCH THỜI LƯỢNG cho từng phần tử, cùng thứ tự với mảng gốc:\n"
            + json.dumps(duration_budgets, ensure_ascii=False)
            + "\nseconds là số giây đọc; max_characters là giới hạn ký tự mong muốn (kể cả khoảng trắng). "
            "Hãy chọn câu dịch ngắn gọn, dễ đọc ngay từ lần đầu để vừa thời gian, "
            "nhưng giữ đủ ý chính, tên riêng, số lượng và phủ định. Không cắt cụt từ, "
            "không bỏ ý chỉ để đạt giới hạn; không trả về bảng ngân sách.\n"
        )
    if glossary:
        prompt += (
            "10. BẢNG THUẬT NGỮ CỐ ĐỊNH (GLOSSARY) - BẮT BUỘC tuân thủ chính xác các từ khóa sau:\n"
            + json.dumps(dict(glossary), ensure_ascii=False)
            + "\n"
        )
    if entity_map:
        prompt += (
            "11. BẢNG THỰC THỂ / TÊN RIÊNG (ENTITY MAP) - BẮT BUỘC dùng đúng tên thực thể/nhân vật/địa danh sau:\n"
            + json.dumps(dict(entity_map), ensure_ascii=False)
            + "\n"
        )
    if speaker_map:
        prompt += (
            "12. BẢNG NHÂN VẬT / NGƯỜI NÓI (SPEAKER MAP) - Dùng đúng vai vế và đại từ nhân xưng phù hợp cho từng nhân vật:\n"
            + json.dumps(dict(speaker_map), ensure_ascii=False)
            + "\n"
        )
    prompt += "Dữ liệu:\n"
    if prior_context:
        prompt += "Ngữ cảnh nối tiếp từ batch trước (để duy trì xưng hô và mạch truyện, KHÔNG dịch lại):\n"
        prompt += json.dumps(list(prior_context), ensure_ascii=False) + "\n"
    prompt += json.dumps(texts, ensure_ascii=False)
    return prompt

def extract_video_frames_base64(video_path, context_start_seconds=None, context_end_seconds=None, num_frames=3):
    if not video_path or not os.path.exists(video_path):
        return []
    try:
        import cv2
        try:
            from ..video_sampling import sample_video_frames
        except ImportError:
            from video_sampling import sample_video_frames
        frames = sample_video_frames(video_path, num_frames,
                                     context_start_seconds, context_end_seconds)
        b64_list = []
        for frame in frames:
            h, w = frame.shape[:2]
            if w > 640:
                scale = 640.0 / w
                frame = cv2.resize(frame, (640, int(h * scale)), interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                b64_str = base64.b64encode(buffer).decode('utf-8')
                b64_list.append(b64_str)
        return b64_list
    except Exception as img_e:
        logger.debug(f"Không thể trích xuất ảnh từ video: {img_e}")
        return []

_gemini_unhealthy_until = 0.0
_gemini_transient_failures = 0


def is_gemini_available() -> bool:
    return time.time() >= _gemini_unhealthy_until


def mark_gemini_unhealthy(cooldown_seconds: float = 300.0) -> None:
    global _gemini_unhealthy_until
    _gemini_unhealthy_until = time.time() + cooldown_seconds
    logger.warning("Gemini marked unhealthy; cooling down for %g seconds", cooldown_seconds)


def _gemini_transient_failure():
    global _gemini_transient_failures
    _gemini_transient_failures += 1
    if _gemini_transient_failures >= 2:
        mark_gemini_unhealthy(15.0)
        _gemini_transient_failures = 0


def translate_with_gemini(
    texts,
    target_lang="vi",
    api_key="",
    video_path=None,
    context_start_seconds=None,
    context_end_seconds=None,
    prior_context=None,
    timeout=None,
    **kwargs
):
    global _gemini_transient_failures
    api_key = api_key or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    if not is_gemini_available():
        logger.debug("Gemini currently in cooldown health cache, skipping.")
        return None
    try:
        prompt = build_translation_prompt(
            texts,
            target_lang,
            prior_context,
            with_vision=True,
            duration_budgets=kwargs.get("duration_budgets"),
            glossary=kwargs.get("glossary"),
            entity_map=kwargs.get("entity_map"),
            speaker_map=kwargs.get("speaker_map"),
        )
        parts = [{"text": prompt}]
        
        frames = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds, num_frames=3)
        for b64 in frames:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64
                }
            })
        if frames:
            logger.info(f"Đã đính kèm {len(frames)} ảnh từ video vào Gemini Vision.")
        
        models_to_try = current_model_policy().gemini_candidates
        response = None
        for model in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {"contents": [{"parts": parts}]}
            headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
            try:
                logger.info(f"Đang gọi Google Gemini ({model})...")
                request_timeout = timeout if timeout is not None else int(os.getenv("GEMINI_TRANSLATION_TIMEOUT", "20"))
                response = requests.post(url, json=payload, headers=headers, timeout=request_timeout)
                response.encoding = "utf-8"
                if response.status_code == 200:
                    result = response.json()
                    candidates = result.get("candidates", [])
                    if candidates:
                        parts_out = candidates[0].get("content", {}).get("parts", [])
                        raw_text = "".join(p.get("text", "") for p in parts_out if not p.get("thought")).strip()
                        translated = _parse_json_array(raw_text)
                        if (isinstance(translated, list) and len(translated) == len(texts)
                                and all(isinstance(item, str) and item.strip() for item in translated)):
                            try:
                                from mojibake_repair import repair_vietnamese_mojibake
                                translated = [repair_vietnamese_mojibake(t) for t in translated]
                            except Exception:
                                pass
                            _gemini_transient_failures = 0
                            logger.info(f"Dịch thành công bằng Google Gemini ({model})!")
                            return translated
                        elif isinstance(translated, list) and len(texts) >= 4 and len(translated) != len(texts):
                            logger.warning(
                                "Gemini %s trả về %d câu cho %d câu gốc. Đang kích hoạt chia nhỏ thích ứng (divide-and-conquer)...",
                                model, len(translated), len(texts)
                            )
                            split_res = _subdivide_and_retry(
                                translate_with_gemini, texts, target_lang=target_lang, api_key=api_key,
                                prior_context=prior_context, model=model, provider_label="Gemini",
                                video_path=video_path, context_start_seconds=context_start_seconds,
                                context_end_seconds=context_end_seconds, timeout=timeout, **kwargs
                            )
                            if split_res:
                                return split_res
                    logger.warning("Gemini %s không trả về định dạng JSON hợp lệ, chuyển sang model backup kế tiếp...", model)
                    continue
                elif response.status_code in (401, 403):
                    logger.warning(f"Lỗi xác thực API Key cho {model} (HTTP {response.status_code}) - cooldown activated")
                    mark_gemini_unhealthy(300.0)
                    break
                elif response.status_code == 429:
                    logger.warning(f"Lỗi giới hạn tần suất {model} (HTTP 429 Rate Limit) -> Tự động backup sang model tiếp theo...")
                    time.sleep(0.5)
                    continue
                elif response.status_code == 503:
                    logger.warning(f"Lỗi máy chủ Google quá tải {model} (HTTP 503 High Demand) -> Tự động backup sang model tiếp theo...")
                    time.sleep(0.5)
                    continue
                elif response.status_code == 404:
                    logger.warning(f"Model {model} không khả dụng (HTTP 404) -> Tự động backup sang model tiếp theo...")
                    continue
                else:
                    logger.warning(f"Lỗi gọi {model} (HTTP {response.status_code}) -> Tự động backup sang model tiếp theo...")
                    continue
            except Exception as req_e:
                logger.warning("Gemini %s gặp lỗi (%s: %s) -> Tự động backup sang model tiếp theo...", model, type(req_e).__name__, req_e)
                _gemini_transient_failure()
                continue

        logger.warning("Tất cả các model Gemini trong danh sách backup (%s) đều không thực hiện được batch này.", ", ".join(models_to_try))
        if _gemini_transient_failures == 0:
            _gemini_transient_failure()
    except Exception as e:
        logger.warning("Lỗi ngoài dự kiến trong translate_with_gemini: %s", type(e).__name__)
    return None

def translate_with_openai(
    texts,
    target_lang="vi",
    api_key="",
    video_path=None,
    context_start_seconds=None,
    context_end_seconds=None,
    prior_context=None,
    model=None,
    **kwargs
):
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        prompt = build_translation_prompt(
            texts,
            target_lang,
            prior_context,
            with_vision=True,
            duration_budgets=kwargs.get("duration_budgets"),
            glossary=kwargs.get("glossary"),
            entity_map=kwargs.get("entity_map"),
            speaker_map=kwargs.get("speaker_map"),
        )
        messages_content = [{"type": "text", "text": prompt}]
        
        frames = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds)
        for b64 in frames:
            messages_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
            
        policy = current_model_policy()
        seen_models = ordered_unique(model, *policy.openai_candidates)
            
        for om in seen_models:
            try:
                logger.info(f"Đang gọi OpenAI ChatGPT ({om})...")
                url = "https://api.openai.com/v1/chat/completions"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"
                }
                payload = {
                    "model": om,
                    "messages": [
                        {"role": "system", "content": "You are a professional social media video translator. Always respond with pure JSON arrays of translated strings without any markdown or formatting."},
                        {"role": "user", "content": messages_content if frames else prompt}
                    ],
                    "temperature": 0.3
                }
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    translated = _parse_json_array(text)
                    if isinstance(translated, list) and len(translated) == len(texts):
                        logger.info(f"Dịch thành công bằng OpenAI ChatGPT ({om})!")
                        return translated
                    elif isinstance(translated, list) and len(texts) >= 4 and len(translated) != len(texts):
                        logger.warning("OpenAI %s trả về lệch số lượng câu (%d thay vì %d). Chia nhỏ batch...", om, len(translated), len(texts))
                        split_res = _subdivide_and_retry(
                            translate_with_openai, texts, target_lang=target_lang, api_key=api_key,
                            prior_context=prior_context, model=om, provider_label="OpenAI",
                            video_path=video_path, context_start_seconds=context_start_seconds,
                            context_end_seconds=context_end_seconds, **kwargs
                        )
                        if split_res:
                            return split_res
                else:
                    logger.warning(f"Lỗi OpenAI ({om}) HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as o_err:
                logger.warning(f"Lỗi gọi OpenAI ({om}): {o_err}")
    except Exception as e:
        logger.warning(f"Lỗi dịch OpenAI: {e}")
    return None

def translate_with_deepseek(
    texts,
    target_lang="vi",
    api_key="",
    prior_context=None,
    model=None,
    **kwargs
):
    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    try:
        prompt = build_translation_prompt(
            texts,
            target_lang,
            prior_context,
            with_vision=False,
            duration_budgets=kwargs.get("duration_budgets"),
            glossary=kwargs.get("glossary"),
            entity_map=kwargs.get("entity_map"),
            speaker_map=kwargs.get("speaker_map"),
        )
        policy = current_model_policy()
        seen = ordered_unique(model, *policy.deepseek_candidates)
            
        for dm in seen:
            try:
                logger.info(f"Đang gọi DeepSeek ({dm})...")
                url = "https://api.deepseek.com/v1/chat/completions"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"
                }
                payload = {
                    "model": dm,
                    "messages": [
                        {"role": "system", "content": "You are a professional social media video translator. Always respond with pure JSON arrays of translated strings without any markdown or formatting."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3
                }
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    translated = _parse_json_array(text)
                    if isinstance(translated, list) and len(translated) == len(texts):
                        logger.info(f"Dịch thành công bằng DeepSeek ({dm})!")
                        return translated
                    elif isinstance(translated, list) and len(texts) >= 4 and len(translated) != len(texts):
                        logger.warning("DeepSeek %s trả về lệch số lượng câu (%d thay vì %d). Chia nhỏ batch...", dm, len(translated), len(texts))
                        split_res = _subdivide_and_retry(
                            translate_with_deepseek, texts, target_lang=target_lang, api_key=api_key,
                            prior_context=prior_context, model=dm, provider_label="DeepSeek", **kwargs
                        )
                        if split_res:
                            return split_res
                else:
                    logger.warning(f"Lỗi DeepSeek ({dm}) HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as d_err:
                logger.warning(f"Lỗi gọi DeepSeek ({dm}): {d_err}")
    except Exception as e:
        logger.warning(f"Lỗi dịch DeepSeek: {e}")
    return None

def translate_with_g4f(texts, target_lang="vi"):
    try:
        import g4f
        lang_name = "Tiếng Việt" if target_lang == "vi" else target_lang
        prompt = f"""Bạn là một chuyên gia dịch thuật nội dung mạng xã hội (Tiktok, Douyin).
Nhiệm vụ: Dịch mảng JSON chứa các câu phụ đề dưới đây sang {lang_name}.
Yêu cầu TỐI QUAN TRỌNG:
1. BẮT BUỘC giữ nguyên số lượng phần tử của mảng JSON.
2. Dịch tự nhiên, cuốn hút, chuẩn văn phong video ngắn mạng xã hội. Viết hoa chữ cái đầu câu.
3. Đặt dấu phẩy (,) tại các vế câu / chỗ ngắt nghỉ tự nhiên của câu nói gốc để lồng tiếng TTS có nhịp thở. Nếu câu ngắn nói liền một hơi thì không thêm dấu. Chỉ dùng dấu chấm (. ! ?) khi kết thúc câu hoàn chỉnh, không đặt dấu chấm ở câu lửng. KHÔNG dùng dấu ba chấm (... hoặc …).
4. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown; giữ nguyên số phần tử JSON.
Dữ liệu:
"""
        prompt += json.dumps(texts, ensure_ascii=False)
        fallback_models = ["gpt-4o", "deepseek-v3", "claude-3.5-sonnet"]
        for g4f_model in fallback_models:
            try:
                logger.info(f"Đang gọi mô hình AI miễn phí {g4f_model} qua G4F...")
                response = g4f.ChatCompletion.create(
                    model=g4f_model,
                    messages=[{"role": "user", "content": prompt}]
                )
                text = response.strip()
                match = re.search(r'\[[\s\S]*\]', text)
                if match:
                    text = match.group(0)
                translated = json.loads(text)
                if len(translated) == len(texts):
                    logger.info(f"Dịch thành công bằng mô hình {g4f_model}!")
                    return translated
            except Exception as m_err:
                logger.debug(f"Mô hình {g4f_model} gặp lỗi: {m_err}")
    except Exception as e:
        logger.debug(f"Lỗi dịch G4F: {e}")
    return None


def ensure_speech_pauses_and_entity(source_texts, translated_texts, entity_map=None, prior_context=None):
    """Normalize translated texts with proper capitalization, pauses, and entity names."""
    if not translated_texts or len(source_texts) != len(translated_texts):
        return translated_texts

    forbidden_variants = [
        (re.compile(r"\b(?:assassin|assin|sát thủ|thích khách)\b", re.IGNORECASE), "A Thích"),
    ]

    intro_adverbial_pattern = re.compile(
        r"^(?P<lead>(?:Trước khi|Sau khi|Đến khi|Khi|Nếu|Dù|Tuy|Mặc dù|Vì|Do|Nhờ|Để|Sau đó|Trước đó|Từ đó|Về sau|Sau này|Lúc này|Đến nay|Hiện tại|Cuối cùng|Đồng thời|Mặt khác|Thậm chí|Ngược lại)\s+[^,]{0,35}?)\s+(?=(?:nhất định|sẽ|thì|đều|lại|vẫn|phải|liền|đành|buộc phải|không thể|đã|nó|họ|anh|cô|chúng|ông|bà|tôi|bạn|là|đang|cũng)\b)",
        re.IGNORECASE,
    )
    clause_split_patterns = [
        re.compile(r"(\s+)(nhưng\b|mà\b|cho nên\b|thế nhưng\b|tuy nhiên\b|thậm chí\b|ngược lại\b|do đó\b|vì vậy\b|vì thế\b|đồng thời\b|bởi vậy\b|mặt khác\b)", re.IGNORECASE),
        re.compile(r"(\s+)(khi\s+bạn\b|khi\s+mùa\b|khi\s+nó\b|đến\s+khi\b|đến\s+chập\s+tối\b|ngày\s+hôm\s+sau\b|ngày\s+hôm\s+trước\b|hơn\s+mười\s+ngày\s+sau\b|mấy\s+ngày\s+sau\b|vài\s+ngày\s+sau\b|ngay\s+sau\s+đó\b|ít\s+lâu\s+sau\b)", re.IGNORECASE),
        re.compile(r"(\s+)(chia\s+cắt\s+nó\b|nó\s+rơi\s+xuống\b|nó\s+rơi\s+vào\b|đúng\s+vào\s+lúc\b|lúc\s+này\b|đúng\s+lúc\s+này\b)", re.IGNORECASE),
        re.compile(r"(\s+)(lúc\s+thì\b|khi\s+thì\b|nhưng\s+ngày\s+nào\b|cuối\s+cùng\s+vẫn\b|cuối\s+cùng\s+chậm\s+chạp\b|và\s+rồi\b|rồi\s+lại\b)", re.IGNORECASE),
    ]
    mid_word_pattern = re.compile(
        r"(\s+)(sẽ\b|đã\b|đang\b|lại\b|vẫn\b|để\b|thì\b|và\b|cũng\b|liền\b|đều\b|khiến\b|cho\b|bị\b|được\b|nhưng\b|mà\b|bởi\b|do\b|tuy\b|là\b)",
        re.IGNORECASE,
    )

    out = []
    for src, tr in zip(source_texts, translated_texts):
        clean = normalize_subtitle_text(str(tr or "")).strip()
        if not clean:
            out.append(clean)
            continue

        for pat, replacement in forbidden_variants:
            if "a thích" not in clean.lower():
                clean = pat.sub(replacement, clean)

        first_char = clean[0]
        if first_char.isalpha() and not first_char.isupper():
            clean = first_char.upper() + clean[1:]

        clean = re.sub(r"\s*[\.]{2,}\s*", ", ", clean)
        clean = re.sub(r"\s*…\s*", ", ", clean)
        clean = normalize_subtitle_text(clean)

        src_has_pause = any(p in src for p in ("，", ",", "；", ";", "、"))
        words = clean.split()
        if (src_has_pause or len(words) >= 8) and "," not in clean:
            m_intro = intro_adverbial_pattern.search(clean)
            if m_intro and len(clean) - m_intro.end("lead") >= 6:
                clean = m_intro.group("lead") + "," + clean[m_intro.end("lead"):]
            else:
                for pat in clause_split_patterns:
                    m = pat.search(clean)
                    if m and m.start() >= 10 and len(clean) - m.end() >= 6:
                        clean = clean[:m.start()] + "," + clean[m.start():]
                        break
            clean = normalize_subtitle_text(clean)

        if (src_has_pause or len(words) >= 11) and "," not in clean:
            candidates = []
            for m in mid_word_pattern.finditer(clean):
                if m.start() >= 10 and len(clean) - m.end() >= 8:
                    dist = abs((len(clean) / 2) - m.start())
                    candidates.append((dist, m.start()))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                best_pos = candidates[0][1]
                clean = clean[:best_pos] + "," + clean[best_pos:]
                clean = normalize_subtitle_text(clean)

        # Fallback to source pause ratio if source has pause but translation still lacks comma
        words = clean.split()
        if (src_has_pause or len(words) >= 12) and "," not in clean and len(words) >= 6:
            pause_chars = [i for i, c in enumerate(src) if c in ("，", ",", "；", ";", "、")]
            ratio = pause_chars[0] / max(len(src), 1) if pause_chars else 0.45
            split_idx = max(2, min(len(words) - 2, round(len(words) * ratio)))
            if split_idx < len(words) and words[split_idx].lower() == "thích" and words[split_idx - 1].lower() == "a":
                split_idx += 1
            clean = " ".join(words[:split_idx]) + ", " + " ".join(words[split_idx:])
            clean = normalize_subtitle_text(clean)

        # Long sentence (>= 18 words) secondary pause
        words = clean.split()
        if len(words) >= 18 and clean.count(",") == 1:
            parts = clean.split(",", 1)
            for i, p in enumerate(parts):
                p_words = p.strip().split()
                if len(p_words) >= 10:
                    cand = []
                    for m in mid_word_pattern.finditer(p):
                        if m.start() >= 10 and len(p) - m.end() >= 8:
                            cand.append((abs((len(p) / 2) - m.start()), m.start()))
                    if cand:
                        cand.sort(key=lambda x: x[0])
                        bpos = cand[0][1]
                        parts[i] = p[:bpos] + "," + p[bpos:]
                        clean = ",".join(parts)
                        clean = normalize_subtitle_text(clean)
                        break

        src_ends_terminal = any(str(src).strip().endswith(p) for p in ("。", "！", "？", ".", "!", "?"))
        words = clean.split()
        if not clean.endswith((".", "!", "?", ",", ";", ":")):
            if not bool(re.search(r"(?i)\b(thì|mà|nhưng|hoặc|và|lại|khi|lúc|sau|trước|đến)$", clean)):
                if src_ends_terminal or len(words) >= 6:
                    clean += "."

        out.append(clean)

    return out


def validate_translation_batch_quality(
    source_texts,
    translated_texts,
    prior_context=None,
    entity_map=None,
    glossary=None,
    strict=True,
):
    """Quality gate validating a batch of translated subtitles before TTS / persistence."""
    reasons = []
    if not isinstance(translated_texts, (list, tuple)):
        return False, ["Bản dịch không phải là danh sách hợp lệ"]
    if len(source_texts) != len(translated_texts):
        return False, [f"Số câu dịch ({len(translated_texts)}) không khớp số câu gốc ({len(source_texts)})"]

    forbidden_variants = ["assassin", "assin", "sát thủ", "thích khách"]

    for idx, (src, trans) in enumerate(zip(source_texts, translated_texts), 1):
        raw_tr = str(trans or "").strip()
        if not raw_tr:
            reasons.append(f"Câu {idx} rỗng nội dung")
            continue

        if "..." in raw_tr or "…" in raw_tr:
            reasons.append(f"Câu {idx} chứa dấu ba chấm ('...'): '{raw_tr}'")

        if _contains_cjk(raw_tr):
            reasons.append(f"Câu {idx} còn sót chữ Hán (CJK): '{raw_tr}'")

        clean_tr = normalize_subtitle_text(raw_tr).strip()
        if not clean_tr:
            reasons.append(f"Câu {idx} rỗng nội dung sau chuẩn hóa")
            continue

        first_char = clean_tr[0]
        if first_char.isalpha() and not first_char.isupper():
            reasons.append(f"Câu {idx} chưa viết hoa chữ cái đầu: '{clean_tr}'")

        for bad in forbidden_variants:
            if bad in clean_tr.lower() and "a thích" not in clean_tr.lower():
                reasons.append(
                    f"Câu {idx} dùng biến thể sai lệch '{bad}' thay vì 'A Thích': '{clean_tr}'"
                )

        words = clean_tr.split()
        src_has_comma = any(p in src for p in ("，", ",", "；", ";", "、"))
        tr_has_comma = "," in clean_tr

        if src_has_comma and len(words) >= 6 and not tr_has_comma:
            reasons.append(
                f"Câu {idx} là câu nhiều vế (nguồn có ngắt nghỉ) nhưng thiếu dấu phẩy: '{clean_tr}'"
            )
        elif len(words) >= 14 and not tr_has_comma:
            reasons.append(
                f"Câu {idx} dài ({len(words)} từ) nối nhiều ý nhưng thiếu dấu phẩy ngắt nghỉ: '{clean_tr}'"
            )

    return len(reasons) == 0, reasons


def translate_subtitles(
    srt_segments,
    target_lang="vi",
    api_key="",
    video_path=None,
    context_start_seconds=None,
    context_end_seconds=None,
    prior_context=None,
    strict=False,
    enable_g4f=True,
    glossary=None,
    entity_map=None,
    speaker_map=None,
    **kwargs
):
    logger.info("Translating subtitles...")
    kwargs = dict(kwargs)
    quality_metadata = kwargs.get("quality_metadata")
    if glossary is not None:
        kwargs["glossary"] = glossary
    if entity_map is not None:
        kwargs["entity_map"] = entity_map
    if speaker_map is not None:
        kwargs["speaker_map"] = speaker_map
    texts = [seg.content for seg in srt_segments if seg.content]
    if not texts:
        return srt_segments
        
    translated_texts = None
    preferred_provider = os.getenv("LLM_PROVIDER", "auto").lower()
    
    # Danh sách thứ tự ưu tiên các nhà cung cấp LLM
    providers_order = []
    if preferred_provider == "openai":
        providers_order = ["openai", "gemini", "deepseek"]
    elif preferred_provider == "deepseek":
        providers_order = ["deepseek", "gemini", "openai"]
    else: # auto / gemini
        providers_order = ["gemini", "openai", "deepseek"]
        
    used_provider = None
    provider_kind = None
    for p in providers_order:
        if translated_texts:
            break
        if p == "gemini":
            g_key = api_key or os.getenv("GEMINI_API_KEY", "")
            if g_key:
                translated_texts = translate_with_gemini(
                    texts, target_lang, g_key, video_path,
                    context_start_seconds=context_start_seconds,
                    context_end_seconds=context_end_seconds,
                    prior_context=prior_context,
                    **kwargs
                )
                if translated_texts:
                    used_provider = "gemini"
                    provider_kind = "llm"
        elif p == "openai":
            o_key = os.getenv("OPENAI_API_KEY", "")
            if o_key:
                translated_texts = translate_with_openai(
                    texts, target_lang, o_key, video_path,
                    context_start_seconds=context_start_seconds,
                    context_end_seconds=context_end_seconds,
                    prior_context=prior_context,
                    **kwargs
                )
                if translated_texts:
                    used_provider = "openai"
                    provider_kind = "llm"
        elif p == "deepseek":
            d_key = os.getenv("DEEPSEEK_API_KEY", "")
            if d_key:
                translated_texts = translate_with_deepseek(
                    texts, target_lang, d_key,
                    prior_context=prior_context,
                    **kwargs
                )
                if translated_texts:
                    used_provider = "deepseek"
                    provider_kind = "llm"
                
    # Fallback Tier: G4F Free
    if not translated_texts and enable_g4f and not strict:
        logger.info("Trying ChatGPT (G4F) API...")
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(translate_with_g4f, texts, target_lang)
            try:
                translated_texts = future.result(timeout=40)
                if translated_texts:
                    used_provider = "g4f"
                    provider_kind = "free_llm"
            except concurrent.futures.TimeoutError:
                logger.warning("G4F phản hồi quá lâu (quá 40s), hủy để tránh treo bot.")
                translated_texts = None
            except Exception as e:
                logger.warning(f"Lỗi G4F: {e}")
                translated_texts = None

    if strict:
        if not translated_texts or len(translated_texts) != len(texts):
            raise RuntimeError("Strict translation mode requires a complete LLM translation without machine fallback")
        if any(_contains_cjk(str(t or "")) for t in translated_texts):
            raise RuntimeError("Strict translation mode requires a complete LLM translation without CJK text")

    if isinstance(translated_texts, list):
        translated_texts = [
            normalize_subtitle_text(item) if isinstance(item, str) else item
            for item in translated_texts
        ]
        translated_texts = clean_incomplete_segment_stops(translated_texts)
        translated_texts = ensure_speech_pauses_and_entity(
            texts,
            translated_texts,
            entity_map=entity_map or kwargs.get("entity_map"),
            prior_context=prior_context,
        )

    translated_texts_valid = bool(
        translated_texts
        and len(translated_texts) == len(texts)
        and all(isinstance(item, str) and item.strip() for item in translated_texts)
    )
    if translated_texts_valid and strict:
        is_valid, reasons = validate_translation_batch_quality(
            texts,
            translated_texts,
            prior_context=prior_context,
            entity_map=entity_map or kwargs.get("entity_map"),
            glossary=glossary or kwargs.get("glossary"),
            strict=True,
        )
        if not is_valid:
            raise RuntimeError(
                f"Strict translation mode requires a complete LLM translation conforming to quality rules: {'; '.join(reasons)}"
            )

    if translated_texts_valid and not strict:
        unchanged_cjk = [
            position
            for position, (source, translated) in enumerate(
                zip(texts, translated_texts), 1
            )
            if _contains_cjk(source)
            and normalize_subtitle_text(source) == str(translated).strip()
        ]
        if unchanged_cjk:
            logger.warning(
                "AI translation left CJK source unchanged at positions: %s",
                ", ".join(str(position) for position in unchanged_cjk),
            )
            # Dịch bù chọn lọc cho từng câu bị sót chữ Hán bằng fallback để không làm mất các câu đã dịch tốt
            for position in unchanged_cjk:
                idx = position - 1
                src = texts[idx]
                try:
                    retranslated = _translate_with_resilient_fallback(src, target_lang)
                    if retranslated and not (_contains_cjk(src) and normalize_subtitle_text(src) == normalize_subtitle_text(retranslated)):
                        translated_texts[idx] = retranslated
                        logger.info("Dịch bù thành công CJK câu %d qua fallback: %s", position, retranslated)
                except Exception as fb_err:
                    logger.warning("Dịch bù câu %d thất bại: %s", position, fb_err)

            # Kiểm tra lại xem còn câu nào chưa được dịch thoát chữ Hán không
            still_unchanged = [
                position
                for position, (source, translated) in enumerate(
                    zip(texts, translated_texts), 1
                )
                if _contains_cjk(source)
                and normalize_subtitle_text(source) == str(translated).strip()
            ]
            if still_unchanged:
                translated_texts_valid = False

    if translated_texts_valid:
        if isinstance(quality_metadata, dict):
            quality_metadata["provider"] = used_provider
            quality_metadata["provider_kind"] = provider_kind
        idx = 0
        for segment in srt_segments:
            if not segment.content:
                continue
            segment.orig_content = segment.content
            segment.content = translated_texts[idx]
            idx += 1
        logger.info("LLM translation successful.")
        return srt_segments

    if strict:
        raise RuntimeError("Strict translation mode requires a complete LLM translation without machine fallback")

    logger.info("Falling back to Google Translate...")
    failed_segments = []
    for segment in srt_segments:
        if not segment.content:
            continue
            
        try:
            segment.orig_content = segment.content
            translated_text = _translate_with_resilient_fallback(
                segment.content, target_lang
            )
                
        except Exception as e:
            logger.warning(f"Lỗi dịch thuật: {e}")
            failed_segments.append(int(getattr(segment, "index", 0)))
            translated_text = segment.content
            
        segment.content = translated_text
        
    if strict and failed_segments:
        raise RuntimeError(
            "Translation failed for segment indexes: {}".format(
                ", ".join(str(index) for index in failed_segments)
            )
        )

    clean_incomplete_segment_stops(srt_segments)
    return srt_segments
