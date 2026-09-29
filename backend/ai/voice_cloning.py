import os
import asyncio
import re
import threading
import edge_tts
try:
    from ..v1_stage_metrics import stage
except ImportError:
    from v1_stage_metrics import stage
from pydub import AudioSegment

edge_semaphore = asyncio.Semaphore(2)
capcut_semaphore = threading.Semaphore(2)
rvc_semaphore = asyncio.Semaphore(1)
global_rvc_instance = None
global_rvc_model_path = None


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
    filters.append(f"atempo={current:.3f}")
    return ",".join(filters)


def rvc_runtime_available():
    """Return whether the optional legacy RVC engine can actually be imported."""

    import importlib.util

    return importlib.util.find_spec("rvc_python") is not None


def discover_rvc_index(model_path):
    """Find a real RVC feature index, including training-prefixed names."""
    from pathlib import Path

    model = Path(model_path)
    exact = model.with_suffix(".index")
    if exact.is_file() and exact.stat().st_size > 1024:
        return str(exact)
    matches = [
        candidate
        for candidate in model.parent.glob("*.index")
        if model.stem.lower() in candidate.stem.lower()
        and candidate.stat().st_size > 1024
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda candidate: (
            0 if candidate.name.lower().startswith("added_") else 1,
            candidate.name.lower(),
        )
    )
    return str(matches[0])

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
):
    async with edge_semaphore:
        last_error = None
        for attempt in range(max(1, int(attempts))):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
                communicate = edge_tts.Communicate(
                    text, voice, rate=rate, pitch=pitch
                )
                await asyncio.wait_for(communicate.save(output_path), timeout=35.0)
                if not os.path.isfile(output_path) or os.path.getsize(output_path) < 128:
                    raise RuntimeError("Edge TTS returned empty audio")
                return
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
                    "Edge TTS retry {}/{} after {}: {}".format(
                        attempt + 2, attempts, type(exc).__name__, exc
                    )
                )
                await asyncio.sleep(max(0.0, float(delay)))
        raise RuntimeError("Edge TTS failed after {} attempts".format(attempts)) from last_error

_shared_capcut_client = None

def _get_capcut_client():
    global _shared_capcut_client
    if _shared_capcut_client is None:
        from capcut_tts_api import CapCutClient
        _shared_capcut_client = CapCutClient()
    return _shared_capcut_client

@stage("tts_capcut_attempt")
def _run_capcut_tts_once(
    text, output_path, voice="BV562_streaming", poll_interval=1.0
):
    import json, time
    client = _get_capcut_client()
    
    res = client.create_tts_task(texts=text, voice=voice)
    task_id = res["data"]["tasks"][0]["id"]
    token = res["data"]["tasks"][0]["token"]
    
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        time.sleep(min(max(0.0, float(poll_interval)),
                       max(0.0, deadline - time.monotonic())))
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
            
    raise TimeoutError("CapCut TTS polling exceeded 45s (network calls may add time)")


