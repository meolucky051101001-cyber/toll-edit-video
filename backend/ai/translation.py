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
import requests
import base64
import re
from deep_translator import GoogleTranslator, MyMemoryTranslator
import logging

from .model_policy import current_model_policy, ordered_unique

try:
    from ..subtitle_text import normalize_subtitle_text
except ImportError:
    from subtitle_text import normalize_subtitle_text

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


def _validate_fallback_translation(source_text, translated_text, provider):
    if _is_translation_error_payload(translated_text):
        raise RuntimeError(
            "{} returned an error payload: {}".format(
                provider, str(translated_text or "")[:120]
            )
        )
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

def build_translation_prompt(texts, target_lang="vi", prior_context=None, with_vision=True, duration_budgets=None):
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
   KHÔNG dùng dấu ba chấm (... hoặc …), kể cả đầu/cuối đoạn bị ngắt. Chỉ đặt dấu chấm khi đã hết một câu hoàn chỉnh. Hệ thống sẽ chuyển phụ đề sang câu kế tiếp tại dấu kết câu; vẫn giữ đúng số phần tử JSON theo đầu vào.
"""
    if with_vision:
        prompt += "7. TRỰC QUAN: Hãy kết hợp các bức ảnh đính kèm từ video để chọn đại từ nhân xưng và danh từ chính xác tuyệt đối với ngữ cảnh.\n"
    prompt += "8. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
    if duration_budgets and len(duration_budgets) == len(texts):
        prompt += (
            "9. NGÂN SÁCH THỜI LƯỢNG cho từng phần tử, cùng thứ tự với mảng gốc:\n"
            + json.dumps(duration_budgets, ensure_ascii=False)
            + "\nseconds là số giây đọc; max_characters là giới hạn ký tự mong muốn (kể cả khoảng trắng). "
            "Hãy chọn câu dịch ngắn gọn, dễ đọc ngay từ lần đầu để vừa thời gian, "
            "nhưng giữ đủ ý chính, tên riêng, số lượng và phủ định. Không cắt cụt từ, "
            "không bỏ ý chỉ để đạt giới hạn; không trả về bảng ngân sách.\n"
        )
    prompt += "Dữ liệu:\n"
    if prior_context:
        prompt += "Ngữ cảnh nối tiếp từ batch trước (không dịch lại):\n"
        prompt += json.dumps(prior_context, ensure_ascii=False) + "\n"
    prompt += json.dumps(texts, ensure_ascii=False)
    return prompt

def extract_video_frames_base64(video_path, context_start_seconds=None, context_end_seconds=None, num_frames=5):
    if not video_path or not os.path.exists(video_path):
        return []
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if context_start_seconds is not None and context_end_seconds is not None and float(context_end_seconds) > float(context_start_seconds):
            start = max(0.0, float(context_start_seconds))
            span = float(context_end_seconds) - start
            positions = [("msec", (start + span * i / (num_frames + 1)) * 1000.0) for i in range(1, num_frames + 1)]
        elif total_frames > 0:
            step = max(total_frames // (num_frames + 1), 1)
            positions = [("frame", i * step) for i in range(1, num_frames + 1)]
        else:
            positions = []
            
        b64_list = []
        for position_type, position in positions:
            if position_type == "msec":
                cap.set(cv2.CAP_PROP_POS_MSEC, position)
            else:
                cap.set(cv2.CAP_PROP_POS_FRAMES, position)
            ret, frame = cap.read()
            if ret:
                _, buffer = cv2.imencode('.jpg', frame)
                b64_str = base64.b64encode(buffer).decode('utf-8')
                b64_list.append(b64_str)
        cap.release()
        return b64_list
    except Exception as img_e:
        logger.debug(f"Không thể trích xuất ảnh từ video: {img_e}")
        return []

def translate_with_gemini(
    texts,
    target_lang="vi",
    api_key="",
    video_path=None,
    context_start_seconds=None,
    context_end_seconds=None,
    prior_context=None,
    **kwargs
):
    api_key = api_key or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=True,
                                          duration_budgets=kwargs.get("duration_budgets"))
        parts = [{"text": prompt}]
        
        frames = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds)
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
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            payload = {"contents": [{"parts": parts}]}
            headers = {"Content-Type": "application/json"}
            try:
                logger.info(f"Đang gọi Google Gemini: {model}...")
                response = requests.post(url, json=payload, headers=headers, timeout=60)
                if response.status_code == 200:
                    logger.info(f"Gọi thành công Gemini {model}!")
                    break
                else:
                    logger.warning(f"Lỗi gọi {model} (HTTP {response.status_code})")
            except Exception as req_e:
                logger.warning(f"Lỗi kết nối {model}: {req_e}")
                
        if not response or response.status_code != 200:
            return None
            
        result = response.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            text = match.group(0)
            
        translated = json.loads(text)
        if len(translated) == len(texts):
            return translated
    except Exception as e:
        logger.warning(f"Lỗi dịch Gemini: {e}")
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
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=True,
                                          duration_budgets=kwargs.get("duration_budgets"))
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
                    match = re.search(r'\[.*\]', text, re.DOTALL)
                    if match:
                        text = match.group(0)
                    translated = json.loads(text)
                    if len(translated) == len(texts):
                        logger.info(f"Dịch thành công bằng OpenAI ChatGPT ({om})!")
                        return translated
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
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=False,
                                          duration_budgets=kwargs.get("duration_budgets"))
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
                    match = re.search(r'\[.*\]', text, re.DOTALL)
                    if match:
                        text = match.group(0)
                    translated = json.loads(text)
                    if len(translated) == len(texts):
                        logger.info(f"Dịch thành công bằng DeepSeek ({dm})!")
                        return translated
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
4. KHÔNG dùng dấu ba chấm (... hoặc …). Chỉ dùng dấu chấm khi kết thúc câu hoàn chỉnh; giữ nguyên số phần tử JSON.
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
    **kwargs
):
    logger.info("Translating subtitles...")
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

    return srt_segments
