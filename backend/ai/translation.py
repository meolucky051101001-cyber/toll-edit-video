import os
import sys
import io
from pathlib import Path

# Fix Windows console UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import json
from typing import Any, Callable, Dict, List, Optional, Tuple
import requests
import base64
import re
from deep_translator import GoogleTranslator
try:
    from deep_translator import MyMemoryTranslator
except ImportError:
    # Keep provider discovery deterministic. A late import could escape test or
    # runtime dependency isolation and unexpectedly make a network request.
    MyMemoryTranslator = None
import logging
import time
import hashlib
import threading
try:
    from ..v1_stage_metrics import stage
except Exception:
    try:
        from v1_stage_metrics import stage
    except Exception:
        def stage(*args, **kwargs):
            def decorator(func):
                return func
            return decorator

try:
    from .v1_gemini_dispatcher import call_gemini_api, DEFAULT_TRANSLATION_MODELS, DEFAULT_CONDENSATION_MODELS
except ImportError:
    from v1_gemini_dispatcher import call_gemini_api, DEFAULT_TRANSLATION_MODELS, DEFAULT_CONDENSATION_MODELS

logger = logging.getLogger(__name__)
_gemini_health_lock = threading.Lock()
_gemini_cooldown = {}
_gemini_last_good = {}


def _contains_cjk(text):
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))

