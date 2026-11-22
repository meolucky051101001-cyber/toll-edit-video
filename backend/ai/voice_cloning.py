import os
import asyncio
import re
import random
import subprocess
import threading
import logging
import edge_tts
try:
    from ..v1_stage_metrics import stage
except ImportError:
    from v1_stage_metrics import stage
from pydub import AudioSegment
from voice_selection import validate_tts_source
from v1_speech_guard import calculate_reading_windows

logger = logging.getLogger(__name__)

edge_semaphore = asyncio.Semaphore(int(os.getenv("V1_EDGE_WORKERS", "4")))
capcut_semaphore = threading.Semaphore(int(os.getenv("V1_CAPCUT_WORKERS", "6")))


def build_atempo_filter(ratio: float) -> str:
    """Ghép chuỗi filter atempo của FFmpeg cho dải tốc độ mở rộng an toàn [0.25x - 4.0x]."""
    ratio = max(0.25, min(float(ratio), 4.0))
    filters = []
    current = ratio
    while current > 2.0:
        filters.append("atempo=2.0")
        current /= 2.0
    while current < 0.5:
        filters.append("atempo=0.5")
        current /= 0.5
    filters.append(f"atempo={current:.6f}")
    return ",".join(filters)


class FPTQuotaError(Exception): pass
class TTSIncompleteError(RuntimeError): pass
@stage("tts_edge_including_queue")
async def generate_tts_edge(
    text,
    output_path,
    voice="vi-VN-HoaiMyNeural",
    rate="+0%",
    pitch="+0Hz",
    attempts=4,
    retry_delays=(2.0, 5.0, 10.0),
    target_duration=None,
):
    text_clean = str(text or "").strip()
    silence_dur = f"{max(0.2, float(target_duration or 0.5)):.2f}"
    if not any(c.isalnum() for c in text_clean):
        # Text has no pronounceable words, emit silence matching target window
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", silence_dur, "-c:a", "libmp3lame", "-b:a", "64k", str(output_path)
        ]
        subprocess.run(cmd, check=True)
        return True

    async with edge_semaphore:
        last_error = None
        for attempt in range(max(1, int(attempts))):
            curr_rate = rate if attempt == 0 else "+0%"
            curr_pitch = pitch if attempt == 0 else "+0Hz"
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
                communicate = edge_tts.Communicate(
                    text_clean, voice, rate=curr_rate, pitch=curr_pitch
                )
                await asyncio.wait_for(communicate.save(output_path), timeout=35.0)
                if not os.path.isfile(output_path) or os.path.getsize(output_path) < 128:
                    raise RuntimeError("Edge TTS returned empty audio")
                await asyncio.sleep(0.15)
                return False
            except Exception as exc:
                last_error = exc
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except OSError:
                    pass
                if attempt + 1 >= max(1, int(attempts)):
                    logger.error("Edge TTS thất bại sau %s lần thử cho câu '%s': %s", attempts, text_clean[:40], exc)
                    break

                base_delay = (
                    retry_delays[min(attempt, len(retry_delays) - 1)]
                    if retry_delays
                    else 0.0
                )
                delay = base_delay + random.uniform(0.5, 1.5)
                print(
                    "Edge TTS retry {}/{} after {}: {}".format(
                        attempt + 2, attempts, type(exc).__name__, exc
                    )
                )
                await asyncio.sleep(max(0.0, float(delay)))
        raise RuntimeError("Edge TTS failed after {} attempts".format(attempts)) from last_error

_shared_capcut_client = None

def _get_capcut_client():
    global _shared_capcut_client
    from capcut_tts_api import CapCutClient
    if _shared_capcut_client is None or not isinstance(_shared_capcut_client, CapCutClient):
        _shared_capcut_client = CapCutClient()
        if hasattr(_shared_capcut_client, "session") and _shared_capcut_client.session is not None:
            try:
                from requests.adapters import HTTPAdapter
                adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
                _shared_capcut_client.session.mount("https://", adapter)
                _shared_capcut_client.session.mount("http://", adapter)
            except Exception:
                pass
    return _shared_capcut_client

@stage("tts_capcut_attempt")
def _run_capcut_tts_once(
    text, output_path, voice="BV562_streaming", poll_interval=3.0, cancel=None
):
    import json, time
    client = _get_capcut_client()
    
    res = client.create_tts_task(texts=text, voice=voice)
    task_id = res["data"]["tasks"][0]["id"]
    token = res["data"]["tasks"][0]["token"]
    
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        delay = min(max(0.0, float(poll_interval)), max(0.0, deadline - time.monotonic()))
        if cancel is None:
            time.sleep(delay)
        elif cancel.wait(delay):
            raise InterruptedError("Translation prefetch ended")
        if time.monotonic() >= deadline:
            break
        query_res = client.query_tts_task(task_id, token)
        status = query_res["data"]["tasks"][0]["status"]
        if status in ("success", "succeed"):
            payload = json.loads(query_res["data"]["tasks"][0]["payload"])
            speech_url = payload["audio_subtitles"][0]["speech_url"]
            r = client.session.get(speech_url, timeout=30)
            r.raise_for_status()
            if len(r.content) < 128:
                raise RuntimeError("CapCut TTS returned empty audio")
            with open(output_path, "wb") as f:
                f.write(r.content)
            return True
        elif status == "failed":
            raise Exception("CapCut TTS task failed")
            
    raise TimeoutError("CapCut TTS polling exceeded 60s (network calls may add time)")


