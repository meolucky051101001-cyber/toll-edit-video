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
2. DỊCH CHUẨN XÁC NHƯNG HẤP DẪN: Ưu tiên dịch đúng nghĩa đen và bóng của câu chữ. Giữ văn phong tự nhiên, cuốn hút, có chút thiên hướng mạng xã hội để đăng video.
3. XỬ LÝ TỪ NGỮ VĂN HOA/THƠ CA: Các video Douyin thường dùng câu chữ hoa mỹ. Ví dụ '懒春秋' mang ý nghĩa 'thư thái, nhàn hạ' chứ KHÔNG PHẢI là 'lười biếng'. Hãy dịch thoát ý, sang trọng.
4. TUYỆT ĐỐI KHÔNG lạm dụng từ tiếng Anh. Ưu tiên tiếng Việt thuần túy.
5. KHỚP KHẨU HÌNH & THỜI LƯỢNG (LIP-SYNC): Văn bản dịch dùng để lồng tiếng (TTS), độ dài âm tiết của câu tiếng Việt PHẢI TƯƠNG ĐƯƠNG VỚI CÂU GỐC để khớp hoàn hảo khẩu hình miệng của nhân vật (không được dịch quá dài khiến AI phải đọc quá nhanh, và không được dịch quá cụt khiến AI đọc xong trước khi nhân vật khép miệng).
6. Ngữ cảnh nối tiếp: Vì phụ đề thường bị ngắt giữa chừng, hãy đọc cả đoạn để dịch sao cho ý nối liền mạch trơn tru.
    KHÔNG dùng dấu ba chấm (... hoặc …), kể cả đầu/cuối đoạn bị ngắt. TUYỆT ĐỐI KHÔNG chèn dấu chấm (.) giữa câu lửng hoặc ở cuối các vế câu chưa hết ý. Chỉ đặt dấu kết câu (. ! ?) khi đã kết thúc một câu hoàn chỉnh trọn vẹn ý nghĩa. Hệ thống sẽ chuyển phụ đề sang câu kế tiếp tại dấu kết câu; vẫn giữ đúng số phần tử JSON theo đầu vào.