def build_translation_prompt(texts, target_lang="vi", prior_context=None, with_vision=True):
    lang_name = "Tiếng Việt" if target_lang == "vi" else target_lang
    prompt = f"""Bạn là một chuyên gia dịch thuật nội dung mạng xã hội (Tiktok, Douyin).
Nhiệm vụ: Dịch mảng JSON chứa các câu phụ đề dưới đây sang {lang_name}.
Yêu cầu TỐI QUAN TRỌNG:
1. BẮT BUỘC giữ nguyên số lượng phần tử của mảng JSON. Mỗi câu gốc tương ứng đúng 1 câu dịch. Không tự ý gộp câu hay tách câu để đảm bảo khớp thời gian hiển thị (timing).
2. DỊCH CHUẨN XÁC NHƯNG HẤP DẪN: Ưu tiên dịch đúng nghĩa đen và bóng của câu chữ. Giữ văn phong tự nhiên, cuốn hút, có chút thiên hướng mạng xã hội để đăng video.
3. XỬ LÝ TỪ NGỮ VĂN HOA/THƠ CA: Các video Douyin thường dùng câu chữ hoa mỹ. Ví dụ '懒春秋' mang ý nghĩa 'thư thái, nhàn hạ' chứ KHÔNG PHẢI là 'lười biếng'. Hãy dịch thoát ý, sang trọng.
4. TUYỆT ĐỐI KHÔNG lạm dụng từ tiếng Anh. Ưu tiên tiếng Việt thuần túy.
5. KHỚP KHẨU HÌNH & THỜI LƯỢNG (LIP-SYNC): Văn bản dịch dùng để lồng tiếng (TTS), độ dài âm tiết của câu tiếng Việt PHẢI TƯƠNG ĐƯƠNG VỚI CÂU GỐC để khớp hoàn hảo khẩu hình miệng của nhân vật.
6. THUẬT NGỮ KIẾN TRÚC & ĐỜI SỐNG: '三合院' dịch là 'nhà tam hợp viện / nhà ba gian', '占地' dịch là 'diện tích đất', '大气' dịch là 'bề thế, sang trọng / đẳng cấp' (tuyệt đối không dịch thành 'dấu chân', 'khí quyển').
7. LỌC HOẶC VIỆT HÓA CÂU KÊU GỌI (CTA): Các câu kêu gọi Douyin/TikTok như '回复888', '关注我', '点赞' hãy dịch khéo thành lời kêu gọi tự nhiên ngắn gọn (ví dụ: 'để lại bình luận bên dưới nhé' hoặc 'liên hệ ngay nhé'), không dịch máy số hiệu thô thiển.
8. BẮT LỖI ĐỒNG ÂM ASR DO NHẬN DẠNG GIỌNG NÓI (WHISPER): Phụ đề tiếng Trung gốc được trích xuất bằng ASR nên thường xuất hiện các từ đồng âm/gần âm sai trong video review, handmade, đồ gia dụng. Hãy dùng ngữ cảnh sản phẩm để tự động sửa:
   - '天手章' hoặc '手张' -> hiểu đúng là '贴手帐' hoặc '手帐' (dán sổ tay / chơi sổ Bullet Journal / planner); tuyệt đối KHÔNG dịch thành 'chương tay' hay 'quả trứng'.
   - '怪蛋' -> hiểu đúng là '怪诞' (kỳ ảo, kỳ thú, độc lạ); KHÔNG dịch thành 'quả trứng quái'.
   - '风味感' trong ngữ cảnh đồ dùng/thủ công -> hiểu đúng là '氛围感' (cảm giác không gian chill / vibe nghệ thuật); KHÔNG dịch thành 'hương vị ẩm thực' hay 'phong vị'.
   - '苏打' khi nói về keo dán/băng keo -> hiểu đúng là '胶带' / '调色板贴纸' (băng dính Washi Tape / sticker bảng màu); KHÔNG dịch thành nước sô-đa.
   - '叶芝麦' -> hiểu đúng là '叶之脉' (gân của chiếc lá); dịch thoát ý tự nhiên.
   - '可思线' -> hiểu đúng là '可撕线' (đường răng cưa dễ xé).
9. NGỮ CẢNH NỐI TIẾP: Vì phụ đề thường bị ngắt giữa chừng, hãy đọc cả đoạn để dịch sao cho ý nối liền mạch trơn tru.
"""
    if with_vision:
        prompt += "10. TRỰC QUAN: Hãy kết hợp các bức ảnh đính kèm từ video để chọn đại từ nhân xưng và danh từ chính xác tuyệt đối với ngữ cảnh.\n"
    prompt += "11. THUẬT NGỮ CÔNG NGHỆ & PHẦN CỨNG: Các tên card màn hình, CPU, GPU, đơn vị dung lượng (như RTX 4070, RX 6600 XT, Core i5, Ryzen 7, 2GB, 16GB, VRAM, FPS, CS2): BẮT BUỘC giữ nguyên tên chuẩn quốc tế, định dạng khoảng trắng rõ ràng giữa chữ và số (ví dụ: 'RTX 4070' thay vì 'RTX4070', '8 GB' hoặc '8GB'). Tuyệt đối KHÔNG dịch tên riêng hay mã hiệu kỹ thuật sang tiếng Việt.\n"
    prompt += "12. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
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
    if not api_key or not texts:
        return None
    if len(texts) > 40:
        chunk_size = 40
        all_translated = []
        for i in range(0, len(texts), chunk_size):
            chunk_texts = texts[i:i + chunk_size]
            chunk_kwargs = dict(kwargs)
            chunk_kwargs["job_id"] = f"{kwargs.get('job_id', 'translate')}_p{i // chunk_size + 1}"
            chunk_res = translate_with_gemini(
                chunk_texts, target_lang=target_lang, api_key=api_key,
                video_path=video_path, context_start_seconds=context_start_seconds,
                context_end_seconds=context_end_seconds, prior_context=prior_context,
                **chunk_kwargs
            )
            if not chunk_res or len(chunk_res) != len(chunk_texts):
                logger.warning("Gemini chunk %d..%d thất bại, hủy toàn bộ batch để fallback", i, i + len(chunk_texts))
                return None
            all_translated.extend(chunk_res)
        return all_translated

    try:
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=True)
        parts = [{"text": prompt}]
        
        frames = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds)
        for b64 in frames:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64
                }
            })
        preferred_gemini = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip()
        candidate_models = [
            preferred_gemini,
            "gemini-3.7-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.8-flash",
        ]
        models_to_try = []
        for m in candidate_models:
            if m and m not in models_to_try:
                models_to_try.append(m)
        response = None
        account = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        try:
            from .v1_translation_cache import cache_key, read_cache, write_cache
            cache_k = cache_key(parts, candidate_models, account)
            cached = read_cache(cache_k, len(texts), target_lang)
            if cached:
                m_cache = cached.get("model", "gemini-cached")
                logger.info("Sử dụng bản dịch từ cache (model=%s)", m_cache)
                with _gemini_health_lock:
                    _gemini_last_good[account] = cached["model"]
                try:
                    import job_tracker
                    job_tracker.record_translation_model(m_cache)
                except Exception:
                    pass
                return cached["texts"]
        except ImportError:
            cache_k = None
            write_cache = None

        from .v1_gemini_dispatcher import call_gemini_api
        payload = {"contents": [{"parts": parts}]}
        res_data, used_model = call_gemini_api(
            payload=payload,
            purpose="translation",
            models=candidate_models,
            api_key=api_key,
            job_id=str(kwargs.get("job_id", "translate"))
        )
        parts_out = res_data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts_out if not p.get("thought")).strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        translated = json.loads(match.group(0) if match else raw)
        if not isinstance(translated, list) or len(translated) != len(texts) or not all(
                isinstance(t, str) and t.strip() for t in translated):
            raise ValueError("Invalid translation array")
        try:
            from mojibake_repair import repair_vietnamese_mojibake
            translated = [repair_vietnamese_mojibake(t) for t in translated]
        except Exception:
            pass
        if write_cache:
            write_cache(cache_k, translated, used_model)
        try:
            import job_tracker
            job_tracker.record_translation_model(used_model)
        except Exception:
            pass
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
    model="gpt-4o",
    **kwargs
):
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=True)
        messages_content = [{"type": "text", "text": prompt}]
        
        frames = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds)
        for b64 in frames:
            messages_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
            
        openai_models = [model, "gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest"]
        seen_models = []
        for m in openai_models:
            if m not in seen_models: seen_models.append(m)
            
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
                        try:
                            import job_tracker
                            job_tracker.record_translation_model(f"OpenAI {om}")
                        except Exception:
                            pass
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
    model="deepseek-v4",
    **kwargs
):
    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    try:
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=False)
        models_to_try = [model, "deepseek-v4", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"]
        seen = []
        for m in models_to_try:
            if m not in seen: seen.append(m)
            
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
                        try:
                            import job_tracker
                            job_tracker.record_translation_model(f"DeepSeek {dm}")
                        except Exception:
                            pass
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

@stage("translation")
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
        logger.info("Trying ChatGPT (G4F) API via bounded fallback...")
        try:
            from ai.v1_bounded_fallback import run_fallback
            translated_texts = run_fallback(translate_with_g4f, texts, target_lang, timeout=40)
        except TimeoutError:
            logger.warning("G4F phản hồi quá lâu (quá 40s), hủy để tránh treo bot.")
            translated_texts = None
        except Exception as e:
            logger.warning(f"Lỗi G4F: {e}")
            translated_texts = None
        
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
            if target_lang.lower().startswith("vi") and _contains_cjk(translated)
        ]
        if unchanged_cjk:
            logger.warning(
                "AI translation left CJK source unchanged at positions: %s",
                ", ".join(str(position) for position in unchanged_cjk),
            )
            translated_texts_valid = False

    if translated_texts_valid:
        try:
            from mojibake_repair import repair_vietnamese_mojibake
            translated_texts = [repair_vietnamese_mojibake(t) for t in translated_texts]
        except Exception:
            pass
        idx = 0
        for segment in srt_segments:
            if not segment.content:
                continue
            segment.orig_content = segment.content
            segment.content = translated_texts[idx]
            idx += 1
        logger.info("LLM translation successful.")
        try:
            import job_tracker
            t_models = job_tracker.get_status().get("translation_models", [])
            m_name = ", ".join(t_models) if t_models else "AI"
            logger.info(f"Hoàn thành dịch {len(texts)} đoạn phụ đề bằng model: {m_name}")
        except Exception:
            pass
        return srt_segments
    
    logger.info("Falling back to Google Translate...")
    failed_segments = []
    error_keywords = [
        "error 500", "server error", "invalid source language",
        "langpair", "almost all languages supported", "mymemory",
        "query length limit", "daily limit reached", "too many requests"
    ]
    for segment in srt_segments:
        if not segment.content:
            continue

        segment.orig_content = segment.content
        # Không chứa ký tự CJK tiếng Trung (như số, tiếng Anh, tên card 2GB, RTX3070, CS2) -> Giữ nguyên gốc!
        if not _contains_cjk(segment.content):
            continue

        try:
            src_lang = 'zh-CN'
            try:
                translator = GoogleTranslator(source=src_lang, target=target_lang)
                translated_text = translator.translate(segment.content)
            except Exception:
                translated_text = None

            if (
                not translated_text
                or any(k in str(translated_text).lower() for k in error_keywords)
                or str(translated_text).startswith("Error")
                or (target_lang.lower().startswith("vi") and _contains_cjk(translated_text))
            ):
                try:
                    if MyMemoryTranslator is not None:
                        mm_target = "vi-VN" if target_lang.lower().startswith("vi") else target_lang
                        translated_text = MyMemoryTranslator(source="zh-CN", target=mm_target).translate(segment.content)
                except Exception:
                    pass

            if not isinstance(translated_text, str) or not translated_text.strip():
                raise RuntimeError("Google Translate returned an empty result")
            if any(k in translated_text.lower() for k in error_keywords) or translated_text.startswith("Error"):
                raise RuntimeError("Google Translate returned an error payload: {}".format(translated_text[:120]))
            if target_lang.lower().startswith("vi") and _contains_cjk(translated_text):
                raise RuntimeError("Chinese source text remained untranslated")

        except Exception as e:
            logger.warning(f"Lỗi dịch thuật cho đoạn '{segment.content}': {e}")
            failed_segments.append(int(getattr(segment, "index", 0)))
            translated_text = segment.orig_content

        segment.content = translated_text
        
    if failed_segments and strict:
        raise RuntimeError(
            "Translation failed for segment indexes: {}".format(
                ", ".join(str(index) for index in failed_segments)
            )
        )

    try:
        import job_tracker
        t_models = job_tracker.get_status().get("translation_models", [])
        if not t_models:
            job_tracker.record_translation_model("Google Translate (fallback)")
            logger.info("Hoàn tất dịch phụ đề bằng model: Google Translate (fallback)")
    except Exception:
        pass

    return srt_segments