def _run_capcut_tts(
    text,
    output_path,
    voice="BV562_streaming",
    attempts=3,
    retry_delays=(1.5, 3.0),
    poll_interval=0.5,
):
    import time

    last_error = None
    with capcut_semaphore:
        for attempt in range(max(1, int(attempts))):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
                return _run_capcut_tts_once(
                    text,
                    output_path,
                    voice=voice,
                    poll_interval=poll_interval,
                )
            except Exception as exc:
                last_error = exc
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except OSError:
                    pass
                if attempt + 1 >= max(1, int(attempts)):
                    break
                delay = (
                    retry_delays[min(attempt, len(retry_delays) - 1)]
                    if retry_delays
                    else 0.0
                )
                print(
                    "CapCut TTS retry {}/{} after {}: {}".format(
                        attempt + 2, attempts, type(exc).__name__, exc
                    )
                )
                time.sleep(max(0.0, float(delay)))
    raise RuntimeError("CapCut TTS failed after {} attempts".format(attempts)) from last_error

async def generate_tts_fpt(text, output_path, api_key, voice="banmai", speed="0"):
    import httpx
    url = "https://api.fpt.ai/hmi/tts/v5"
    headers = {
        "api-key": api_key,
        "voice": voice,
        "speed": speed,
        "format": "mp3"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, content=text.encode('utf-8'))
        
        if response.status_code == 200:
            result = response.json()
            if str(result.get("error")) != "0":
                if "quota" in str(result.get("message")).lower() or str(result.get("error")) == "1":
                    raise FPTQuotaError(f"Hết dung lượng FPT.AI hoặc lỗi: {result.get('message')}")
                raise Exception(f"FPT API Lỗi: {result.get('message')}")
                
            audio_url = result.get("async")
            if not audio_url:
                raise Exception("Không tìm thấy link async trong phản hồi FPT API")
                
            for _ in range(30):
                await asyncio.sleep(2.0)
                try:
                    audio_res = await client.get(audio_url, timeout=15.0)
                    if audio_res.status_code == 200 and "application/json" not in audio_res.headers.get("Content-Type", ""):
                        with open(output_path, "wb") as f:
                            f.write(audio_res.content)
                        return
                except Exception:
                    pass
        elif response.status_code in [401, 403, 429]:
            raise FPTQuotaError(f"Lỗi API Key FPT hoặc hết dung lượng/rate limit ({response.status_code})")
        else:
            raise Exception(f"Lỗi kết nối FPT API: {response.status_code}")


def trim_audio_silence(audio: AudioSegment, silence_thresh_db: float = -45.0, pad_ms: int = 60) -> AudioSegment:
    """
    Cắt bỏ khoảng lặng thừa ở đầu và cuối file âm thanh TTS một cách an toàn.
    Giữ lại pad_ms (60ms) và ngưỡng -45dB để bảo toàn tuyệt đối phụ âm đầu/đuôi và âm gió tiếng Việt.
    """
    if len(audio) < 100:
        return audio
    try:
        from pydub.silence import detect_leading_silence
        start_trim = detect_leading_silence(audio, silence_threshold=silence_thresh_db)
        end_trim = detect_leading_silence(audio.reverse(), silence_threshold=silence_thresh_db)
        
        start_idx = max(0, start_trim - pad_ms)
        end_idx = len(audio) - max(0, end_trim - pad_ms)
        
        if end_idx > start_idx + 100:
            return audio[start_idx:end_idx]
    except Exception:
        pass
    return audio


