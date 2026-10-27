import os
import sys
from pathlib import Path

# Fix Windows console UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import json
from typing import Callable, Dict, List, Optional
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

try:
    from .transcription import save_srt
except Exception:
    save_srt = None
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

from .model_policy import current_model_policy, ordered_unique

try:
    from ..subtitle_text import clean_incomplete_segment_stops, normalize_subtitle_text
except ImportError:
    from subtitle_text import clean_incomplete_segment_stops, normalize_subtitle_text

logger = logging.getLogger(__name__)
_gemini_health_lock = threading.Lock()
_gemini_cooldown = {}
_gemini_last_good = {}


def _contains_cjk(text):
    return any(("\u4e00" <= char <= "\u9fff") or ("\u3400" <= char <= "\u4dbf") for char in str(text or ""))


def _parse_json_array(text):
    """Parse JSON array of strings from LLM text with resilience against markdown formatting, trailing commas, and Python syntax."""
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
    # 1. Try standard JSON
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # 2. Try removing trailing commas before closing bracket (e.g. '["a", "b", ]')
    try:
        cleaned_comma = re.sub(r",\s*\]", "]", cleaned)
        data = json.loads(cleaned_comma)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # 3. Try ast.literal_eval for single-quoted Python list syntax (e.g. "['Câu 1', 'Câu 2']")
    import ast
    try:
        data = ast.literal_eval(cleaned)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return None


def _subdivide_and_retry(translate_func, texts, **kwargs):
    """Split batch in half (divide-and-conquer) when LLM returns mismatched item count."""
    if not texts or len(texts) < 2:
        return None
    mid = len(texts) // 2
    left_texts = texts[:mid]
    right_texts = texts[mid:]

    # Left kwargs: preserve context_start_seconds, drop context_end_seconds
    left_kwargs = dict(kwargs)
    left_kwargs.pop("context_end_seconds", None)
    if left_kwargs.get("duration_budgets") is not None:
        left_kwargs["duration_budgets"] = left_kwargs["duration_budgets"][:mid]

    left_res = translate_func(left_texts, **left_kwargs)
    if not left_res or len(left_res) != len(left_texts):
        return None

    # Right kwargs: preserve context_end_seconds, drop context_start_seconds
    right_kwargs = dict(kwargs)
    right_kwargs.pop("context_start_seconds", None)
    if right_kwargs.get("duration_budgets") is not None:
        right_kwargs["duration_budgets"] = right_kwargs["duration_budgets"][mid:]
    if "prior_context" in right_kwargs:
        right_kwargs["prior_context"] = left_res[-2:] if len(left_res) >= 2 else left_res

    right_res = translate_func(right_texts, **right_kwargs)
    if not right_res or len(right_res) != len(right_texts):
        return None

    return list(left_res) + list(right_res)


BUILTIN_GLOSSARY_PRESETS: Dict[str, Dict[str, str]] = {
    "bjd_handmade": {
        "bjd": "BJD",
        "BJD": "BJD",
        "手帐": "sổ tay Bullet Journal",
        "贴手帐": "trang trí sổ tay",
        "胶带": "băng dính Washi Tape",
        "流麻": "quicksand shaker",
        "咕卡": "trang trí thẻ Guka",
    },
    "architecture_sanheyuan": {
        "三合院": "nhà tam hợp viện",
        "四合院": "tứ hợp viện",
        "占地": "diện tích đất",
        "开间": "chiều ngang mặt tiền",
        "进深": "chiều sâu ngôi nhà",
        "堂屋": "gian chính giữa",
    },
    "football_sports": {
        "梅西": "Messi",
        "巴萨": "Barça",
        "阿根廷": "Argentina",
        "麦卡利斯特": "Mac Allister",
        "迪马利亚": "Di María",
        "C罗": "Cristiano Ronaldo",
    },
    "tech_hardware": {
        "显卡": "card đồ họa",
        "主板": "bo mạch chủ",
        "散热器": "tản nhiệt",
        "固态硬盘": "ổ cứng SSD",
        "帧率": "mức FPS",
    },
}


def _glossary_paths(workspace: Optional[str] = None) -> List[Path]:
    base = Path(workspace) if workspace else Path(os.getenv("AUTODUB_WORKSPACE", str(Path(__file__).resolve().parents[2] / "workspace")))
    return [
        base / "bot_system" / "control" / "custom_glossary.json",
        base / "control" / "custom_glossary.json",
    ]


def load_custom_glossary(workspace: Optional[str] = None) -> Dict[str, str]:
    """Load user-configured custom glossary (Chinese -> Vietnamese) from workspace control directory."""
    for p in _glossary_paths(workspace):
        try:
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    entries = raw.get("entries", raw) if "entries" in raw and isinstance(raw.get("entries"), dict) else raw
                    return {
                        str(k).strip(): str(v).strip()
                        for k, v in entries.items()
                        if str(k).strip() and str(v).strip() and not str(k).startswith("_")
                    }
        except Exception:
            pass
    return {}


