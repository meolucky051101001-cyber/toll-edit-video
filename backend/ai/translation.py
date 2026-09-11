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
except ImportError:
    from v1_stage_metrics import stage

logger = logging.getLogger(__name__)
_gemini_health_lock = threading.Lock()
_gemini_cooldown = {}
_gemini_last_good = {}


def _contains_cjk(text):
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))

def build_translation_prompt(texts, target_lang="vi", prior_context=None, with_vision=True):
    lang_name = "Tiáº¿ng Viá»‡t" if target_lang == "vi" else target_lang
    prompt = f"""Báº¡n lÃ  má»™t chuyÃªn gia dá»‹ch thuáº­t ná»™i dung máº¡ng xÃ£ há»™i (Tiktok, Douyin).
Nhiá»‡m vá»¥: Dá»‹ch máº£ng JSON chá»©a cÃ¡c cÃ¢u phá»¥ Ä‘á» dÆ°á»›i Ä‘Ã¢y sang {lang_name}.
YÃªu cáº§u Tá»I QUAN TRá»ŒNG:
1. Báº®T BUá»˜C giá»¯ nguyÃªn sá»‘ lÆ°á»£ng pháº§n tá»­ cá»§a máº£ng JSON. Má»—i cÃ¢u gá»‘c tÆ°Æ¡ng á»©ng Ä‘Ãºng 1 cÃ¢u dá»‹ch. KhÃ´ng tá»± Ã½ gá»™p cÃ¢u hay tÃ¡ch cÃ¢u Ä‘á»ƒ Ä‘áº£m báº£o khá»›p thá»i gian hiá»ƒn thá»‹ (timing).
2. Dá»ŠCH CHUáº¨N XÃC NHÆ¯NG Háº¤P DáºªN: Æ¯u tiÃªn dá»‹ch Ä‘Ãºng nghÄ©a Ä‘en vÃ  bÃ³ng cá»§a cÃ¢u chá»¯. Giá»¯ vÄƒn phong tá»± nhiÃªn, cuá»‘n hÃºt, cÃ³ chÃºt thiÃªn hÆ°á»›ng máº¡ng xÃ£ há»™i Ä‘á»ƒ Ä‘Äƒng video.
3. Xá»¬ LÃ Tá»ª NGá»® VÄ‚N HOA/THÆ  CA: CÃ¡c video Douyin thÆ°á»ng dÃ¹ng cÃ¢u chá»¯ hoa má»¹. VÃ­ dá»¥ 'æ‡’æ˜¥ç§‹' mang Ã½ nghÄ©a 'thÆ° thÃ¡i, nhÃ n háº¡' chá»© KHÃ”NG PHáº¢I lÃ  'lÆ°á»i biáº¿ng'. HÃ£y dá»‹ch thoÃ¡t Ã½, sang trá»ng.
4. TUYá»†T Äá»I KHÃ”NG láº¡m dá»¥ng tá»« tiáº¿ng Anh. Æ¯u tiÃªn tiáº¿ng Viá»‡t thuáº§n tÃºy.
5. KHá»šP KHáº¨U HÃŒNH & THá»œI LÆ¯á»¢NG (LIP-SYNC): VÄƒn báº£n dá»‹ch dÃ¹ng Ä‘á»ƒ lá»“ng tiáº¿ng (TTS), Ä‘á»™ dÃ i Ã¢m tiáº¿t cá»§a cÃ¢u tiáº¿ng Viá»‡t PHáº¢I TÆ¯Æ NG ÄÆ¯Æ NG Vá»šI CÃ‚U Gá»C Ä‘á»ƒ khá»›p hoÃ n háº£o kháº©u hÃ¬nh miá»‡ng cá»§a nhÃ¢n váº­t.
6. THUáº¬T NGá»® KIáº¾N TRÃšC & Äá»œI Sá»NG: 'ä¸‰åˆé™¢' dá»‹ch lÃ  'nhÃ  tam há»£p viá»‡n / nhÃ  ba gian', 'å åœ°' dá»‹ch lÃ  'diá»‡n tÃ­ch Ä‘áº¥t', 'å¤§æ°”' dá»‹ch lÃ  'bá» tháº¿, sang trá»ng / Ä‘áº³ng cáº¥p' (tuyá»‡t Ä‘á»‘i khÃ´ng dá»‹ch thÃ nh 'dáº¥u chÃ¢n', 'khÃ­ quyá»ƒn').
7. Lá»ŒC HOáº¶C VIá»†T HÃ“A CÃ‚U KÃŠU Gá»ŒI (CTA): CÃ¡c cÃ¢u kÃªu gá»i Douyin/TikTok nhÆ° 'å›žå¤888', 'å…³æ³¨æˆ‘', 'ç‚¹èµž' hÃ£y dá»‹ch khÃ©o thÃ nh lá»i kÃªu gá»i tá»± nhiÃªn ngáº¯n gá»n (vÃ­ dá»¥: 'Ä‘á»ƒ láº¡i bÃ¬nh luáº­n bÃªn dÆ°á»›i nhÃ©' hoáº·c 'liÃªn há»‡ ngay nhÃ©'), khÃ´ng dá»‹ch mÃ¡y sá»‘ hiá»‡u thÃ´ thiá»ƒn.
8. Ngá»¯ cáº£nh ná»‘i tiáº¿p: VÃ¬ phá»¥ Ä‘á» thÆ°á»ng bá»‹ ngáº¯t giá»¯a chá»«ng, hÃ£y Ä‘á»c cáº£ Ä‘oáº¡n Ä‘á»ƒ dá»‹ch sao cho Ã½ ná»‘i liá»n máº¡ch trÆ¡n tru.
"""
    if with_vision:
        prompt += "7. TRá»°C QUAN: HÃ£y káº¿t há»£p cÃ¡c bá»©c áº£nh Ä‘Ã­nh kÃ¨m tá»« video Ä‘á»ƒ chá»n Ä‘áº¡i tá»« nhÃ¢n xÆ°ng vÃ  danh tá»« chÃ­nh xÃ¡c tuyá»‡t Ä‘á»‘i vá»›i ngá»¯ cáº£nh.\n"
    prompt += "8. CHá»ˆ tráº£ vá» máº£ng JSON chá»©a cÃ¡c chuá»—i dá»‹ch, khÃ´ng giáº£i thÃ­ch, khÃ´ng markdown.\n"
    prompt += "Dá»¯ liá»‡u:\n"
    if prior_context:
        prompt += "Ngá»¯ cáº£nh ná»‘i tiáº¿p tá»« batch trÆ°á»›c (khÃ´ng dá»‹ch láº¡i):\n"
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
        logger.debug(f"KhÃ´ng thá»ƒ trÃ­ch xuáº¥t áº£nh tá»« video: {img_e}")
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
                logger.info("Sử dụng bản dịch từ cache")
                with _gemini_health_lock:
                    _gemini_last_good[account] = cached["model"]
                return cached["texts"]
        except ImportError:
            cache_k = None
            write_cache = None

        with _gemini_health_lock:
            last_good = _gemini_last_good.get(account)
            if last_good in models_to_try:
                models_to_try.remove(last_good)
                models_to_try.insert(0, last_good)
            models_to_try = [m for m in models_to_try
                             if _gemini_cooldown.get((account, m), 0) <= time.monotonic()]
        deadline = time.monotonic() + 90.0
        for model in models_to_try:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                break
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            payload = {"contents": [{"parts": parts}]}
            headers = {"Content-Type": "application/json"}
            try:
                logger.info(f"Äang gá»i Google Gemini: {model}...")
                response = requests.post(url, json=payload, headers=headers,
                                         timeout=(min(5.0, remaining / 2), min(25.0, remaining / 2)))
                if response.status_code == 200:
                    result = response.json()
                    parts_out = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    raw = "".join(p.get("text", "") for p in parts_out if not p.get("thought")).strip()
                    match = re.search(r'\[.*\]', raw, re.DOTALL)
                    translated = json.loads(match.group(0) if match else raw)
                    if not isinstance(translated, list) or len(translated) != len(texts) or not all(
                            isinstance(t, str) and t.strip() for t in translated):
                        raise ValueError("Invalid translation array")
                    if write_cache:
                        write_cache(cache_k, translated, model)
                    with _gemini_health_lock:
                        _gemini_last_good[account] = model
                        _gemini_cooldown.pop((account, model), None)
                    logger.info(f"Gá»i thÃ nh cÃ´ng Gemini {model}!")
                    return translated
                else:
                    logger.warning(f"Lá»—i gá»i {model} (HTTP {response.status_code})")
                    with _gemini_health_lock:
                        if response.status_code == 429:
                            _gemini_cooldown[(account, model)] = time.monotonic() + 15
                        elif response.status_code in (500, 502, 503, 504):
                            _gemini_cooldown[(account, model)] = time.monotonic() + 20
                        elif response.status_code == 404:
                            _gemini_cooldown[(account, model)] = time.monotonic() + 86400
                        else:
                            _gemini_cooldown[(account, model)] = time.monotonic() + 60
                    if response.status_code in (401, 403):
                        break
            except Exception as req_e:
                # Exception URLs can contain API keys; log only the error type.
                logger.warning("Lá»—i dá»‹ch %s: %s", model, type(req_e).__name__)
                with _gemini_health_lock:
                    if isinstance(req_e, (requests.ConnectionError, requests.Timeout)):
                        _gemini_cooldown.pop((account, model), None)
                    else:
                        _gemini_cooldown[(account, model)] = time.monotonic() + 30
                
        return None
    except Exception as e:
        logger.warning(f"Lá»—i dá»‹ch Gemini: {e}")
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
                logger.info(f"Äang gá»i OpenAI ChatGPT ({om})...")
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
                        logger.info(f"Dá»‹ch thÃ nh cÃ´ng báº±ng OpenAI ChatGPT ({om})!")
                        return translated
                else:
                    logger.warning(f"Lá»—i OpenAI ({om}) HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as o_err:
                logger.warning(f"Lá»—i gá»i OpenAI ({om}): {o_err}")
    except Exception as e:
        logger.warning(f"Lá»—i dá»‹ch OpenAI: {e}")
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
                logger.info(f"Äang gá»i DeepSeek ({dm})...")
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
                        logger.info(f"Dá»‹ch thÃ nh cÃ´ng báº±ng DeepSeek ({dm})!")
                        return translated
                else:
                    logger.warning(f"Lá»—i DeepSeek ({dm}) HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as d_err:
                logger.warning(f"Lá»—i gá»i DeepSeek ({dm}): {d_err}")
    except Exception as e:
        logger.warning(f"Lá»—i dá»‹ch DeepSeek: {e}")
    return None

def translate_with_g4f(texts, target_lang="vi"):
    try:
        import g4f
        lang_name = "Tiáº¿ng Viá»‡t" if target_lang == "vi" else target_lang
        prompt = f"""Báº¡n lÃ  má»™t chuyÃªn gia dá»‹ch thuáº­t ná»™i dung máº¡ng xÃ£ há»™i (Tiktok, Douyin).
Nhiá»‡m vá»¥: Dá»‹ch máº£ng JSON chá»©a cÃ¡c cÃ¢u phá»¥ Ä‘á» dÆ°á»›i Ä‘Ã¢y sang {lang_name}.
YÃªu cáº§u Tá»I QUAN TRá»ŒNG:
1. Báº®T BUá»˜C giá»¯ nguyÃªn sá»‘ lÆ°á»£ng pháº§n tá»­ cá»§a máº£ng JSON.
2. Dá»‹ch tá»± nhiÃªn, cuá»‘n hÃºt, chuáº©n vÄƒn phong video ngáº¯n máº¡ng xÃ£ há»™i.
3. CHá»ˆ tráº£ vá» máº£ng JSON chá»©a cÃ¡c chuá»—i dá»‹ch, khÃ´ng giáº£i thÃ­ch, khÃ´ng markdown.
Dá»¯ liá»‡u:
"""
        prompt += json.dumps(texts, ensure_ascii=False)
        fallback_models = ["gpt-4o", "deepseek-v3", "claude-3.5-sonnet"]
        for g4f_model in fallback_models:
            try:
                logger.info(f"Äang gá»i mÃ´ hÃ¬nh AI miá»…n phÃ­ {g4f_model} qua G4F...")
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
                    logger.info(f"Dá»‹ch thÃ nh cÃ´ng báº±ng mÃ´ hÃ¬nh {g4f_model}!")
                    return translated
            except Exception as m_err:
                logger.debug(f"MÃ´ hÃ¬nh {g4f_model} gáº·p lá»—i: {m_err}")
    except Exception as e:
        logger.debug(f"Lá»—i dá»‹ch G4F: {e}")
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
    
    # Danh sÃ¡ch thá»© tá»± Æ°u tiÃªn cÃ¡c nhÃ  cung cáº¥p LLM
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
                logger.warning("G4F pháº£n há»“i quÃ¡ lÃ¢u (quÃ¡ 40s), há»§y Ä‘á»ƒ trÃ¡nh treo bot.")
                translated_texts = None
            except Exception as e:
                logger.warning(f"Lá»—i G4F: {e}")
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
            src_lang = 'zh-CN' if _contains_cjk(segment.content) else 'auto'
            try:
                translator = GoogleTranslator(source=src_lang, target=target_lang)
                translated_text = translator.translate(segment.content)
            except Exception:
                translated_text = None
            
            if (
                not translated_text
                or "Error 500" in str(translated_text)
                or "Server Error" in str(translated_text)
                or str(translated_text).startswith("Error")
                or (target_lang.lower().startswith("vi") and _contains_cjk(translated_text))
            ):
                try:
                    if MyMemoryTranslator is None:
                        raise RuntimeError("MyMemoryTranslator is unavailable")
                    mm_target = "vi-VN" if target_lang.lower().startswith("vi") else target_lang
                    mm_src = "zh-CN" if _contains_cjk(segment.content) else "auto"
                    translated_text = MyMemoryTranslator(source=mm_src, target=mm_target).translate(segment.content)
                except Exception:
                    pass

            if not isinstance(translated_text, str) or not translated_text.strip():
                raise RuntimeError("Google Translate returned an empty result")
            if (
                "Error 500" in translated_text
                or "Server Error" in translated_text
                or translated_text.startswith("Error")
            ):
                raise RuntimeError(
                    "Google Translate returned an error payload: {}".format(
                        translated_text[:120]
                    )
                )
            if target_lang.lower().startswith("vi") and _contains_cjk(translated_text):
                raise RuntimeError("Chinese source text remained untranslated")
                
        except Exception as e:
            logger.warning(f"Lá»—i dá»‹ch thuáº­t: {e}")
            failed_segments.append(int(getattr(segment, "index", 0)))
            translated_text = segment.content
            
        segment.content = translated_text
        
    if failed_segments and strict:
        raise RuntimeError(
            "Translation failed for segment indexes: {}".format(
                ", ".join(str(index) for index in failed_segments)
            )
        )

    return srt_segments