def _run_capcut_tts(
    text,
    output_path,
    voice="BV562_streaming",
    attempts=3,
    retry_delays=(1.5, 3.0),
    poll_interval=1.0,
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

rvc_semaphore = asyncio.Semaphore(1)

@stage("rvc_including_queue")
async def apply_rvc_clone(
    input_audio,
    output_audio,
    model_path,
    strict=False,
    index_path=None,
    **kwargs,
):
    async with rvc_semaphore:
        print(f"Applying RVC model from {model_path} to {input_audio}...")
        import traceback

        if not rvc_runtime_available():
            if strict:
                raise RuntimeError("RVC runtime is unavailable")
            import shutil

            print("⚠️ rvc-python chưa sẵn sàng; dùng trực tiếp giọng TTS dự phòng.")
            shutil.copy(input_audio, output_audio)
            return

        try:
            if os.path.exists(output_audio):
                os.remove(output_audio)
        except OSError:
            pass
        
        @stage("rvc_execution_excluding_queue")
        def run_rvc_with_method(method="rmvpe"):
            global global_rvc_instance, global_rvc_model_path
            import torch
            torch.backends.cudnn.enabled = False
            from rvc_python.infer import RVCInference
            if global_rvc_instance is None:
                if not torch.cuda.is_available():
                    raise RuntimeError("V1 requires CUDA for RVC; CPU fallback disabled")
                global_rvc_instance = RVCInference(device="cuda:0")
            
            if global_rvc_model_path != model_path:
                print("=> Nap model RVC vao VRAM (Chi chay 1 lan duy nhat)...")
                resolved_index = index_path or discover_rvc_index(model_path)
                if resolved_index:
                    global_rvc_instance.load_model(
                        model_path, version="v2", index_path=resolved_index
                    )
                else:
                    global_rvc_instance.load_model(model_path, version="v2")
                global_rvc_model_path = model_path
            global_rvc_instance.set_params(f0up_key=0, f0method=method, index_rate=0.6, protect=0.1, filter_radius=3, rms_mix_rate=0.25)
            global_rvc_instance.infer_file(input_audio, output_audio)

        success = False
        # Thử lần 1 bằng rmvpe
        try:
            await asyncio.to_thread(run_rvc_with_method, "rmvpe")
            if os.path.exists(output_audio) and os.path.getsize(output_audio) > 100:
                success = True
        except Exception as e:
            print(f"=> Lỗi RVC (rmvpe): {e}. Đang thử lại với phương pháp pm (Parselmouth)...")

        # Thử lần 2 bằng pm nếu rmvpe bị lỗi (âm thanh quá ngắn hoặc không bắt được cao độ)
        if not success:
            try:
                await asyncio.to_thread(run_rvc_with_method, "pm")
                if os.path.exists(output_audio) and os.path.getsize(output_audio) > 100:
                    success = True
                    print(f"=> RVC Cloning (pm) thành công!")
            except Exception as e:
                print(f"=> Lỗi RVC (pm): {e}")

        # Thử lần 3 bằng harvest
        if not success:
            try:
                await asyncio.to_thread(run_rvc_with_method, "harvest")
                if os.path.exists(output_audio) and os.path.getsize(output_audio) > 100:
                    success = True
                    print(f"=> RVC Cloning (harvest) thành công!")
            except Exception as e:
                print(f"=> Lỗi RVC (harvest): {e}")

        if not success:
            print(f"⚠️ CẢNH BÁO: RVC thất bại cả 3 phương pháp. Giữ file gốc.")
            if strict:
                try:
                    if os.path.exists(output_audio):
                        os.remove(output_audio)
                except OSError:
                    pass
                raise RuntimeError("RVC conversion failed")
            import shutil
            shutil.copy(input_audio, output_audio)
        else:
            print(f"=> RVC Cloning Successful cho file {output_audio}")

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


def calculate_reading_windows(segments, video_duration=None, gap_s=0.05) -> dict:
    """
    Tính khoảng đọc tối đa cho từng câu theo mốc kết thúc, câu kế và kết thúc video (Codex Plan - Điểm 1).
    Chừa khoảng chuyển câu nhỏ gap_s (0.05s).
    Tuyệt đối không ép sàn cứng max(0.3, ...) khi câu sau bắt đầu sát hơn, nhằm ngăn chặn hoàn toàn
    nguy cơ đè giọng khi các câu ở cự ly hẹp.
    """
    windows = {}
    n = len(segments)
    for i, seg in enumerate(segments):
        start_s = seg.start.total_seconds()
        end_s = seg.end.total_seconds()
        if i + 1 < n:
            next_start_s = segments[i + 1].start.total_seconds()
            if next_start_s > start_s:
                hard_max_s = max(0.05, next_start_s - start_s - gap_s)
            else:
                hard_max_s = max(0.05, end_s - start_s)
        else:
            if video_duration is not None and video_duration > start_s + gap_s:
                hard_max_s = max(0.05, video_duration - start_s - gap_s)
            else:
                hard_max_s = max(0.05, end_s - start_s + 1.0)
        windows[seg.index] = hard_max_s
    return windows


async def fit_audio_file(audio_input_path: str, target_max_duration: float, output_path: str = None) -> tuple:
    """
    Căn chỉnh thời lượng audio nếu bị tràn khung đọc, đảm bảo giữ 100% độ thanh thoát, trong trẻo nguyên bản của giọng đọc:
    1. Không nén lại nếu audio đã vừa khung đọc -> giữ nguyên bản file gốc 160kbps (bit-for-bit, 0 transcoding).
    2. Tuyệt đối không ép chậm (slow down) bằng atempo khi audio ngắn hơn khung đọc -> giữ nhịp đọc tự nhiên 1.0x.
    3. Khi cần tăng tốc do câu quá dài: dùng WAV PCM không nén ở khâu trung gian, xuất MP3 192k ở khâu cuối (giữ dải tần số cao).
    4. Cắt khoảng lặng an toàn (pad_ms=60, -45dB) để không bao giờ nuốt âm gió, phụ âm đầu/đuôi.
    Returns: (output_path, final_duration_s, speed_ratio)
    """
    import shutil
    output_path = output_path or audio_input_path
    audio = AudioSegment.from_file(audio_input_path)
    orig_dur = len(audio) / 1000.0

    # 1. Nếu audio đã vừa khung đọc -> Giữ nguyên vẹn 100% file gốc CapCut (160kbps), KHÔNG nén lại
    if target_max_duration is None or target_max_duration <= 0 or orig_dur <= target_max_duration:
        if os.path.abspath(audio_input_path) != os.path.abspath(output_path):
            shutil.copy2(audio_input_path, output_path)
        return output_path, orig_dur, 1.0

    # 2. Nếu tràn khung đọc nhưng độ lệch cực nhỏ (<= 3% hoặc <= 0.06s), giữ nguyên bản để tránh méo tiếng
    ratio = orig_dur / target_max_duration
    if ratio <= 1.03:
        if os.path.abspath(audio_input_path) != os.path.abspath(output_path):
            shutil.copy2(audio_input_path, output_path)
        return output_path, orig_dur, 1.0

    # 3. Chỉ khi audio thực sự tràn câu sau (ratio > 1.03), mới tăng tốc nhẹ bằng atempo
    # Dùng WAV PCM trung gian để KHÔNG bị suy hao chất lượng nén MP3
    base, _ = os.path.splitext(output_path)
    temp_wav_in = f"{base}_fit_in.wav"
    temp_wav_out = f"{base}_fit_out.wav"
    tempo_filter = build_atempo_filter(ratio)

    try:
        audio.export(temp_wav_in, format="wav")
        import subprocess
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-i", temp_wav_in, "-filter:a", tempo_filter, temp_wav_out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=True, timeout=60
        )
        if os.path.exists(temp_wav_out) and os.path.getsize(temp_wav_out) > 128:
            final_audio = AudioSegment.from_file(temp_wav_out)
            final_audio = trim_audio_silence(final_audio, silence_thresh_db=-45.0, pad_ms=60)
            final_dur = len(final_audio) / 1000.0
            # Xuất MP3 chất lượng cao 192k (bảo tồn trọn vẹn dải treble >10kHz)
            final_audio.export(output_path, format="mp3", bitrate="192k")
            return output_path, final_dur, ratio
    except Exception as exc:
        print(f"[fit_audio_file] Lỗi căn tốc độ: {exc}. Giữ nguyên file gốc.")
    finally:
        for p in (temp_wav_in, temp_wav_out):
            if os.path.exists(p):
                try: os.remove(p)
                except OSError: pass

    if os.path.abspath(audio_input_path) != os.path.abspath(output_path):
        shutil.copy2(audio_input_path, output_path)
    return output_path, orig_dur, 1.0