def save_custom_glossary(
    entries: Dict[str, str],
    preset: Optional[str] = None,
    merge: bool = True,
    workspace: Optional[str] = None,
) -> Dict[str, str]:
    """Save custom glossary atomically across workspace control directories."""
    current = load_custom_glossary(workspace) if merge else {}
    if preset and preset in BUILTIN_GLOSSARY_PRESETS:
        current.update(BUILTIN_GLOSSARY_PRESETS[preset])
    if entries and isinstance(entries, dict):
        for k, v in entries.items():
            k_clean = str(k).strip()
            v_clean = str(v).strip()
            if k_clean:
                if v_clean:
                    current[k_clean] = v_clean
                else:
                    current.pop(k_clean, None)
    payload = json.dumps({"entries": current, "updated_at": time.time()}, ensure_ascii=False, indent=2)
    for p in _glossary_paths(workspace):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:
            logger.debug("Could not write custom glossary %s: %s", p, exc)
    return current


def build_translation_prompt(texts, target_lang="vi", prior_context=None, with_vision=True, script_mode="default", glossary=None):
    lang_name = "Tiếng Việt" if target_lang == "vi" else target_lang
    is_humorous = str(script_mode or "default").lower() in ("humorous", "hai_huoc", "haihuoc", "comedy")

    if is_humorous:
        role_desc = "Bạn là một biên kịch chuyên sáng tạo nội dung video ngắn hài hước, review tấu hài triệu view trên TikTok và Douyin."
        style_rule = (
            "2. PHONG CÁCH HÀI HƯỚC, TẤU HÀI DÍ DỎM: Dịch thoát ý hóm hỉnh, tếu táo, lầy lội một cách duyên dáng ('hài hài buồn cười một chút'). "
            "Sử dụng khéo léo các khẩu ngữ, thán từ và tiếng lóng mạng xã hội văn minh của giới trẻ Việt Nam "
            "(ví dụ: 'ối giồi ôi', 'chúa tể', 'phen này toang', 'đỉnh nóc kịch trần', 'ảo ma', 'cười xỉu', 'bất ngờ chưa bà già', 'cứu tui', 'khổ chủ', 'đồng chí'...). "
            "Cách xưng hô gần gũi, hài hước (tui, mị, anh em, các bác, khổ thân...). "
            "LƯU Ý QUAN TRỌNG: Hài hước giải trí văn minh, TUYỆT ĐỐI KHÔNG dùng từ ngữ thô tục hay phản cảm. Giữ đúng các số liệu và ý chính của sản phẩm."
        )
        lipsync_rule = (
            "5. KHỚP KHẨU HÌNH & THỜI LƯỢNG (LIP-SYNC): Văn bản dịch dùng để lồng tiếng (TTS). "
            "Dù là phong cách hài hước nhưng độ dài âm tiết của câu tiếng Việt PHẢI TƯƠNG ĐƯƠNG VỚI CÂU GỐC, KHÔNG ĐƯỢC kéo dài dòng lê thê "
            "để đảm bảo AI lồng tiếng vừa nhịp thời lượng video và khớp khẩu hình nhân vật."
        )
    else:
        role_desc = "Bạn là một chuyên gia dịch thuật nội dung mạng xã hội (Tiktok, Douyin)."
        style_rule = (
            "2. DỊCH CHUẨN XÁC NHƯNG HẤP DẪN: Ưu tiên dịch đúng nghĩa đen và bóng của câu chữ. "
            "Giữ văn phong tự nhiên, cuốn hút, có chút thiên hướng mạng xã hội để đăng video."
        )
        lipsync_rule = (
            "5. KHỚP KHẨU HÌNH & THỜI LƯỢNG (LIP-SYNC): Văn bản dịch dùng để lồng tiếng (TTS), "
            "độ dài âm tiết của câu tiếng Việt PHẢI TƯƠNG ĐƯƠNG VỚI CÂU GỐC để khớp hoàn hảo khẩu hình miệng của nhân vật."
        )

    prompt = f"""{role_desc}
Nhiệm vụ: Dịch mảng JSON chứa các câu phụ đề dưới đây sang {lang_name}.
Yêu cầu TỐI QUAN TRỌNG:
1. BẮT BUỘC giữ nguyên số lượng phần tử của mảng JSON. Mỗi câu gốc tương ứng đúng 1 câu dịch. Không tự ý gộp câu hay tách câu để đảm bảo khớp thời gian hiển thị (timing).
{style_rule}
3. XỬ LÝ TỪ NGỮ VĂN HOA/THƠ CA: Các video Douyin thường dùng câu chữ hoa mỹ. Ví dụ '懒春秋' mang ý nghĩa 'thư thái, nhàn hạ' chứ KHÔNG PHẢI là 'lười biếng'. Hãy dịch thoát ý, sang trọng.
4. TUYỆT ĐỐI KHÔNG lạm dụng từ tiếng Anh. Ưu tiên tiếng Việt thuần túy.
{lipsync_rule}
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
    active_glossary = load_custom_glossary()
    if glossary and isinstance(glossary, dict):
        active_glossary.update({str(k).strip(): str(v).strip() for k, v in glossary.items() if str(k).strip() and str(v).strip()})
    if active_glossary and texts:
        matched_glossary = {
            k: v for k, v in active_glossary.items()
            if any(k in str(t) for t in texts)
        }
        if matched_glossary:
            glossary_str = ", ".join(f"'{k}' -> '{v}'" for k, v in matched_glossary.items())
            prompt += f"12. TỪ ĐIỂN THUẬT NGỮ BẮT BUỘC (CUSTOM GLOSSARY): Khi gặp các từ gốc sau, BẮT BUỘC dịch chính xác theo quy ước: {glossary_str}.\n"
            prompt += "13. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
        else:
            prompt += "12. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
    else:
        prompt += "12. CHỈ trả về mảng JSON chứa các chuỗi dịch, không giải thích, không markdown.\n"
    prompt += "Dữ liệu:\n"
    if prior_context:
        prompt += "Ngữ cảnh nối tiếp từ batch trước (để duy trì xưng hô và mạch truyện, KHÔNG dịch lại):\n"
        prompt += json.dumps(list(prior_context)[-4:], ensure_ascii=False) + "\n"
    prompt += json.dumps(texts, ensure_ascii=False)
    return prompt

def extract_video_frames_base64(video_path, context_start_seconds=None, context_end_seconds=None, num_frames=3):
    if not video_path or not os.path.exists(video_path):
        return []
    # 1. Ultra-fast extraction via ffmpeg fast seek (scaled to 720p, ~1.5s total)
    try:
        import subprocess
        dur = None
        if context_start_seconds is not None and context_end_seconds is not None and float(context_end_seconds) > float(context_start_seconds):
            start = max(0.0, float(context_start_seconds))
            span = float(context_end_seconds) - start
            timestamps = [start + span * i / (num_frames + 1) for i in range(1, num_frames + 1)]
        else:
            probe_cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path
            ]
            try:
                p_dur = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
                if p_dur.returncode == 0 and p_dur.stdout.strip():
                    dur = float(p_dur.stdout.strip().decode("utf-8", "ignore"))
            except Exception:
                pass
            if dur and dur > 0:
                timestamps = [dur * i / (num_frames + 1) for i in range(1, num_frames + 1)]
            else:
                timestamps = [1.0, 5.0, 10.0, 20.0, 30.0][:num_frames]

        b64_list = []
        for ts in timestamps:
            cmd = [
                "ffmpeg", "-ss", f"{ts:.2f}", "-noaccurate_seek", "-i", video_path,
                "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"
            ]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
            if p.returncode == 0 and p.stdout:
                b64_list.append(base64.b64encode(p.stdout).decode("utf-8"))
        if b64_list:
            return b64_list
    except Exception as ffmpeg_e:
        logger.debug(f"FFmpeg frame extraction fallback: {ffmpeg_e}")

    # 2. Encode the frames returned by the bounded sampler.
    try:
        import cv2
        try:
            from ..video_sampling import sample_video_frames
        except ImportError:
            from video_sampling import sample_video_frames
        frames = sample_video_frames(video_path, num_frames,
                                     context_start_seconds, context_end_seconds)
        b64_list = []
        t0 = time.monotonic()
        for frame in frames:
            if time.monotonic() - t0 > 12.0:
                break
            if frame is not None and frame.size:
                h, w = frame.shape[:2]
                if max(h, w) > 720:
                    scale = 720.0 / max(h, w)
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                encoded, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if encoded:
                    b64_list.append(base64.b64encode(buffer).decode('utf-8'))
        return b64_list
    except Exception as img_e:
        logger.debug(f"Không thể trích xuất ảnh từ video: {img_e}")
        return []

_gemini_unhealthy_until = 0.0
_gemini_transient_failures = 0


def is_gemini_available() -> bool:
    return time.time() >= _gemini_unhealthy_until


def mark_gemini_unhealthy(cooldown_seconds: float = 30.0) -> None:
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
    api_key = api_key or os.getenv("GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEYS", "")
    if not api_key or not texts:
        return None
    if kwargs.get("stop_checker") is None:
        import shared_state
        kwargs["stop_checker"] = lambda: bool(shared_state.stop_requested)

    # Decode vision frames ONCE before setting overall deadline to preserve full API budget
    if video_path and "_vision_frames" not in kwargs:
        kwargs["_vision_frames"] = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds)

    # One shared budget for all chunks. Cache hits do not consume API quota.
    import time
    kwargs.setdefault("overall_deadline", time.monotonic() + min(1800.0, 90.0 * max(1, (len(texts) + 39) // 40)))

    if len(texts) > 40:
        chunk_size = 40
        all_translated = []
        for i in range(0, len(texts), chunk_size):
            chunk_texts = texts[i:i + chunk_size]
            chunk_kwargs = dict(kwargs)
            chunk_kwargs["job_id"] = f"{kwargs.get('job_id', 'translate')}_p{i // chunk_size + 1}"
            rolling_context = all_translated[-4:] if all_translated else prior_context
            chunk_res = translate_with_gemini(
                chunk_texts, target_lang=target_lang, api_key=api_key,
                video_path=video_path, context_start_seconds=context_start_seconds,
                context_end_seconds=context_end_seconds, prior_context=rolling_context,
                **chunk_kwargs
            )
            if not chunk_res or len(chunk_res) != len(chunk_texts):
                logger.warning("Gemini chunk %d..%d không thành công; giữ cache những chunk hợp lệ, không dùng nguyên văn thay bản dịch", i, i + len(chunk_texts))
                return None
            all_translated.extend(chunk_res)
        return all_translated

    try:
        script_mode = kwargs.get("script_mode") or os.getenv("SCRIPT_MODE", "default")
        prompt = build_translation_prompt(
            texts,
            target_lang,
            prior_context,
            with_vision=True,
            script_mode=script_mode,
            glossary=kwargs.get("glossary"),
        )
        parts = [{"text": prompt}]
        
        frames = kwargs.get("_vision_frames")
        if frames is None:
            frames = extract_video_frames_base64(video_path, context_start_seconds, context_end_seconds)
        for b64 in frames:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64
                }
            })
        preferred_gemini = os.getenv("GEMINI_MODEL", "").strip()
        candidate_models = []
        if preferred_gemini:
            candidate_models.append(preferred_gemini)
        for m in DEFAULT_TRANSLATION_MODELS:
            if m not in candidate_models:
                candidate_models.append(m)
        models_to_try = list(candidate_models)
        response = None
        account = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        try:
            from .v1_translation_cache import cache_key, read_cache, write_cache
            cache_k = cache_key(parts, candidate_models, account, script_mode=script_mode)
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
        from .v1_gemini_response import translation_response
        payload = {"contents": [{"parts": parts}]}
        res_data, used_model = call_gemini_api(
            payload=payload,
            purpose="translation",
            models=candidate_models,
            api_key=api_key,
            job_id=str(kwargs.get("job_id", "translate")),
            overall_deadline=kwargs["overall_deadline"],
            stop_checker=kwargs["stop_checker"],
            timeout_per_request=35.0 if frames else 20.0,
            response_validator=lambda data: translation_response(data, len(texts), target_lang),
        )
        translated = translation_response(res_data, len(texts), target_lang)
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
        from .v1_gemini_dispatcher import GeminiCancelledError, mask_secret
        if isinstance(e, GeminiCancelledError):
            raise
        # Preserve the cause if every enabled provider fails; do not lose retry
        # classification behind a generic "translation failed" message.
        kwargs.get("failure_causes", []).append(e)
        mark_gemini_unhealthy(15.0)
        logger.warning("Lỗi dịch Gemini: %s", mask_secret(str(e)))
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
        script_mode = kwargs.get("script_mode") or os.getenv("SCRIPT_MODE", "default")
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=True, script_mode=script_mode)
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
                        try:
                            import job_tracker
                            job_tracker.record_translation_model(f"OpenAI {om}")
                        except Exception:
                            pass
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
        script_mode = kwargs.get("script_mode") or os.getenv("SCRIPT_MODE", "default")
        prompt = build_translation_prompt(texts, target_lang, prior_context, with_vision=False, script_mode=script_mode)
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
                    translated = _parse_json_array(text)
                    if isinstance(translated, list) and len(translated) == len(texts):
                        logger.info(f"Dịch thành công bằng DeepSeek ({dm})!")
                        try:
                            import job_tracker
                            job_tracker.record_translation_model(f"DeepSeek {dm}")
                        except Exception:
                            pass
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

def translate_with_g4f(texts, target_lang="vi", script_mode="default"):
    try:
        import g4f
        lang_name = "Tiếng Việt" if target_lang == "vi" else target_lang
        prompt = build_translation_prompt(texts, target_lang, with_vision=False, script_mode=script_mode)
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

def ensure_speech_pauses_and_entity(
    source_texts,
    translated_texts,
    entity_map=None,
    prior_context=None,
    glossary=None,
):
    """Normalize translated texts with proper capitalization, pauses, and entity names.

    Entity normalization is strictly context- and glossary-driven:
    - User glossary takes precedence, followed by job entity_map.
    - An entity replacement is applied ONLY when the source keyword actually appears in the corresponding source text.
    - No ordinary terms (like assassin, sát thủ, thích khách) are converted to proper names without explicit source evidence.
    - Terminal periods are added ONLY when the source text actually ends with sentence-terminal punctuation.
    - Commas are inserted ONLY when source punctuation indicates a clause break, never based arbitrarily on word count.
    """
    if not translated_texts or len(source_texts) != len(translated_texts):
        return translated_texts

    active_entity_map = {}
    custom_gloss = load_custom_glossary()
    if custom_gloss:
        active_entity_map.update(custom_gloss)
    if entity_map and isinstance(entity_map, dict):
        active_entity_map.update(entity_map)
    if glossary and isinstance(glossary, dict):
        active_entity_map.update(glossary)

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

    out = []
    for src, tr in zip(source_texts, translated_texts):
        clean = normalize_subtitle_text(str(tr or "")).strip()
        if not clean:
            out.append(clean)
            continue

        src_str = str(src or "")

        # 1. Entity normalization with provenance: only apply if the source entity is in the source segment
        if active_entity_map:
            for src_key, target_val in active_entity_map.items():
                if src_key and target_val and src_key in src_str:
                    # Standardize casing of target term in translation if present
                    tgt_pat = re.compile(rf"\b{re.escape(str(target_val))}\b", re.IGNORECASE)
                    if tgt_pat.search(clean):
                        clean = tgt_pat.sub(str(target_val), clean)
                    # Also replace untranslated source term if left in translation
                    src_pat = re.compile(rf"\b{re.escape(str(src_key))}\b", re.IGNORECASE)
                    if src_pat.search(clean):
                        clean = src_pat.sub(str(target_val), clean)

        # 2. Capitalization of first character
        first_char = clean[0]
        if first_char.isalpha() and not first_char.isupper():
            clean = first_char.upper() + clean[1:]

        # 3. Ellipses normalization: no '...' in final subtitles
        clean = re.sub(r"\s*[\.]{2,}\s*", ", ", clean)
        clean = re.sub(r"\s*…\s*", ", ", clean)
        clean = normalize_subtitle_text(clean)

        # 4. Punctuation pauses: strictly driven by source pause, NEVER by word length alone
        src_has_pause = any(p in src_str for p in ("，", ",", "；", ";", "、"))
        if src_has_pause and "," not in clean:
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

        # 5. Terminal punctuation: strictly driven by source sentence ending
        src_trimmed = src_str.strip()
        src_ends_terminal = any(src_trimmed.endswith(p) for p in ("。", "！", "？", ".", "!", "?"))
        if src_ends_terminal:
            if not clean.endswith((".", "!", "?")):
                if src_trimmed.endswith(("？", "?")):
                    clean += "?"
                elif src_trimmed.endswith(("！", "!")):
                    clean += "!"
                else:
                    clean += "."
        else:
            # Source does NOT end with terminal punctuation (an ongoing or split clause).
            # Ensure no rogue terminal punctuation was placed in the middle of a continuous sentence.
            while clean.endswith((".", "!", "?")):
                # If source ends with a comma or pause, retain comma instead of period
                if any(src_trimmed.endswith(p) for p in ("，", ",", "；", ";", "、")):
                    clean = clean[:-1].rstrip() + ","
                    break
                else:
                    clean = clean[:-1].rstrip()

        # 6. Ensure first letter is capitalized
        if clean and clean[0].isalpha() and not clean[0].isupper():
            clean = clean[0].upper() + clean[1:]

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

    active_terms = {}
    if entity_map and isinstance(entity_map, dict):
        active_terms.update(entity_map)
    if glossary and isinstance(glossary, dict):
        active_terms.update(glossary)

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

        # Validate glossary compliance only when explicitly defined
        src_str = str(src or "")
        for src_key, target_val in active_terms.items():
            if src_key and target_val and src_key in src_str:
                if str(target_val).lower() not in clean_tr.lower():
                    reasons.append(
                        f"Câu {idx} chứa thuật ngữ '{src_key}' nhưng bản dịch thiếu '{target_val}': '{clean_tr}'"
                    )

        # Ensure no premature sentence termination in mid-clause segments
        src_trimmed = src_str.strip()
        src_ends_terminal = any(src_trimmed.endswith(p) for p in ("。", "！", "？", ".", "!", "?"))
        if not src_ends_terminal and clean_tr.endswith((".", "!", "?")):
            reasons.append(
                f"Câu {idx} câu nguồn chưa kết thúc nhưng bản dịch lại có dấu kết câu ở giữa: '{clean_tr}'"
            )

    return len(reasons) == 0, reasons

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
    script_mode="default",
    entity_map=None,
    glossary=None,
    **kwargs
):
    entity_map = entity_map or kwargs.get("entity_map")
    glossary = glossary or kwargs.get("glossary")
    script_mode = script_mode or kwargs.get("script_mode") or os.getenv("SCRIPT_MODE", "default")
    kwargs["script_mode"] = script_mode
    logger.info("Translating subtitles (script_mode=%s)...", script_mode)
    texts = [seg.content for seg in srt_segments if seg.content]
    if not texts:
        return srt_segments
        
    translated_texts = None
    failure_causes = []
    kwargs["failure_causes"] = failure_causes
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
            g_key = api_key or os.getenv("GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEYS", "")
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
    if not translated_texts and enable_g4f:
        logger.info("Trying ChatGPT (G4F) API via bounded fallback...")
        try:
            from ai.v1_bounded_fallback import run_fallback
            translated_texts = run_fallback(lambda: translate_with_g4f(texts, target_lang, script_mode=script_mode), timeout=40)
        except TimeoutError:
            logger.warning("G4F phản hồi quá lâu (quá 40s), hủy để tránh treo bot.")
            translated_texts = None
        except Exception as e:
            logger.warning(f"Lỗi G4F: {e}")
            translated_texts = None

    if (
        translated_texts
        and len(translated_texts) == len(texts)
        and all(isinstance(item, str) for item in translated_texts)
    ):
        translated_texts = [
            normalize_subtitle_text(t)
            for t in clean_incomplete_segment_stops(translated_texts)
        ]
        translated_texts = ensure_speech_pauses_and_entity(
            texts,
            translated_texts,
            entity_map=entity_map or kwargs.get("entity_map"),
            prior_context=prior_context,
            glossary=glossary or kwargs.get("glossary"),
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
            # Granular retry: Retry specifically failing items with alternate LLM candidate
            failing_indices = set()
            for r in reasons:
                m = re.match(r"Câu (\d+)", r)
                if m:
                    failing_indices.add(int(m.group(1)) - 1)

            if failing_indices:
                sorted_failing = sorted(failing_indices)
                failing_texts = [texts[i] for i in sorted_failing]
                logger.warning(
                    "Dịch thuật batch gặp %d lỗi chất lượng ở các câu %s: %s. Thử lại cụ thể các câu lỗi qua LLM...",
                    len(reasons),
                    [i + 1 for i in sorted_failing],
                    reasons,
                )
                retranslated = None
                available_providers = ["gemini", "openai", "deepseek"]
                alt_providers = [p for p in available_providers if p != used_provider]
                if not alt_providers or used_provider == "gemini":
                    if "gemini" not in alt_providers:
                        alt_providers.append("gemini")
                g_key = api_key or os.getenv("GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEYS", "")
                for alt_p in alt_providers:
                    if alt_p == "openai" and os.getenv("OPENAI_API_KEY"):
                        retranslated = translate_with_openai(failing_texts, target_lang, os.getenv("OPENAI_API_KEY"), **kwargs)
                    elif alt_p == "deepseek" and os.getenv("DEEPSEEK_API_KEY"):
                        retranslated = translate_with_deepseek(failing_texts, target_lang, os.getenv("DEEPSEEK_API_KEY"), **kwargs)
                    elif alt_p == "gemini" and g_key:
                        retranslated = translate_with_gemini(failing_texts, target_lang, g_key, **kwargs)
                    if retranslated and len(retranslated) == len(failing_texts):
                        for k, orig_idx in enumerate(sorted_failing):
                            translated_texts[orig_idx] = retranslated[k]
                        break

                # Re-clean and re-validate
                translated_texts = [
                    normalize_subtitle_text(t)
                    for t in clean_incomplete_segment_stops(translated_texts)
                ]
                translated_texts = ensure_speech_pauses_and_entity(
                    texts,
                    translated_texts,
                    entity_map=entity_map or kwargs.get("entity_map"),
                    prior_context=prior_context,
                    glossary=glossary or kwargs.get("glossary"),
                )
                is_valid, reasons = validate_translation_batch_quality(
                    texts,
                    translated_texts,
                    prior_context=prior_context,
                    entity_map=entity_map or kwargs.get("entity_map"),
                    glossary=glossary or kwargs.get("glossary"),
                    strict=True,
                )

            if not is_valid:
                allow_fallback = os.getenv("V1_ALLOW_TRANSLATION_FALLBACK", "false").lower() in ("true", "1", "yes")
                if strict and not allow_fallback:
                    raise RuntimeError(f"Strict translation mode requires a complete LLM translation conforming to quality rules: {'; '.join(reasons)}")
                logger.warning(
                    f"Strict translation quality check flagged: {'; '.join(reasons)}. Proceeding with sanitized translation to avoid job failure."
                )

    if translated_texts_valid and not strict:
        unchanged_cjk = [
            position
            for position, (source, translated) in enumerate(
                zip(texts, translated_texts), 1
            )
            if target_lang.lower().startswith("vi") and _contains_cjk(translated)
        ]
        if unchanged_cjk:
            logger.warning(
                "AI translation left CJK source unchanged at %d positions; applying partial translation before fallback",
                len(unchanged_cjk),
            )
            try:
                from mojibake_repair import repair_vietnamese_mojibake
                translated_texts = [repair_vietnamese_mojibake(t) for t in translated_texts]
            except Exception:
                pass
            p_idx = 0
            for segment in srt_segments:
                if not segment.content:
                    continue
                if p_idx < len(translated_texts) and not _contains_cjk(translated_texts[p_idx]):
                    segment.orig_content = segment.content
                    segment.content = translated_texts[p_idx]
                p_idx += 1
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

    if strict:
        allow_fallback = os.getenv("V1_ALLOW_TRANSLATION_FALLBACK", "false").lower() in ("true", "1", "yes")
        if not allow_fallback:
            raise RuntimeError("Strict translation mode requires a complete LLM translation without machine fallback")
        logger.warning("Strict translation mode: LLM translation unavailable after retries. Falling back to machine translation to prevent job failure...")

    logger.info("Falling back to Google Translate...")
    failed_segments = []
    error_keywords = [
        "error 500", "server error", "invalid source language",
        "langpair", "almost all languages supported", "mymemory",
        "query length limit", "daily limit reached", "too many requests"
    ]
    from concurrent.futures import ThreadPoolExecutor

    def _translate_one_seg(segment):
        if not segment.content:
            return segment, segment.content, None
        orig_content = segment.content
        if not _contains_cjk(segment.content):
            return segment, segment.content, None

        if kwargs.get("stop_checker") and kwargs["stop_checker"]():
            return segment, orig_content, RuntimeError("Operation cancelled by user stop request.")

        try:
            src_lang = 'zh-CN'
            translated_text = None
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
            translated_text = normalize_subtitle_text(translated_text)
            if not translated_text.strip():
                raise RuntimeError("Google Translate returned an empty result")
            if any(k in translated_text.lower() for k in error_keywords) or translated_text.startswith("Error"):
                raise RuntimeError("Google Translate returned an error payload: {}".format(translated_text[:120]))
            if target_lang.lower().startswith("vi") and _contains_cjk(translated_text):
                raise RuntimeError("Chinese source text remained untranslated")

            return segment, translated_text, None
        except Exception as e:
            return segment, orig_content, e

    with ThreadPoolExecutor(max_workers=5) as executor:
        trans_results = list(executor.map(_translate_one_seg, srt_segments))

    for segment, translated_text, err in trans_results:
        if err is not None:
            logger.warning(f"Lỗi dịch thuật cho đoạn '{getattr(segment, 'content', '')}': {err}")
            failed_segments.append(int(getattr(segment, "index", 0)))
        else:
            if not getattr(segment, "orig_content", None):
                segment.orig_content = segment.content
        segment.content = translated_text
        
    if failed_segments and strict:
        raise RuntimeError(
            "Translation failed for segment indexes: {}".format(
                ", ".join(str(index) for index in failed_segments)
            )
        ) from (failure_causes[-1] if failure_causes else None)

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
    4. Bảo toàn các số quan trọng và giữ tính phủ định nếu câu gốc là phủ định.
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
    orig_has_neg = any(f" {neg} " in f" {orig_text.lower()} " for neg in negations)
    short_has_neg = any(f" {neg} " in f" {shortened.lower()} " for neg in negations)
    if orig_has_neg and not short_has_neg:
        # Nếu câu gốc có nhiều từ phủ định (phủ định kép: không ai không thích -> thích) thì câu khẳng định hợp lệ.
        # Ngược lại, nếu câu gốc phủ định đơn thì bắt buộc câu rút gọn cũng phải giữ tính phủ định.
        orig_neg_count = sum(1 for neg in negations if f" {neg} " in f" {orig_text.lower()} ")
        if orig_neg_count < 2:
            return False

    return True


def trim_to_target_words(text: str, target_words: int) -> str:
    """Rút gọn phụ trợ nếu câu vẫn hơi dài: lược bỏ các từ đệm mà không làm đổi ý nghĩa."""
    words = text.strip().split()
    if len(words) <= target_words:
        return text
    # Danh sách từ đệm có thể lược bỏ an toàn trong khẩu ngữ tiếng Việt
    fillers = {"thì", "mà", "là", "rằng", "ấy", "nhé", "nha", "ạ", "luôn", "này", "cái", "các", "nhưng", "đấy", "vậy"}
    filtered = [w for w in words if w.lower() not in fillers]
    if len(filtered) > target_words:
        filtered = filtered[:target_words]
    elif len(filtered) < max(1, target_words // 2):
        filtered = words[:target_words]
    return " ".join(filtered)


def condense_vietnamese_subtitles_batch(
    items: list,
    api_key: str = "",
    deadline: Optional[float] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
    job_id: str = "condense",
    script_mode: str = "default",
) -> dict:
    """
    Rút gọn các câu thoại tiếng Việt quá dài theo ngữ nghĩa bằng Gemini theo Kế hoạch Codex:
    1. Kiểm tra cache trước (phân tách theo script_mode).
    2. Gom tất cả câu chưa cache vào đúng MỘT lượt gọi qua v1_gemini_dispatcher.
    3. Ưu tiên model nhẹ, nhanh: gemini-2.5-flash -> gemini-2.5-flash-lite -> gemini-3.5-flash-lite.
    4. Kiểm tra cấu trúc và ngữ nghĩa chặt chẽ (giữ số, phủ định, không rỗng, không CJK).
    5. Chỉ chấp nhận các câu hợp lệ, ghi cache.
    """
    if not items:
        return {}

    script_mode = script_mode or os.getenv("SCRIPT_MODE", "default")
    from .v1_translation_cache import read_condense_cache, write_condense_cache
    from mojibake_repair import repair_vietnamese_mojibake

    result = {}
    uncached_items = []

    for it in items:
        idx = it["index"]
        text = it["text"].strip()
        sec = float(it.get("target_seconds", 2.0))
        if sec < 1.0:
            default_tw = max(1, int(sec / 0.35))
        else:
            default_tw = max(2, int((sec - 0.05) / 0.28))
        tw = int(it.get("target_words") or default_tw)
        cached_val = read_condense_cache(text, sec, tw, script_mode=script_mode)
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
        logger.info(f"[CONDENSE] Tất cả {len(items)} câu đều có sẵn trong cache (100% Cache HIT, script_mode={script_mode}).")
        return result

    is_humorous = str(script_mode or "default").lower() in ("humorous", "hai_huoc", "haihuoc", "comedy")
    humorous_rule = (
        "4. ĐẶC BIỆT: Đây là video có kịch bản HÀI HƯỚC, TẤU HÀI. Khi rút gọn câu, BẮT BUỘC giữ lại "
        "tính dí dỏm, tếu táo, từ lóng gây cười và ngữ điệu vui nhộn, TUYỆT ĐỐI KHÔNG làm câu bị khô khan hay nghiêm túc hóa!\n"
        if is_humorous else ""
    )

    prompt = (
        "Bạn là chuyên gia biên tập phụ đề video ngắn chuyên nghiệp.\n"
        "Các câu thoại tiếng Việt sau đây đang đọc quá dài so với thời lượng video gốc.\n"
        "Nhiệm vụ: Viết lại/rút gọn từng câu sao cho thật ngắn gọn, súc tích (cô đọng nội dung, bỏ từ đệm thừa, "
        "giữ trọn vẹn ý chính và tự nhiên, khống chế số lượng từ tối đa theo yêu cầu để người đọc và AI đọc trọn vẹn mà không bị nhanh).\n"
        "QUY TẮC BẮT BUỘC:\n"
        "1. Mỗi câu rút gọn BẮT BUỘC phải ÍT TỪ HƠN câu gốc và KHÔNG ĐƯỢC VƯỢT QUÁ số từ mục tiêu (tối đa N từ). Tuyệt đối không được viết dài hơn câu gốc.\n"
        "2. Giữ trọn vẹn các con số và từ phủ định (không, chưa, chẳng, đừng) nếu câu gốc có.\n"
        "3. Trả về duy nhất một đối tượng JSON ánh xạ ID dạng chuỗi sang câu đã rút gọn, ví dụ:\n"
        '{"1": "câu 1 ngắn gọn", "2": "câu 2 ngắn gọn"}\n'
        f"{humorous_rule}"
        "Tuyệt đối không thêm lời dẫn giải hay bất kỳ ký tự nào ngoài JSON.\n\n"
        "Danh sách câu cần rút gọn:\n"
    )
    for it in uncached_items:
        prompt += f"- ID {it['index']} (mục tiêu: ~{it['target_seconds']:.2f}s, tối đa {it['target_words']} từ): \"{it['text']}\"\n"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
        }
    }

    try:
        from .v1_gemini_response import response_json, GeminiResponseError
        def validate_response(data):
            parsed = response_json(data, dict)
            if not isinstance(parsed, dict) or not parsed:
                raise GeminiResponseError("Invalid condensed dialogue response")
            has_valid = False
            for item in uncached_items:
                idx_str = str(item["index"])
                shortened = parsed.get(idx_str) or parsed.get(item["index"])
                if shortened and isinstance(shortened, str):
                    cleaned = repair_vietnamese_mojibake(shortened.strip())
                    if validate_condensed_text(item["text"], cleaned, item["target_words"]):
                        has_valid = True
                        break
                    trimmed = trim_to_target_words(cleaned, item["target_words"])
                    if validate_condensed_text(item["text"], trimmed, item["target_words"]):
                        has_valid = True
                        break
                    orig_w = item["text"].strip().split()
                    short_w = cleaned.split()
                    if 0 < len(short_w) < len(orig_w) and not any("\u3400" <= c <= "\u9fff" for c in cleaned):
                        has_valid = True
                        break
            if not has_valid:
                raise GeminiResponseError("No valid condensed dialogue in response")
        data, used_model = call_gemini_api(
            payload=payload,
            purpose="condensation",
            models=DEFAULT_CONDENSATION_MODELS,
            overall_deadline=deadline,
            stop_checker=stop_checker,
            job_id=job_id,
            api_key=api_key,
            timeout_per_request=20.0,
            response_validator=validate_response,
        )
        validate_response(data)
        parts_out = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts_out if not p.get("thought")).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            valid_count = 0
            for it in uncached_items:
                idx = it["index"]
                orig_t = it["text"]
                shortened = parsed.get(str(idx)) or parsed.get(idx)
                if shortened and isinstance(shortened, str):
                    shortened = repair_vietnamese_mojibake(shortened.strip())
                    if not validate_condensed_text(orig_t, shortened, it["target_words"]):
                        # Cắt tỉa từ đệm nếu câu do Gemini sinh ra vẫn hơi dài hơn ngưỡng
                        shortened_trimmed = trim_to_target_words(shortened, it["target_words"])
                        if validate_condensed_text(orig_t, shortened_trimmed, it["target_words"]):
                            shortened = shortened_trimmed

                    if validate_condensed_text(orig_t, shortened, it["target_words"]):
                        result[idx] = shortened
                        write_condense_cache(orig_t, it["target_seconds"], it["target_words"], shortened, used_model, script_mode=script_mode)
                        valid_count += 1
                    else:
                        # Fallback cấp cứu: nếu câu ngắn hơn hẳn câu gốc và không chứa CJK/markdown
                        orig_w = orig_t.strip().split()
                        short_w = shortened.split()
                        if len(short_w) < len(orig_w) and not any("\u4e00" <= c <= "\u9fff" for c in shortened):
                            logger.info(f"[CONDENSE] Áp dụng fallback khẩn cấp cho câu #{idx}: '{orig_t}' -> '{shortened}'")
                            result[idx] = shortened
                            valid_count += 1
                        else:
                            trimmed_orig = trim_to_target_words(orig_t, max(it["target_words"], max(1, len(orig_w) // 2)))
                            if len(trimmed_orig.split()) < len(orig_w):
                                logger.info(f"[CONDENSE] Cắt tỉa từ đệm câu gốc #{idx}: '{orig_t}' -> '{trimmed_orig}'")
                                result[idx] = trimmed_orig
                                valid_count += 1
                            else:
                                logger.warning(f"[CONDENSE] Câu #{idx} không vượt qua kiểm định (giữ nguyên): '{shortened}'")
            logger.info(f"[CONDENSE] Gemini {used_model} rút gọn thành công {valid_count}/{len(uncached_items)} câu chưa cache!")
    except Exception as e:
        from .v1_gemini_dispatcher import GeminiCancelledError
        if isinstance(e, GeminiCancelledError):
            raise
        logger.warning(f"[CONDENSE] Rút gọn câu qua Gemini không hoàn tất: {e}")

    return result