async def fit_audio_file(audio_input_path: str, target_max_duration: float, output_path: str = None, *, max_speed=None, allow_emergency: bool = False) -> tuple:
    """
    Căn chỉnh thời lượng audio nếu bị tràn khung đọc, đảm bảo giữ 100% độ thanh thoát, trong trẻo nguyên bản của giọng đọc:
    1. Không nén lại nếu audio đã vừa khung đọc -> giữ nguyên bản file gốc 160kbps (bit-for-bit, 0 transcoding).
    2. Tuyệt đối không ép chậm (slow down) bằng atempo khi audio ngắn hơn khung đọc -> giữ nhịp đọc tự nhiên 1.0x.
    3. Khi cần tăng tốc do câu quá dài: dùng WAV PCM không nén ở khâu trung gian, xuất MP3 192k ở khâu cuối (giữ dải tần số cao).
    4. Cắt khoảng lặng an toàn (pad_ms=60, -45dB) để không bao giờ nuốt âm gió, phụ âm đầu/đuôi.
    If the speed limit cannot fit the cue, retain it for translation condensation.
    Returns: (output_path, measured_duration_s, applied_speed_ratio)
    """
    import shutil
    output_path = output_path or audio_input_path
    from v1_speech_guard import MAX_EMERGENCY_SPEED as _FIT_EMERGENCY_SPEED, load_audio, SpeechTimingError, natural_speed_limit
    if target_max_duration is not None:
        import math
        if not math.isfinite(float(target_max_duration)) or target_max_duration <= 0:
            raise SpeechTimingError("Reading window must be positive and finite")

    audio = None
    orig_dur = None
    try:
        import soundfile as sf
        probed_dur = float(sf.info(str(audio_input_path)).duration)
        if probed_dur > 0:
            orig_dur = probed_dur
    except Exception:
        orig_dur = None
    if orig_dur is None:
        audio = load_audio(audio_input_path)
        orig_dur = len(audio) / 1000.0
    original_duration = orig_dur

    # 1. Nếu audio đã vừa khung đọc -> Giữ nguyên vẹn 100% file gốc CapCut (160kbps), KHÔNG nén lại
    if target_max_duration is None or target_max_duration <= 0 or orig_dur <= target_max_duration:
        if os.path.abspath(audio_input_path) != os.path.abspath(output_path):
            shutil.copy2(audio_input_path, output_path)
        return output_path, orig_dur, 1.0

    # Trim safe silence before computing speed; even small overflow must be fit.
    if audio is None:
        audio = load_audio(audio_input_path)
    audio = trim_audio_silence(audio)
    orig_dur = len(audio) / 1000.0
    ratio = orig_dur / max(0.001, target_max_duration - 0.015)
    applied_ratio = max(1.0, ratio)
    ceiling = _FIT_EMERGENCY_SPEED if allow_emergency else MAX_NATURAL_SPEED
    max_speed = min(ceiling, natural_speed_limit(max_speed))
    if applied_ratio > max_speed:
        if os.path.abspath(audio_input_path) != os.path.abspath(output_path):
            shutil.copy2(audio_input_path, output_path)
        return output_path, original_duration, 1.0
    tempo_filter = build_atempo_filter(applied_ratio)
    base, _ = os.path.splitext(output_path)
    temp_wav_in = f"{base}_fit_in.wav"
    temp_wav_out = f"{base}_fit_out.wav"
    temp_mp3_out = f"{base}_fit_stage.mp3"

    try:
        with audio.export(temp_wav_in, format="wav"):
            pass
        import subprocess
        from batch_control import run
        await asyncio.to_thread(
            run,
            ["ffmpeg", "-y", "-i", temp_wav_in, "-filter:a", tempo_filter, temp_wav_out],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=True, timeout=60
        )
        if os.path.exists(temp_wav_out) and os.path.getsize(temp_wav_out) > 128:
            final_audio = load_audio(temp_wav_out)
            final_audio = trim_audio_silence(final_audio, silence_thresh_db=-45.0, pad_ms=60)
            final_dur = len(final_audio) / 1000.0
            # Xuất MP3 chất lượng cao 192k (bảo tồn trọn vẹn dải treble >10kHz)
            with final_audio.export(temp_mp3_out, format="mp3", bitrate="192k"):
                pass
            import soundfile as sf
            final_dur = float(sf.info(temp_mp3_out).duration)
            os.replace(temp_mp3_out, output_path)
            return output_path, final_dur, applied_ratio
        else:
            raise RuntimeError(f"FFmpeg output file missing or empty: {temp_wav_out}")
    except Exception as exc:
        raise SpeechTimingError("FFmpeg speech fit failed; original audio retained") from exc
    finally:
        for p in (temp_wav_in, temp_wav_out, temp_mp3_out):
            if os.path.exists(p):
                try: os.remove(p)
                except OSError: pass


async def generate_single_tts(segment, output_folder, voice_source, voice_param, api_key, target_max_duration=None):
    validate_tts_source(voice_source)
    import shared_state
    if shared_state.stop_requested:
        raise Exception("Bị hủy bởi lệnh /stop")
        
    text = segment.content.strip()
    if not text or not re.search(r'\w', text):
        return None

    try:
        from ai.v1_tech_pronunciation import normalize_text_for_tts
        text = normalize_text_for_tts(text)
    except Exception:
        pass

    audio_filename = f"{segment.index}.mp3"
    audio_path = os.path.join(output_folder, audio_filename)
    base_audio, ext = os.path.splitext(audio_path)
    temp_raw = f"{base_audio}_raw{ext}"
    
    last_error = None
    for attempt in range(2):
        try:
            if voice_source == "fpt":
                try:
                    await generate_tts_fpt(text, temp_raw, api_key, voice="banmai")
                except FPTQuotaError as q_err:
                    raise TTSIncompleteError("FPT quota exhausted; locked voice is not substituted") from q_err
            elif voice_source == "edge":
                if voice_param == "vi-VN-HoaiMyNeural":
                    await generate_tts_edge(text, temp_raw, voice_param, pitch="+15Hz", rate="+15%")
                else:
                    await generate_tts_edge(text, temp_raw, voice_param, pitch="+0Hz", rate="+5%")
            elif voice_source == "capcut":
                from .v1_voice_prefetch import raw_voice_path
                prepared = raw_voice_path(output_folder, text, voice_param)
                try:
                    os.replace(prepared, temp_raw)
                except FileNotFoundError:
                    await asyncio.to_thread(_run_capcut_tts, text, temp_raw, voice_param)
            elif voice_source in ("vieneu", "vie-tts"):
                from .vieneu_tts_service import generate_tts_vieneu
                await generate_tts_vieneu(text, temp_raw, voice=voice_param)

            # ĐO VÀ CĂN TỐC ĐỘ THEO KHOẢNG ĐỌC MỤC TIÊU (Codex Plan - Điểm 1 & 2)
            _, actual_duration_s, speed_ratio = await fit_audio_file(
                temp_raw, target_max_duration=target_max_duration, output_path=audio_path
            )

            # Lọc trong trẻo (clear filter) cho giọng Hoài My nếu dùng Edge
            if voice_source == "edge" and voice_param == "vi-VN-HoaiMyNeural":
                temp_filtered = f"{base_audio}_filtered{ext}"
                clear_filter = "highpass=f=100,equalizer=f=3500:width_type=q:width=1.5:g=3,treble=g=3,acompressor=threshold=-15dB:ratio=3:attack=5:release=50:makeup=5dB"
                import subprocess, shutil
                await asyncio.to_thread(
                    subprocess.run,
                    ["ffmpeg", "-y", "-i", audio_path, "-filter:a", clear_filter, temp_filtered],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    check=True, timeout=60
                )
                if os.path.exists(temp_filtered):
                    shutil.move(temp_filtered, audio_path)
            
            if os.path.exists(temp_raw):
                try: os.remove(temp_raw)
                except OSError: pass
            
            return {
                "index": segment.index,
                "path": audio_path,
                "start": segment.start.total_seconds(),
                "end": segment.start.total_seconds() + actual_duration_s,
                "actual_audio_duration": actual_duration_s,
                "speed_ratio": speed_ratio,
                "content": text
            }
        except Exception as e:
            from v1_speech_guard import SpeechTimingError
            if isinstance(e, SpeechTimingError) or shared_state.stop_requested:
                raise
            last_error = e
            logger.warning("[generate_single_tts] Loi TTS doan %s (Lan %s): %s", segment.index, attempt + 1, e)
            if os.path.exists(temp_raw):
                try: os.remove(temp_raw)
                except OSError: pass
            if "failed after" in str(e):
                break
            await asyncio.sleep(1.5)
            
    raise TTSIncompleteError(f"Locked-voice TTS failed for cue {segment.index}; successful audio retained") from last_error