def validate_condensed_text(orig_text: str, shortened: str, target_words: int) -> bool:
    """
    Kiểm tra chặt chẽ câu đã rút gọn theo Codex Plan:
    1. Không rỗng, không chứa markdown, code fence, hoặc lời giải thích.
    2. Không chứa ký tự CJK tiếng Trung.
    3. Không dài hơn câu gốc.
    4. Bảo toàn các số quan trọng và từ phủ định (không, chưa, chẳng, đừng).
    """
    if not isinstance(shortened, str) or not shortened.strip():
        return False
    shortened = shortened.strip()
    if "```" in shortened or "{" in shortened or "}" in shortened or "\n" in shortened:
        return False
    if any("\u4e00" <= c <= "\u9fff" for c in shortened):
        return False

    orig_words = orig_text.strip().split()
    short_words = shortened.split()
    if len(short_words) > len(orig_words):
        return False

    orig_nums = set(re.findall(r'\b\d+\b', orig_text))
    if orig_nums:
        short_nums = set(re.findall(r'\b\d+\b', shortened))
        if not orig_nums.issubset(short_nums):
            return False

    negations = ["không", "chưa", "chẳng", "đừng"]
    for neg in negations:
        if f" {neg} " in f" {orig_text.lower()} " and f" {neg} " not in f" {shortened.lower()} ":
            return False

    return True