async def generate_single_tts(segment, output_folder, voice_source, voice_param, api_key, target_max_duration=None):
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
    
    for attempt in range(2):
        try:
            if voice_source == "fpt":
                try:
                    await generate_tts_fpt(text, temp_raw, api_key, voice="banmai")
                except FPTQuotaError as q_err:
                    print(f"CẢNH BÁO FPT: {q_err}. Fallback vĩnh viễn sang Edge TTS (Hoài My)")
                    voice_source = "edge"
                    await generate_tts_edge(text, temp_raw, voice_param)
            elif voice_source == "edge":
                if voice_param == "vi-VN-HoaiMyNeural":
                    await generate_tts_edge(text, temp_raw, voice_param, pitch="+15Hz", rate="+15%")
                else:
                    await generate_tts_edge(text, temp_raw, voice_param, pitch="+0Hz", rate="+5%")
            elif voice_source == "capcut":
                await asyncio.to_thread(_run_capcut_tts, text, temp_raw, voice_param)
            elif voice_source == "rvc":
                temp_rvc_raw = f"{base_audio}_temp_rvc_raw{ext}"
                await generate_tts_edge(text, temp_rvc_raw, "vi-VN-HoaiMyNeural", pitch="+0Hz", rate="+0%")
                await apply_rvc_clone(temp_rvc_raw, temp_raw, voice_param, strict=True)
                if os.path.exists(temp_rvc_raw):
                    try: os.remove(temp_rvc_raw)
                    except OSError: pass

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
            print(f"Lỗi TTS đoạn {segment.index} (Lần {attempt+1}): {e}")
            if os.path.exists(temp_raw):
                try: os.remove(temp_raw)
                except OSError: pass
            if "failed after" in str(e):
                break
            await asyncio.sleep(1.5)
            
    return None


