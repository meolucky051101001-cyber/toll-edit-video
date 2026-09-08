import os
import asyncio
import re
import threading
import edge_tts
from pydub import AudioSegment

edge_semaphore = asyncio.Semaphore(2)
capcut_semaphore = threading.Semaphore(2)
rvc_semaphore = asyncio.Semaphore(1)
global_rvc_instance = None
global_rvc_model_path = None


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

def _run_capcut_tts_once(
    text, output_path, voice="BV562_streaming", poll_interval=3.0
):
    import json, requests, time
    from capcut_tts_api import CapCutClient
    client = CapCutClient()
    
    res = client.create_tts_task(texts=text, voice=voice)
    task_id = res["data"]["tasks"][0]["id"]
    token = res["data"]["tasks"][0]["token"]
    
    for _ in range(60):
        time.sleep(max(0.0, float(poll_interval)))
        query_res = client.query_tts_task(task_id, token)
        status = query_res["data"]["tasks"][0]["status"]
        if status in ("success", "succeed"):
            payload = json.loads(query_res["data"]["tasks"][0]["payload"])
            speech_url = payload["audio_subtitles"][0]["speech_url"]
            r = requests.get(speech_url, timeout=30)
            r.raise_for_status()
            if len(r.content) < 128:
                raise RuntimeError("CapCut TTS returned empty audio")
            with open(output_path, "wb") as f:
                f.write(r.content)
            return True
        elif status == "failed":
            raise Exception("CapCut TTS task failed")
            
    raise Exception("CapCut TTS timeout after 60s")


def _run_capcut_tts(
    text,
    output_path,
    voice="BV562_streaming",
    attempts=3,
    retry_delays=(2.0, 5.0),
    poll_interval=3.0,
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

        try:
            if os.path.exists(output_audio):
                os.remove(output_audio)
        except OSError:
            pass
        
        def run_rvc_with_method(method="rmvpe"):
            global global_rvc_instance, global_rvc_model_path
            import torch
            from rvc_python.infer import RVCInference
            if global_rvc_instance is None:
                global_rvc_instance = RVCInference(device="cuda:0" if torch.cuda.is_available() else "cpu")
            
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

async def generate_single_tts(segment, output_folder, voice_source, voice_param, api_key):
    import shared_state
    if shared_state.stop_requested:
        raise Exception("Bị hủy bởi lệnh /stop")
        
    text = segment.content.strip()
    if not text or not re.search(r'\w', text):
        return None
        
    audio_filename = f"{segment.index}.mp3"
    audio_path = os.path.join(output_folder, audio_filename)
    
    for attempt in range(5):
        try:
            if voice_source == "fpt":
                try:
                    await generate_tts_fpt(text, audio_path, api_key, voice="banmai")
                except FPTQuotaError as q_err:
                    print(f"CẢNH BÁO FPT: {q_err}. Fallback vĩnh viễn sang Edge TTS (Hoài My)")
                    voice_source = "edge"
                    await generate_tts_edge(text, audio_path, voice_param)
            
            if voice_source == "edge":
                if voice_param == "vi-VN-HoaiMyNeural":
                    await generate_tts_edge(text, audio_path, voice_param, pitch="+15Hz", rate="+15%")
                    audio = AudioSegment.from_file(audio_path)
                    duration_s = len(audio) / 1000.0
                    expected_s = (segment.end - segment.start).total_seconds()
                    
                    if expected_s > 0 and duration_s > expected_s:
                        ratio = duration_s / expected_s
                        ratio = min(ratio, 1.8) 
                        temp_speed = audio_path.replace(".mp3", "_speed.mp3")
                        import subprocess, shutil
                        subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-filter:a", f"atempo={ratio:.2f}", temp_speed], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                        if os.path.exists(temp_speed):
                            shutil.move(temp_speed, audio_path)
                        
                    temp_filtered = audio_path.replace(".mp3", "_filtered.mp3")
                    clear_filter = "highpass=f=100,equalizer=f=3500:width_type=q:width=1.5:g=3,treble=g=3,acompressor=threshold=-15dB:ratio=3:attack=5:release=50:makeup=5dB"
                    import subprocess, shutil
                    subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-filter:a", clear_filter, temp_filtered], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                    if os.path.exists(temp_filtered):
                        shutil.move(temp_filtered, audio_path)
                else:
                    await generate_tts_edge(text, audio_path, voice_param, pitch="+0Hz", rate="+5%")
            elif voice_source == "rvc":
                temp_edge_raw = audio_path.replace(".mp3", "_temp_raw.mp3")
                temp_edge_rvc = audio_path.replace(".mp3", "_temp_rvc.mp3")
                
                try:
                    await asyncio.to_thread(_run_capcut_tts, text, temp_edge_raw, "BV562_streaming")
                except Exception as e:
                    print(f"Lỗi CapCut TTS: {e}. Fallback sang Edge TTS (Hoài My)...")
                    await generate_tts_edge(text, temp_edge_raw, "vi-VN-HoaiMyNeural", pitch="+0Hz", rate="+0%")
                
                await apply_rvc_clone(temp_edge_raw, temp_edge_rvc, voice_param)
                
                audio = AudioSegment.from_file(temp_edge_rvc)
                duration_s = len(audio) / 1000.0
                expected_s = (segment.end - segment.start).total_seconds()
                
                if expected_s > 0 and duration_s > expected_s + 0.3:
                    ratio = duration_s / expected_s
                    ratio = min(ratio, 2.0)
                    import subprocess
                    subprocess.run(["ffmpeg", "-y", "-i", temp_edge_rvc, "-filter:a", f"atempo={ratio}", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                else:
                    import shutil
                    shutil.copy(temp_edge_rvc, audio_path)
                
                try:
                    os.remove(temp_edge_raw)
                    os.remove(temp_edge_rvc)
                except:
                    pass
            
            final_audio = AudioSegment.from_file(audio_path)
            actual_duration_s = len(final_audio) / 1000.0
            
            return {
                "index": segment.index,
                "path": audio_path,
                "start": segment.start.total_seconds(),
                "end": segment.end.total_seconds(),
                "actual_audio_duration": actual_duration_s,
                "content": text
            }
        except Exception as e:
            print(f"Lỗi TTS đoạn {segment.index} (Lần {attempt+1}): {e}")
            await asyncio.sleep(1.5)
            
    return None

async def generate_dubbing_audio(translated_segments, output_folder, voice_source="edge", voice_param="vi-VN-HoaiMyNeural", api_key=""):
    print(f"Generating TTS for dubbing using {voice_source} (Parallel)...")
    os.makedirs(output_folder, exist_ok=True)
    
    tasks = [
        generate_single_tts(seg, output_folder, voice_source, voice_param, api_key)
        for seg in translated_segments
    ]
    
    results = await asyncio.gather(*tasks)
    return [res for res in results if res is not None]