def condense_vietnamese_subtitles_batch(
    items: list,
    api_key: str = "",
    deadline: Optional[float] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
    job_id: str = "condense"
) -> dict:
    """
    Rút gọn các câu thoại tiếng Việt quá dài theo ngữ nghĩa bằng Gemini theo Kế hoạch Codex:
    1. Kiểm tra cache trước.
    2. Gom tất cả câu chưa cache vào đúng MỘT lượt gọi qua v1_gemini_dispatcher.
    3. Ưu tiên model nhẹ, nhanh: gemini-3.5-flash-lite -> gemini-flash-lite-latest -> gemini-3.5-flash -> gemini-3.7-flash.
    4. Kiểm tra cấu trúc và ngữ nghĩa chặt chẽ (giữ số, phủ định, không rỗng, không CJK).
    5. Chỉ chấp nhận các câu hợp lệ, ghi cache.
    """
    if not items:
        return {}

    from .v1_translation_cache import read_condense_cache, write_condense_cache
    from mojibake_repair import repair_vietnamese_mojibake

    result = {}
    uncached_items = []

    for it in items:
        idx = it["index"]
        text = it["text"].strip()
        sec = float(it.get("target_seconds", 2.0))
        tw = int(it.get("target_words") or max(2, int((sec - 0.05) / 0.28)))
        cached_val = read_condense_cache(text, sec, tw)
        if cached_val:
            result[idx] = cached_val
        else:
            uncached_items.append({
                "index": idx,
                "text": text,
                "target_seconds": sec,
                "target_words": tw,
            })

    if not uncached_items:
        logger.info(f"[CONDENSE] Tất cả {len(items)} câu đều có sẵn trong cache (100% Cache HIT).")
        return result

    prompt = (
        "Bạn là chuyên gia biên tập phụ đề video ngắn chuyên nghiệp.\n"
        "Các câu thoại tiếng Việt sau đây đang đọc quá dài so với thời lượng video gốc.\n"
        "Nhiệm vụ: Viết lại/rút gọn từng câu sao cho thật ngắn gọn, súc tích (cô đọng nội dung, bỏ từ đệm thừa, "
        "giữ trọn vẹn ý chính và tự nhiên, khống chế số lượng từ tối đa theo yêu cầu để người đọc và AI đọc trọn vẹn mà không bị nhanh).\n"
        "Yêu cầu định dạng: Trả về duy nhất một đối tượng JSON ánh xạ ID dạng chuỗi sang câu đã rút gọn, ví dụ:\n"
        '{"1": "câu 1 ngắn gọn", "2": "câu 2 ngắn gọn"}\n'
        "Tuyệt đối không thêm lời dẫn giải hay bất kỳ ký tự nào ngoài JSON.\n\n"
        "Danh sách câu cần rút gọn:\n"
    )
    for it in uncached_items:
        prompt += f"- ID {it['index']} (mục tiêu: ~{it['target_seconds']:.2f}s, tối đa {it['target_words']} từ): \"{it['text']}\"\n"

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        data, used_model = call_gemini_api(
            payload=payload,
            purpose="condensation",
            models=DEFAULT_CONDENSATION_MODELS,
            overall_deadline=deadline,
            stop_checker=stop_checker,
            job_id=job_id,
            api_key=api_key,
            timeout_per_request=20.0
        )
        parts_out = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts_out if not p.get("thought")).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            valid_count = 0
            for it in uncached_items:
                idx = it["index"]
                shortened = parsed.get(str(idx)) or parsed.get(idx)
                if shortened and isinstance(shortened, str):
                    shortened = repair_vietnamese_mojibake(shortened.strip())
                    if validate_condensed_text(it["text"], shortened, it["target_words"]):
                        result[idx] = shortened
                        write_condense_cache(it["text"], it["target_seconds"], it["target_words"], shortened, used_model)
                        valid_count += 1
                    else:
                        logger.warning(f"[CONDENSE] Câu #{idx} không vượt qua kiểm định (giữ nguyên): '{shortened}'")
            logger.info(f"[CONDENSE] Gemini {used_model} rút gọn thành công {valid_count}/{len(uncached_items)} câu chưa cache!")
    except Exception as e:
        logger.warning(f"[CONDENSE] Rút gọn câu qua Gemini không hoàn tất: {e}")

    return result