from v1_speech_guard import MAX_EMERGENCY_SPEED, natural_speed_limit

MAX_NATURAL_SPEED = natural_speed_limit()

@stage("voice")
async def generate_dubbing_audio(translated_segments, output_folder, voice_source="edge", voice_param="vi-VN-HoaiMyNeural", api_key="", video_duration=None, segment_voices=None, script_mode="default", condensation_api_key=None):
    from .v1_translation_meaning import ensure_translation_speech_integrity
    ensure_translation_speech_integrity(translated_segments)
    validate_tts_source(voice_source)
    for seg in translated_segments:
        if segment_voices:
            info = segment_voices.get(str(seg.index))
            if not info or not info.get("source") or not info.get("param"):
                raise TTSIncompleteError(f"Missing locked voice for cue {seg.index}")
            validate_tts_source(info["source"])
        else:
            validate_tts_source(getattr(seg, "voice_source", None) or voice_source)
    script_mode = script_mode or os.getenv("SCRIPT_MODE", "default")
    print(f"Generating TTS for dubbing using {voice_source} (Anti-Overlap Enabled, script_mode={script_mode})...")
    os.makedirs(output_folder, exist_ok=True)
    
    from .v1_voice_cache import voice_cache_key, read_voice_cache, write_voice_cache
    from .translation import _contains_cjk, translate_subtitles
    pending_translation = [s for s in translated_segments if _contains_cjk(s.content)]
    if pending_translation:
        await asyncio.to_thread(translate_subtitles, pending_translation,
                                target_lang="vi", strict=True, enable_g4f=False, script_mode=script_mode)
        if any(_contains_cjk(s.content) for s in pending_translation):
            raise RuntimeError("Translation incomplete; Vietnamese TTS was not started")
        ensure_translation_speech_integrity(translated_segments)

    import time
    condense_remaining = 45.0
    condensation_failures = {}
    condensation_api_key = (condensation_api_key if condensation_api_key is not None else api_key)
    condensation_api_key = condensation_api_key or os.getenv("GEMINI_API_KEY", "")
    import v1_speech_guard as _speech_guard_mod
    from v1_speech_guard import coalesce_tiny_same_voice_cues, validate_speech_timeline, SpeechTimingError, vietnamese_spoken_seconds
    from config.env import env_flag
    emergency_enabled = env_flag("V1_ALLOW_EMERGENCY_SPEED", True)
    emergency_speed_cap = float(globals().get("MAX_EMERGENCY_SPEED") or getattr(_speech_guard_mod, "MAX_EMERGENCY_SPEED", 1.75))
    allow_speech_truncation = env_flag("V1_ALLOW_SPEECH_TRUNCATION", True)
    if segment_voices:
        for seg in translated_segments:
            info = segment_voices[str(seg.index)]
            seg.voice_source, seg.voice_param = info["source"], info["param"]
            seg.gender = info.get("gender", "unknown")
    coalesce_tiny_same_voice_cues(translated_segments)

    # 1. TÍNH KHOẢNG ĐỌC TỐI ĐA CHO TỪNG CÂU (Codex Plan - Điểm 1)
    windows = calculate_reading_windows(translated_segments, video_duration=video_duration, gap_s=0.05)
    
    # 2. VÒNG 1: RÚT GỌN THEO LÔ TRƯỚC TTS DỰA TRÊN DỰ ĐOÁN (Codex Plan - Điểm 3 & Đợt 5)
    # Dự đoán độ dài dựa trên số từ (trung bình 0.28s mỗi từ tiếng Việt).
    # Gom một lượt gọi Gemini duy nhất để rút gọn toàn bộ câu quá dài trước khi render.
    overly_long_items = []
    for seg in translated_segments:
        text = seg.content.strip()
        hard_max = windows.get(seg.index, 3.0)
        est_duration = max(1.0, vietnamese_spoken_seconds(text))
        if hard_max > 0 and (est_duration / hard_max) > MAX_NATURAL_SPEED:
            comfortable_speed = min(1.25, max(1.0, float(MAX_NATURAL_SPEED) * 0.85))
            target_words = max(2, int(round((hard_max - 0.05) * comfortable_speed / 0.28)))
            cur_words = len(text.split())
            hard_fit_words = max(target_words, int(round((hard_max - 0.05) * float(MAX_NATURAL_SPEED) / 0.28)))
            max_accept_words = max(target_words, min(cur_words - 1, hard_fit_words + 1)) if cur_words > 2 else target_words
            overly_long_items.append({
                "index": seg.index,
                "text": text,
                "target_seconds": hard_max,
                "target_words": target_words,
                "max_accept_words": max_accept_words,
                "current_seconds": est_duration
            })

    limiter = asyncio.Semaphore(int(os.getenv("V1_TTS_WORKERS", "6")))
    async def run_one(seg, write_cache=True):
        async with limiter:
            if not seg.content.strip() or not re.search(r'\w', seg.content):
                return None
            seg_source = getattr(seg, "voice_source", None) or voice_source
            seg_param = getattr(seg, "voice_param", None) or voice_param
            hard_max = windows.get(seg.index, (seg.end - seg.start).total_seconds())
            key = voice_cache_key(seg, seg_source, seg_param, max_duration=hard_max)
            path = os.path.join(output_folder, f"{seg.index}.mp3")
            duration = read_voice_cache(path, key, seg.content.strip(), max_duration=hard_max)
            if duration is not None:
                return dict(index=seg.index, path=path, start=seg.start.total_seconds(),
                            end=seg.start.total_seconds() + duration, actual_audio_duration=duration,
                            content=seg.content.strip())
            
            result = None
            for attempt in range(4):
                if __import__("shared_state").stop_requested:
                    raise TTSIncompleteError("TTS stopped before the next locked-voice attempt")
                if attempt:
                    await asyncio.sleep(1.2 * attempt)
                try:
                    result = await generate_single_tts(
                        seg, output_folder, seg_source, seg_param, api_key, hard_max
                    )
                    if result is not None:
                        break
                except Exception as tts_err:
                    if isinstance(tts_err, SpeechTimingError):
                        raise
                    logger.warning("[TTS_RETRY] Cue %s, locked voice %s/%s, attempt %s: %s",
                                   seg.index, seg_source, seg_param, attempt + 1, tts_err)

            if result is None:
                if os.path.exists(path):
                    try: os.remove(path)
                    except OSError: pass
                # Cho phép ghi nhận lỗi để đánh giá dung sai tổng thể thay vì raise ngay
                return dict(
                    index=seg.index, path=path, start=seg.start.total_seconds(),
                    end=seg.start.total_seconds() + hard_max, actual_audio_duration=hard_max,
                    content=seg.content.strip(), error=f"TTS segment {seg.index} thất bại với giọng đã khóa {seg_source}/{seg_param}"
                )
            
            # Ghi nhận cache version 8
            if os.path.exists(path) and os.path.getsize(path) > 128 and result.get("actual_audio_duration", 0) > 0.1:
                if write_cache and result["actual_audio_duration"] <= hard_max + 0.005 and result.get("speed_ratio", 1.0) <= MAX_NATURAL_SPEED:
                    write_voice_cache(path, key, result["actual_audio_duration"], seg.content.strip())
            else:
                if os.path.exists(path):
                    try: os.remove(path)
                    except OSError: pass
                return dict(
                    index=seg.index, path=path, start=seg.start.total_seconds(),
                    end=seg.start.total_seconds() + hard_max, actual_audio_duration=hard_max,
                    content=seg.content.strip(), error=f"Failed segment: {seg.index} (Audio invalid or too short)"
                )
            return result

    if overly_long_items and condense_remaining > 2.0:
        long_indices = {item["index"] for item in overly_long_items}
        # Khởi chạy ngay lập tức các câu độ dài bình thường song song với lúc chờ Gemini rút gọn câu dài
        task_map = {
            i: asyncio.create_task(run_one(seg))
            for i, seg in enumerate(translated_segments)
            if seg.index not in long_indices
        }
        print(f"[VOICE_GUARD] Vòng 1: Gom {len(overly_long_items)} câu dự kiến quá dài (> {MAX_NATURAL_SPEED}x), rút gọn song song khi đang đọc {len(task_map)} câu thường...")
        from .translation import condense_vietnamese_subtitles_batch
        condense_started = time.monotonic()
        condensed_map = await asyncio.to_thread(
            condense_vietnamese_subtitles_batch,
            overly_long_items,
            api_key=condensation_api_key or os.getenv("GEMINI_API_KEY", ""),
            deadline=condense_started + min(25.0, condense_remaining),
            stop_checker=lambda: bool(__import__('shared_state').stop_requested),
            job_id="round1_pre_tts",
            failure_causes=condensation_failures,
            script_mode=script_mode
        )
        condense_remaining = max(0.0, condense_remaining - (time.monotonic() - condense_started))
        for i, seg in enumerate(translated_segments):
            if seg.index in condensed_map:
                old_text = seg.content
                seg.content = condensed_map[seg.index]
                print(f"[VOICE_GUARD] Đã rút gọn câu #{seg.index}: '{old_text[:35]}...' -> '{seg.content}'")
            if i not in task_map:
                task_map[i] = asyncio.create_task(run_one(seg))
        ordered_tasks = [task_map[i] for i in range(len(translated_segments))]
        results = await asyncio.gather(*ordered_tasks, return_exceptions=True)
    else:
        results = await asyncio.gather(*(run_one(seg) for seg in translated_segments), return_exceptions=True)
    exceptions = [r for r in results if isinstance(r, BaseException)]
    failed_items = [r for r in results if isinstance(r, dict) and r.get("error")]
    total_failures = len(exceptions) + len(failed_items)
    if total_failures:
        first_err = exceptions[0] if exceptions else TTSIncompleteError(failed_items[0]["error"])
        raise TTSIncompleteError(f"TTS incomplete: {total_failures} subtitle(s); successful audio retained") from first_err

    # 4. VÒNG 2: ĐO THỜI LƯỢNG THỰC TẾ & GOM RÚT GỌN BỔ SUNG (Codex Plan - Đợt 5)
    # Gom các câu vẫn vượt trần tốc độ sau TTS lần 1 để rút gọn cùng một lượt.
    res_by_idx = {r["index"]: r for r in results if r is not None and isinstance(r, dict)}
    # CapCut and FFmpeg may return a few extra MP3 frames even after the first
    # fit. Refit only those measured files, sequentially, before considering
    # another Gemini rewrite or mixing. Keep the locked voice and every word.
    for seg in translated_segments:
        r = res_by_idx.get(seg.index)
        if not r:
            continue
        hard_max = windows.get(seg.index, 3.0)
        measured = float(r.get("actual_audio_duration", 0.0))
        if measured <= hard_max + 0.005:
            continue
        previous_speed = float(r.get("speed_ratio", 1.0))
        if previous_speed >= MAX_NATURAL_SPEED:
            continue
        try:
            _, fitted_duration, extra_speed = await fit_audio_file(
                r["path"], hard_max, r["path"], max_speed=MAX_NATURAL_SPEED / previous_speed
            )
        except Exception as exc:
            logger.warning("[VOICE_GUARD] Cue %s measured refit failed: %s", seg.index, exc)
            continue
        r["actual_audio_duration"] = fitted_duration
        r["end"] = float(r["start"]) + fitted_duration
        r["speed_ratio"] = previous_speed * extra_speed
        if fitted_duration <= hard_max + 0.005 and r["speed_ratio"] <= MAX_NATURAL_SPEED:
            seg_source = getattr(seg, "voice_source", None) or voice_source
            seg_param = getattr(seg, "voice_param", None) or voice_param
            key = voice_cache_key(seg, seg_source, seg_param, max_duration=hard_max)
            write_voice_cache(r["path"], key, fitted_duration, seg.content.strip())

    post_tts_overly_long = []
    for seg in translated_segments:
        if seg.index not in res_by_idx:
            continue
        r = res_by_idx[seg.index]
        dur = r.get("actual_audio_duration", 0.0)
        hard_max = windows.get(seg.index, 3.0)
        speed_ratio = r.get("speed_ratio", 1.0)
        if hard_max > 0 and (dur > hard_max + 0.005 or speed_ratio > MAX_NATURAL_SPEED):
            if hard_max < 1.0:
                target_words = max(1, int(hard_max / 0.35))
            else:
                target_words = max(2, int((hard_max - 0.05) / 0.28))
            cur_words = len(seg.content.strip().split())
            max_accept_words = target_words
            if dur > hard_max and cur_words > 2:
                # Use the selected voice's measured pace, including any tempo
                # already applied to this file. Leave headroom below the cap.
                preferred_speed = min(MAX_NATURAL_SPEED, 1.25)
                measured_words = cur_words * (hard_max - 0.05) * preferred_speed / (dur * speed_ratio)
                target_words = max(2, min(cur_words - 1, int(measured_words)))
                hard_measured_words = cur_words * (hard_max - 0.05) * float(MAX_NATURAL_SPEED) / (dur * speed_ratio)
                max_accept_words = max(target_words, min(cur_words - 1, int(hard_measured_words) + 1))
            post_tts_overly_long.append({
                "index": seg.index,
                "text": seg.content.strip(),
                "target_seconds": hard_max,
                "target_words": target_words,
                "max_accept_words": max_accept_words,
                "current_seconds": dur
            })

    if post_tts_overly_long and condense_remaining > 2.0:
        print(f"[VOICE_GUARD] Vòng 2 (Bổ sung): Phát hiện {len(post_tts_overly_long)} câu thực tế vẫn dài, gom 1 lượt gọi Gemini...")
        from .translation import condense_vietnamese_subtitles_batch
        condense_started = time.monotonic()
        condensed_map_round2 = await asyncio.to_thread(
            condense_vietnamese_subtitles_batch,
            post_tts_overly_long,
            api_key=condensation_api_key or os.getenv("GEMINI_API_KEY", ""),
            deadline=condense_started + condense_remaining,
            stop_checker=lambda: bool(__import__('shared_state').stop_requested),
            job_id="round2_post_tts",
            failure_causes=condensation_failures,
            script_mode=script_mode
        )
        condense_remaining = max(0.0, condense_remaining - (time.monotonic() - condense_started))
        cues_to_re_render = []
        for seg in translated_segments:
            if seg.index in condensed_map_round2 and condensed_map_round2[seg.index] != seg.content:
                old_t = seg.content
                seg.content = condensed_map_round2[seg.index]
                print(f"[VOICE_GUARD] Đã rút gọn bổ sung câu #{seg.index}: '{old_t[:35]}...' -> '{seg.content}'")
                cues_to_re_render.append(seg)

        # Chỉ tạo lại TTS cho những câu THỰC SỰ thay đổi nội dung
        if cues_to_re_render:
            re_results = await asyncio.gather(*(run_one(seg) for seg in cues_to_re_render), return_exceptions=True)
            re_errors = [r for r in re_results if isinstance(r, BaseException)]
            re_errors.extend(TTSIncompleteError(r["error"]) for r in re_results
                             if isinstance(r, dict) and r.get("error"))
            if re_errors:
                raise TTSIncompleteError("Condensed TTS regeneration failed; stale dialogue is not published") from re_errors[0]
            for r in re_results:
                if r and isinstance(r, dict):
                    res_by_idx[r["index"]] = r

    import soundfile as sf

    # A rewrite can meet the word-count target but still take too long when
    # spoken by the selected voice. Measure the regenerated files and give only
    # unresolved, genuinely long cues one stricter, bounded rewrite attempt.
    # The final hard guard below remains authoritative and never clips speech.
    final_condense_items = []
    for seg in translated_segments:
        r = res_by_idx.get(seg.index)
        if not r or not r.get("path"):
            continue
        try:
            measured_duration = float(sf.info(r["path"]).duration)
        except Exception:
            continue
        hard_max = windows.get(seg.index, 3.0)
        cur_words = len(seg.content.strip().split())
        if measured_duration <= hard_max + 0.005 or cur_words <= 2:
            continue
        previous_speed = float(r.get("speed_ratio", 1.0))
        preferred_speed = min(MAX_NATURAL_SPEED, 1.10)
        measured_words = cur_words * (hard_max - 0.05) * preferred_speed / (measured_duration * previous_speed)
        target_words = max(2, min(cur_words - 1, int(measured_words)))
        rescue_speed = emergency_speed_cap if emergency_enabled else float(MAX_NATURAL_SPEED)
        hard_measured_words = cur_words * (hard_max - 0.05) * rescue_speed / (measured_duration * previous_speed)
        max_accept_words = max(target_words, min(cur_words - 1, int(hard_measured_words)))
        final_condense_items.append({
            "index": seg.index,
            "text": seg.content.strip(),
            "target_seconds": hard_max,
            "target_words": target_words,
            "max_accept_words": max_accept_words,
            "current_seconds": measured_duration,
        })

    if final_condense_items and condense_remaining > 2.0:
        logger.info("[VOICE_GUARD] Final bounded rewrite for %s measured overlong cue(s)",
                    len(final_condense_items))
        from .translation import condense_vietnamese_subtitles_batch
        condense_started = time.monotonic()
        condensed_map_final = await asyncio.to_thread(
            condense_vietnamese_subtitles_batch,
            final_condense_items,
            api_key=condensation_api_key or os.getenv("GEMINI_API_KEY", ""),
            deadline=condense_started + min(15.0, condense_remaining),
            stop_checker=lambda: bool(__import__('shared_state').stop_requested),
            job_id="round3_final_tts",
            failure_causes=condensation_failures,
            script_mode=script_mode,
        )
        condense_remaining = max(0.0, condense_remaining - (time.monotonic() - condense_started))
        cues_to_re_render = []
        for seg in translated_segments:
            condensed = condensed_map_final.get(seg.index)
            if condensed and condensed != seg.content:
                logger.info("[VOICE_GUARD] Final rewrite applied to cue %s (%s -> %s words)",
                            seg.index, len(seg.content.split()), len(condensed.split()))
                seg.content = condensed
                cues_to_re_render.append(seg)

        if cues_to_re_render:
            re_results = await asyncio.gather(*(run_one(seg) for seg in cues_to_re_render), return_exceptions=True)
            re_errors = [r for r in re_results if isinstance(r, BaseException)]
            re_errors.extend(TTSIncompleteError(r["error"]) for r in re_results
                             if isinstance(r, dict) and r.get("error"))
            if re_errors:
                raise TTSIncompleteError("Final condensed TTS regeneration failed; stale dialogue is not published") from re_errors[0]
            for r in re_results:
                if r and isinstance(r, dict):
                    res_by_idx[r["index"]] = r

    # Final Hard Guard: Ensure EVERY single audio cue strictly fits its reading window
    # to guarantee zero audio overlap in the mixer.
    sorted_active_segs = sorted([s for s in translated_segments if s.index in res_by_idx], key=lambda s: s.start)
    for k, seg in enumerate(sorted_active_segs):
        r = res_by_idx.get(seg.index)
        if not r:
            continue
        cur_start = float(r.get("start", seg.start.total_seconds()))
        # Measure the artifact itself; cached labels cannot authorize clipping.
        dur = float(sf.info(r["path"]).duration)
        r["actual_audio_duration"] = dur
        r["end"] = cur_start + dur
        previous_speed = float(r.get("speed_ratio", 1.0))

        # Determine true boundaries on the timeline
        prev_end = 0.0 if k == 0 else (
            float(res_by_idx[sorted_active_segs[k - 1].index].get("start", sorted_active_segs[k - 1].start.total_seconds())) +
            float(res_by_idx[sorted_active_segs[k - 1].index].get("actual_audio_duration", 0.0))
        )
        next_start = (
            float(sorted_active_segs[k + 1].start.total_seconds())
            if k + 1 < len(sorted_active_segs)
            else (video_duration if video_duration is not None else cur_start + dur + 1.0)
        )

        # 1. Borrow pre-silence if cue overflows next boundary
        min_allowed_start = prev_end + 0.05 if k > 0 else 0.0
        available_pre_gap = max(0.0, cur_start - min_allowed_start)
        max_end_allowed = next_start - 0.05
        if cur_start + dur > max_end_allowed + 0.005 and available_pre_gap > 0:
            overflow = (cur_start + dur) - max_end_allowed
            shift = min(available_pre_gap, overflow)
            cur_start -= shift
            r["start"] = cur_start
            r["end"] = cur_start + dur
            logger.info("[VOICE_GUARD] Shifted cue %s earlier by %.3fs into pre-silence (new start: %.3f)",
                        seg.index, shift, cur_start)

        hard_max = max(0.05, next_start - cur_start - 0.05)
        windows[seg.index] = hard_max

        # 2. Standard tempo fit within speed limit
        if dur > hard_max + 0.005 and previous_speed < MAX_NATURAL_SPEED:
            try:
                _, fitted_dur, extra_spd = await fit_audio_file(
                    r["path"], hard_max, r["path"], max_speed=MAX_NATURAL_SPEED / previous_speed
                )
                fitted_dur = float(sf.info(r["path"]).duration)
                r["actual_audio_duration"] = fitted_dur
                r["end"] = cur_start + fitted_dur
                r["speed_ratio"] = previous_speed * extra_spd
                dur = fitted_dur
                logger.info("[VOICE_GUARD] Cue %s after final fit: duration=%.3fs, window=%.3fs",
                            seg.index, fitted_dur, hard_max)
            except Exception as exc:
                logger.warning("[VOICE_GUARD] Final fit for cue %s warning: %s", seg.index, exc)

        # 2b. Bounded emergency tempo fit (never cached) when cue still overflows slightly
        dur = float(sf.info(r["path"]).duration)
        previous_speed = float(r.get("speed_ratio", 1.0))
        if emergency_enabled and dur > hard_max + 0.005 and previous_speed < emergency_speed_cap:
            try:
                _, fitted_dur, extra_spd = await fit_audio_file(
                    r["path"], hard_max, r["path"],
                    max_speed=emergency_speed_cap / previous_speed,
                    allow_emergency=True,
                )
                fitted_dur = float(sf.info(r["path"]).duration)
                r["actual_audio_duration"] = fitted_dur
                r["end"] = cur_start + fitted_dur
                r["speed_ratio"] = previous_speed * extra_spd
                dur = fitted_dur
                if extra_spd > 1.0 + 1e-6:
                    logger.warning(
                        "[VOICE_GUARD] Emergency tempo fit applied to cue %s (speed=%.2fx, duration=%.3fs, window=%.3fs)",
                        seg.index, r["speed_ratio"], fitted_dur, hard_max,
                    )
            except Exception as exc:
                logger.warning("[VOICE_GUARD] Emergency fit for cue %s warning: %s", seg.index, exc)

        # Condensation and bounded tempo fitting have already had their chance.
        # Retain every spoken sample when the remaining window cannot fit it.
        dur = float(sf.info(r["path"]).duration)
        r["actual_audio_duration"] = dur
        r["end"] = cur_start + dur
        if dur > hard_max + 0.005:
            from v1_retry_policy import is_transient_network_error

            failure = condensation_failures.get(seg.index)
            if failure is not None and is_transient_network_error(failure):
                raise TTSIncompleteError(
                    f"Speech cue {seg.index} still exceeds its reading window after a temporary condensation failure; audio retained"
                ) from failure
            cur_words = len(seg.content.split())
            truncated = False
            if allow_speech_truncation and cur_words > 1:
                original_text = seg.content
                trunc_speed_cap = emergency_speed_cap if emergency_enabled else MAX_NATURAL_SPEED
                prev_k = None
                for _trunc_attempt in range(3):
                    speed_ratio = float(r.get("speed_ratio", 1.0))
                    raw = dur * speed_ratio
                    k_words = int(cur_words * (hard_max - 0.05) * trunc_speed_cap / raw) if raw > 0 else 1
                    k_words = max(1, min(k_words, cur_words - 1))
                    if prev_k is not None:
                        if k_words >= prev_k:
                            k_words = prev_k - 1
                        if k_words < 1:
                            break
                    new_text = " ".join(original_text.split()[:k_words]).rstrip(",;:-–")
                    if not new_text:
                        break
                    seg.content = new_text
                    try:
                        new_r = await run_one(seg, write_cache=False)
                    except Exception as exc:
                        raise TTSIncompleteError(
                            "Truncated TTS regeneration failed; stale dialogue is not published"
                        ) from exc
                    if not isinstance(new_r, dict) or new_r.get("error"):
                        raise TTSIncompleteError(
                            "Truncated TTS regeneration failed; stale dialogue is not published"
                        )
                    res_by_idx[seg.index] = new_r
                    r = new_r
                    r.setdefault("truncated_from", original_text)
                    dur = float(sf.info(r["path"]).duration)
                    r["actual_audio_duration"] = dur
                    r["start"] = cur_start
                    r["end"] = cur_start + dur
                    previous_speed = float(r.get("speed_ratio", 1.0))
                    if dur > hard_max + 0.005 and previous_speed < trunc_speed_cap:
                        try:
                            _, fitted_dur, extra_spd = await fit_audio_file(
                                r["path"], hard_max, r["path"],
                                max_speed=trunc_speed_cap / previous_speed,
                                allow_emergency=emergency_enabled,
                            )
                            fitted_dur = float(sf.info(r["path"]).duration)
                            r["actual_audio_duration"] = fitted_dur
                            r["end"] = cur_start + fitted_dur
                            r["speed_ratio"] = previous_speed * extra_spd
                            dur = fitted_dur
                        except Exception as exc:
                            logger.warning("[VOICE_GUARD] Truncated fit for cue %s warning: %s", seg.index, exc)
                    prev_k = k_words
                    if dur <= hard_max + 0.005:
                        truncated = True
                        logger.warning(
                            "[VOICE_GUARD] Last-resort truncation applied to cue %s (%s -> %s words, duration=%.3fs, window=%.3fs)",
                            seg.index, cur_words, k_words, dur, hard_max,
                        )
                        break
            if not truncated:
                raise SpeechTimingError(
                    f"Speech cue {seg.index} requires {dur:.3f}s but its reading window "
                    f"is {hard_max:.3f}s; shorten translation before retry; audio retained"
                )
        speed_ceiling = emergency_speed_cap if emergency_enabled else MAX_NATURAL_SPEED
        if float(r.get("speed_ratio", 1.0)) > speed_ceiling + 1e-9:
            raise SpeechTimingError(
                f"Speech cue {seg.index} exceeds the natural speed limit; "
                "shorten translation before retry; audio retained"
            )

    final_results = [res_by_idx[seg.index] for seg in translated_segments if seg.index in res_by_idx]
    validate_speech_timeline(final_results, total_duration=video_duration, max_speed=MAX_NATURAL_SPEED)
    return final_results