7. BẮT LỖI ĐỒNG ÂM ASR DO NHẬN DẠNG GIỌNG NÓI (WHISPER): Phụ đề tiếng Trung gốc được trích xuất bằng ASR nên thường xuất hiện các từ đồng âm/gần âm sai trong video review, handmade, đồ gia dụng. Hãy dùng ngữ cảnh sản phẩm để tự động sửa:
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
        prompt += "8. TRỰC QUAN: Hãy kết hợp các bức ảnh đính kèm từ video để chọn đại từ nhân xưng và danh từ chính xác tuyệt đối với ngữ cảnh.\n"
    prompt += "9. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
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
                            mid = len(texts) // 2
                            left_kwargs = dict(kwargs)
                            right_kwargs = dict(kwargs)
                            budgets = kwargs.get("duration_budgets")
                            if budgets and len(budgets) == len(texts):
                                left_kwargs["duration_budgets"] = budgets[:mid]
                                right_kwargs["duration_budgets"] = budgets[mid:]
                            left_res = translate_with_gemini(
                                texts[:mid], target_lang=target_lang, api_key=api_key,
                                video_path=video_path, context_start_seconds=context_start_seconds,
                                prior_context=prior_context, timeout=timeout, **left_kwargs
                            )
                            if left_res and len(left_res) == mid:
                                combined_prior = list(prior_context or [])
                                for s, t in zip(texts[:mid], left_res):
                                    combined_prior.append({"source": s, "translated": t})
                                right_res = translate_with_gemini(
                                    texts[mid:], target_lang=target_lang, api_key=api_key,
                                    video_path=video_path, context_end_seconds=context_end_seconds,
                                    prior_context=combined_prior[-4:], timeout=timeout, **right_kwargs
                                )
                                if right_res and len(right_res) == len(texts) - mid:
                                    logger.info("Ghép nối thành công %d câu từ hai nửa batch Gemini!", len(texts))
                                    return left_res + right_res
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
                        mid = len(texts) // 2
                        left_kwargs = dict(kwargs)
                        right_kwargs = dict(kwargs)
                        budgets = kwargs.get("duration_budgets")
                        if budgets and len(budgets) == len(texts):
                            left_kwargs["duration_budgets"] = budgets[:mid]
                            right_kwargs["duration_budgets"] = budgets[mid:]
                        left_res = translate_with_openai(
                            texts[:mid], target_lang=target_lang, api_key=api_key,
                            video_path=video_path, context_start_seconds=context_start_seconds,
                            prior_context=prior_context, model=om, **left_kwargs
                        )
                        if left_res and len(left_res) == mid:
                            combined_prior = list(prior_context or [])
                            for s, t in zip(texts[:mid], left_res):
                                combined_prior.append({"source": s, "translated": t})
                            right_res = translate_with_openai(
                                texts[mid:], target_lang=target_lang, api_key=api_key,
                                video_path=video_path, context_end_seconds=context_end_seconds,
                                prior_context=combined_prior[-4:], model=om, **right_kwargs
                            )
                            if right_res and len(right_res) == len(texts) - mid:
                                return left_res + right_res
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
                        mid = len(texts) // 2
                        left_kwargs = dict(kwargs)
                        right_kwargs = dict(kwargs)
                        budgets = kwargs.get("duration_budgets")
                        if budgets and len(budgets) == len(texts):
                            left_kwargs["duration_budgets"] = budgets[:mid]
                            right_kwargs["duration_budgets"] = budgets[mid:]
                        left_res = translate_with_deepseek(
                            texts[:mid], target_lang=target_lang, api_key=api_key,
                            prior_context=prior_context, model=dm, **left_kwargs
                        )
                        if left_res and len(left_res) == mid:
                            combined_prior = list(prior_context or [])
                            for s, t in zip(texts[:mid], left_res):
                                combined_prior.append({"source": s, "translated": t})
                            right_res = translate_with_deepseek(
                                texts[mid:], target_lang=target_lang, api_key=api_key,
                                prior_context=combined_prior[-4:], model=dm, **right_kwargs
                            )
                            if right_res and len(right_res) == len(texts) - mid:
                                return left_res + right_res
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
2. Dịch tự nhiên, cuốn hút, chuẩn văn phong video ngắn mạng xã hội.
3. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.
4. KHÔNG dùng dấu ba chấm (... hoặc …). Chỉ dùng dấu chấm (. ! ?) khi kết thúc câu hoàn chỉnh, TUYỆT ĐỐI KHÔNG chèn dấu chấm ở cuối câu lửng hoặc vế câu chưa hết ý; giữ nguyên số phần tử JSON.
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
        elif p == "deepseek":
            d_key = os.getenv("DEEPSEEK_API_KEY", "")
            if d_key:
                translated_texts = translate_with_deepseek(
                    texts, target_lang, d_key,
                    prior_context=prior_context,
                    **kwargs
                )
                
    # Fallback Tier: G4F Free
    if not translated_texts and enable_g4f:
        logger.info("Trying ChatGPT (G4F) API...")
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(translate_with_g4f, texts, target_lang)
            try:
                translated_texts = future.result(timeout=40)
            except concurrent.futures.TimeoutError:
                logger.warning("G4F phản hồi quá lâu (quá 40s), hủy để tránh treo bot.")
                translated_texts = None
            except Exception as e:
                logger.warning(f"Lỗi G4F: {e}")
                translated_texts = None
        
    if isinstance(translated_texts, list):
        translated_texts = [
            normalize_subtitle_text(item) if isinstance(item, str) else item
            for item in translated_texts
        ]
        translated_texts = clean_incomplete_segment_stops(translated_texts)
    translated_texts_valid = bool(
        translated_texts
        and len(translated_texts) == len(texts)
        and all(isinstance(item, str) and item.strip() for item in translated_texts)
    )
    if translated_texts_valid:
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
        idx = 0
        for segment in srt_segments:
            if not segment.content:
                continue
            segment.orig_content = segment.content
            segment.content = translated_texts[idx]
            idx += 1
        logger.info("LLM translation successful.")
        return srt_segments
    
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