MAX_NATURAL_SPEED = 1.45

@stage("voice")
async def generate_dubbing_audio(translated_segments, output_folder, voice_source="edge", voice_param="vi-VN-HoaiMyNeural", api_key="", video_duration=None):
    print(f"Generating TTS for dubbing using {voice_source} (Anti-Overlap Enabled)...")
    os.makedirs(output_folder, exist_ok=True)
    
    from .v1_voice_cache import voice_cache_key, read_voice_cache, write_voice_cache
    from .translation import _contains_cjk, translate_subtitles
    pending_translation = [s for s in translated_segments if _contains_cjk(s.content)]
    if pending_translation:
        await asyncio.to_thread(translate_subtitles, pending_translation,
                                target_lang="vi", strict=True, enable_g4f=False)
        if any(_contains_cjk(s.content) for s in pending_translation):
            raise RuntimeError("Translation incomplete; Vietnamese TTS was not started")

    import time
    overall_condense_deadline = time.monotonic() + 45.0

    # 1. TÍNH KHOẢNG ĐỌC TỐI ĐA CHO TỪNG CÂU (Codex Plan - Điểm 1)
    windows = calculate_reading_windows(translated_segments, video_duration=video_duration, gap_s=0.05)
    
    # 2. VÒNG 1: RÚT GỌN THEO LÔ TRƯỚC TTS DỰA TRÊN DỰ ĐOÁN (Codex Plan - Điểm 3 & Đợt 5)
    # Dự đoán độ dài dựa trên số từ (trung bình 0.28s mỗi từ tiếng Việt).
    # Gom một lượt gọi Gemini duy nhất để rút gọn toàn bộ câu quá dài trước khi render.
    overly_long_items = []
    for seg in translated_segments:
        text = seg.content.strip()
        hard_max = windows.get(seg.index, 3.0)
        word_count = len(text.split())
        est_duration = max(1.0, word_count * 0.28)
        if hard_max > 0 and (est_duration / hard_max) > MAX_NATURAL_SPEED:
            target_words = max(2, int((hard_max - 0.05) / 0.28))
            overly_long_items.append({
                "index": seg.index,
                "text": text,
                "target_seconds": hard_max,
                "target_words": target_words,
                "current_seconds": est_duration
            })

    if overly_long_items and (overall_condense_deadline - time.monotonic() > 2.0):
        print(f"[VOICE_GUARD] Vòng 1: Gom {len(overly_long_items)} câu dự kiến quá dài (> {MAX_NATURAL_SPEED}x), rút gọn theo lô...")
        from .translation import condense_vietnamese_subtitles_batch
        condensed_map = await asyncio.to_thread(
            condense_vietnamese_subtitles_batch,
            overly_long_items,
            api_key=api_key,
            deadline=overall_condense_deadline,
            job_id="round1_pre_tts"
        )
        for seg in translated_segments:
            if seg.index in condensed_map:
                old_text = seg.content
                seg.content = condensed_map[seg.index]
                print(f"[VOICE_GUARD] Đã rút gọn câu #{seg.index}: '{old_text[:35]}...' -> '{seg.content}'")

    limiter = asyncio.Semaphore(4)
    async def run_one(seg):
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
            
            result = await generate_single_tts(
                seg, output_folder, seg_source, seg_param, api_key, target_max_duration=hard_max
            )
            if result is None:
                for retry_idx in range(2):
                    await asyncio.sleep(2.0 * (retry_idx + 1))
                    result = await generate_single_tts(
                        seg, output_folder, seg_source, seg_param, api_key, target_max_duration=hard_max
                    )
                    if result is not None:
                        break

            if result is None:
                if os.path.exists(path):
                    try: os.remove(path)
                    except OSError: pass
                raise TTSIncompleteError(
                    f"TTS segment {seg.index} thất bại với giọng '{seg_source}' ({seg_param}). "
                    f"Không thay thế bằng giọng khác để đảm bảo tính nhất quán của video."
                )
            
            # Ghi nhận cache version 8
            if os.path.exists(path) and os.path.getsize(path) > 128 and result.get("actual_audio_duration", 0) > 0.1:
                write_voice_cache(path, key, result["actual_audio_duration"], seg.content.strip())
            else:
                if os.path.exists(path):
                    try: os.remove(path)
                    except OSError: pass
                raise TTSIncompleteError(f"Failed segment: {seg.index} (Audio invalid or too short)")
            return result

    results = await asyncio.gather(*(run_one(seg) for seg in translated_segments), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise RuntimeError(f"TTS incomplete: {len(errors)} subtitle(s); successful audio retained for retry") from errors[0]

    # 4. VÒNG 2: ĐO THỜI LƯỢNG THỰC TẾ & GOM RÚT GỌN BỔ SUNG (Codex Plan - Đợt 5)
    # Gom TẤT CẢ các câu thực tế vẫn quá dài (> 1.45x) sau TTS lần 1 thành MỘT lượt gọi bổ sung duy nhất
    res_by_idx = {r["index"]: r for r in results if r is not None and isinstance(r, dict)}
    post_tts_overly_long = []
    for seg in translated_segments:
        if seg.index not in res_by_idx:
            continue
        r = res_by_idx[seg.index]
        dur = r.get("actual_audio_duration", 0.0)
        hard_max = windows.get(seg.index, 3.0)
        speed_ratio = r.get("speed_ratio", 1.0)
        if hard_max > 0 and (dur / hard_max > MAX_NATURAL_SPEED or speed_ratio > MAX_NATURAL_SPEED):
            target_words = max(2, int((hard_max - 0.05) / 0.28))
            post_tts_overly_long.append({
                "index": seg.index,
                "text": seg.content.strip(),
                "target_seconds": hard_max,
                "target_words": target_words,
                "current_seconds": dur
            })

    if post_tts_overly_long and (overall_condense_deadline - time.monotonic() > 2.0):
        print(f"[VOICE_GUARD] Vòng 2 (Bổ sung): Phát hiện {len(post_tts_overly_long)} câu thực tế vẫn dài, gom 1 lượt gọi Gemini...")
        from .translation import condense_vietnamese_subtitles_batch
        condensed_map_round2 = await asyncio.to_thread(
            condense_vietnamese_subtitles_batch,
            post_tts_overly_long,
            api_key=api_key,
            deadline=overall_condense_deadline,
            job_id="round2_post_tts"
        )
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
            if not re_errors:
                for r in re_results:
                    if r and isinstance(r, dict):
                        res_by_idx[r["index"]] = r

    final_results = [res_by_idx[seg.index] for seg in translated_segments if seg.index in res_by_idx]

    global global_rvc_instance, global_rvc_model_path
    if global_rvc_instance is not None:
        global_rvc_instance = None
        global_rvc_model_path = None
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    return final_results
