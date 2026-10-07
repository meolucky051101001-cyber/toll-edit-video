import os
import sys
import subprocess
from batch_control import run as run_batch_subprocess
import time
import uuid
import logging
try:
    from .v1_stage_metrics import stage
except ImportError:
    from v1_stage_metrics import stage
logger = logging.getLogger(__name__)
import io
if isinstance(sys.stdout, io.TextIOWrapper):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except: pass
if isinstance(sys.stderr, io.TextIOWrapper):
    try: sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except: pass

# Windows flag to hide terminal windows
CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0
import ffmpeg

def extract_audio_from_video(video_path, output_audio_path):
    """
    Trích xuất âm thanh từ video dưới dạng file .wav chất lượng cao (44.1kHz, Stereo)
    để vừa dùng cho Whisper (tự downsample), vừa dùng cho Demucs để nhạc nền trong vắt.
    """
    print(f"Extracting high-quality audio from {video_path}...")
    try:
        cmd = (
            ffmpeg
            .input(str(video_path))
            .output(str(output_audio_path), acodec='pcm_s16le', ac=2, ar='44100')
            .overwrite_output()
            .compile()
        )
        run_batch_subprocess(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        return True
    except ffmpeg.Error as e:
        print("FFmpeg extract audio error:", e)
        return False

def separate_vocals_demucs(
    input_audio_path,
    output_dir,
    segment_seconds=None,
    timeout_seconds=900,
    separation_mode=None,
):
    """
    Tách vocal ra khỏi nhạc nền cho Tool V1.
    Ưu tiên sử dụng mô hình BS-RoFormer (SDR 12.97dB) để giữ nguyên 90-95% chất lượng nhạc nền,
    tự động fallback sang Demucs chất lượng cao (overlap 0.25, shifts 1) nếu cần.
    Hỗ trợ chế độ 'bypass' (giữ 100% âm thanh gốc) theo cấu hình.
    """
    import subprocess
    import sys
    import os
    import json
    import threading
    from ai.v1_model_policy import current_v1_model_policy

    # Resolve đường dẫn tuyệt đối để tránh lỗi ký tự đặc biệt và ".."
    input_audio_path = os.path.abspath(input_audio_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.exists(input_audio_path):
        raise FileNotFoundError(f"File audio không tồn tại để bóc tách vocal: {input_audio_path}")

    # Ưu tiên separation_mode truyền trực tiếp từ caller
    if separation_mode:
        sep_mode = str(separation_mode).strip().lower()
    else:
        try:
            from audio_settings import get_audio_settings
            audio_cfg = get_audio_settings()
            sep_mode = audio_cfg.get("separation_mode", "roformer")
        except Exception:
            sep_mode = "roformer"

        try:
            from job_config_service import get_frozen_config
            frozen = get_frozen_config(input_audio_path) or get_frozen_config(output_dir)
            if frozen and "separation_mode" in frozen.get("effective_config", {}):
                sep_mode = frozen["effective_config"]["separation_mode"]
        except Exception:
            pass

        env_sep = os.getenv("V1_SEPARATOR_BACKEND", "").strip().lower()
        if env_sep:
            sep_mode = env_sep

    if sep_mode == "bypass":
        print(f"[SEPARATION] Chế độ Bypass: Giữ nguyên toàn bộ âm thanh gốc (bao gồm cả lời nói gốc). Không tách lời bằng AI.")
        try:
            from job_tracker import record_separator_info
            record_separator_info({
                "engine": "bypass",
                "model": "none",
                "status": "bypassed",
            })
        except Exception:
            pass
        return input_audio_path, input_audio_path

    import shared_state
    if getattr(shared_state, "stop_requested", False):
        raise RuntimeError("Tác vụ tách âm bị hủy: Lệnh dừng được yêu cầu.")

    total_timeout = float(timeout_seconds) if timeout_seconds else 900.0
    try:
        import soundfile as _sf
        _info = _sf.info(input_audio_path)
        if _info.duration > 0:
            total_timeout = max(total_timeout, float(_info.duration) * 2.5 + 120.0)
    except Exception:
        pass

    policy = current_v1_model_policy()
    from v1_separator_lock import separator_gpu_lock

    def _run_cancellable_proc(cmd, t_limit):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        proc_deadline = time.monotonic() + float(t_limit)
        stdout_chunks = []
        stderr_chunks = []

        def _drain(pipe, collector):
            try:
                for line in iter(pipe.readline, ''):
                    collector.append(line)
                pipe.close()
            except Exception:
                pass

        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
        t_out.start()
        t_err.start()

        while True:
            if getattr(shared_state, "stop_requested", False):
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise RuntimeError("Tiến trình tách âm đã bị dừng theo yêu cầu (/stop).")

            ret = proc.poll()
            if ret is not None:
                t_out.join(timeout=2.0)
                t_err.join(timeout=2.0)
                return ret, "".join(stdout_chunks), "".join(stderr_chunks)

            if time.monotonic() > proc_deadline:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise TimeoutError(f"Tiến trình tách âm timeout sau {t_limit:.1f}s.")

            time.sleep(0.3)

    # 1. THỬ TÁCH BẰNG BS-ROFORMER (Phương án B - Đỉnh cao chất lượng)
    if sep_mode in ("auto", "roformer"):
        roformer_py = policy.separator_python_path()
        worker_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "model_workers",
            "v1_separator_worker.py",
        )
        model_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "models",
            "v1",
            "source-separation",
        )
        model_name = policy.separator_model
        model_ckpt = os.path.join(model_dir, model_name)

        if not (roformer_py.is_file() and os.path.isfile(worker_script) and os.path.isfile(model_ckpt)):
            err_msg = (
                f"BS-RoFormer thiếu thành phần bắt buộc: "
                f"python={roformer_py.is_file()}, worker={os.path.isfile(worker_script)}, model={os.path.isfile(model_ckpt)}"
            )
            if sep_mode == "roformer":
                raise RuntimeError(err_msg)
            print(f"{err_msg}. Chuyển sang Demucs...")
        else:
            roformer_out_dir = os.path.join(output_dir, "bs_roformer")
            os.makedirs(roformer_out_dir, exist_ok=True)
            print(f"Bắt đầu tách âm thanh bằng BS-RoFormer ({model_name}) native FP16...")
            try:
                cmd_roformer = [
                    str(roformer_py),
                    worker_script,
                    "--input-audio", input_audio_path,
                    "--output-dir", roformer_out_dir,
                    "--model-dir", model_dir,
                    "--model-name", model_name,
                    "--use-fp16",
                ]
                with separator_gpu_lock(timeout_seconds=total_timeout):
                    if getattr(shared_state, "stop_requested", False):
                        raise RuntimeError("Tác vụ tách âm bị hủy: Lệnh dừng được yêu cầu.")
                    ret_code, stdout_str, stderr_str = _run_cancellable_proc(cmd_roformer, total_timeout)
                if ret_code == 0:
                    for line in reversed(stdout_str.strip().splitlines()):
                        line = line.strip()
                        if line.startswith("{") and line.endswith("}"):
                            try:
                                data = json.loads(line)
                                if data.get("success"):
                                    v_path = data.get("vocals_path")
                                    bg_path = data.get("background_path")
                                    if v_path and bg_path and os.path.isfile(v_path) and os.path.isfile(bg_path):
                                        print(f"BS-RoFormer tách thành công! Nhạc nền sạch: {bg_path}")
                                        try:
                                            from job_tracker import record_separator_info
                                            record_separator_info({
                                                "engine": "bs_roformer",
                                                "engine_requested": sep_mode,
                                                "model": model_name,
                                                "device": data.get("device", "cuda"),
                                                "device_name": data.get("device_name", ""),
                                                "fp_mode": data.get("fp_mode", "fp32"),
                                                "inference_time_s": data.get("inference_time_s"),
                                                "total_time_s": data.get("total_time_s"),
                                                "peak_vram_mb": data.get("peak_vram_mb"),
                                                "vocals_path": v_path,
                                                "bgm_path": bg_path,
                                                "cache_hit": False,
                                            })
                                        except Exception:
                                            pass
                                        return v_path, bg_path
                            except json.JSONDecodeError:
                                pass
                err_detail = stderr_str.strip() or f"exit code {ret_code}"
                if sep_mode == "roformer":
                    raise RuntimeError(f"BS-RoFormer worker thất bại: {err_detail}")
                print(f"BS-RoFormer gặp sự cố ({err_detail}), tự động fallback sang Demucs...")
            except Exception as r_err:
                if sep_mode == "roformer":
                    raise
                print(f"BS-RoFormer exception ({r_err}), tự động fallback sang Demucs...")

    # 2. FALLBACK SANG DEMUCS CHẤT LƯỢNG CAO (GPU CUDA BẮT BUỘC)
    print(f"Bắt đầu tách âm thanh bằng Demucs ({policy.demucs_model})...")
    venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "Scripts", "python.exe")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    # Kiểm tra CUDA trong chính venv chạy Demucs
    try:
        check_cuda = subprocess.run(
            [venv_python, "-c", "import torch; print(torch.cuda.is_available())"],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW
        )
        has_cuda = check_cuda.stdout.strip().lower() == "true"
    except Exception:
        import torch
        has_cuda = torch.cuda.is_available()

    if not has_cuda:
        raise RuntimeError("GPU CUDA không khả dụng trên môi trường Demucs. Tách âm Demucs trên Tool V1 yêu cầu GPU, không chạy trên CPU.")

    model_name = policy.demucs_model
    device_args = ["-d", "cuda"]

    cmd = [
        venv_python, "-m", "demucs",
        input_audio_path,
        "-n", model_name,
        "--two-stems", "vocals",
        "--shifts", "1",
        "--overlap", "0.25",
        "-o", output_dir
    ]
    if segment_seconds is not None:
        segment_seconds = float(segment_seconds)
        if segment_seconds <= 0:
            raise ValueError("segment_seconds must be positive")
        cmd.extend(["--segment", "{:g}".format(segment_seconds)])
    cmd += device_args

    base_name = os.path.splitext(os.path.basename(input_audio_path))[0]
    demucs_out_dir = os.path.join(output_dir, model_name, base_name)
    vocals_path = os.path.join(demucs_out_dir, "vocals.wav")
    no_vocals_path = os.path.join(demucs_out_dir, "no_vocals.wav")

    # Dọn dẹp file cũ nếu có để tránh stale artifacts
    for old_f in (vocals_path, no_vocals_path):
        try:
            if os.path.isfile(old_f):
                os.remove(old_f)
        except Exception:
            pass

    demucs_start_epoch = time.time()
    with separator_gpu_lock(timeout_seconds=total_timeout):
        if getattr(shared_state, "stop_requested", False):
            raise RuntimeError("Tác vụ tách âm bị hủy: Lệnh dừng được yêu cầu.")
        ret_code, stdout_str, stderr_str = _run_cancellable_proc(cmd, total_timeout)
        if ret_code != 0:
            raise RuntimeError(f"Demucs GPU thất bại (mã thoát {ret_code}): {stderr_str.strip()}")

    if os.path.exists(vocals_path) and os.path.exists(no_vocals_path):
        if os.path.getmtime(vocals_path) >= demucs_start_epoch - 2.0 and os.path.getmtime(no_vocals_path) >= demucs_start_epoch - 2.0:
            print(f"Demucs tách thành công! Vocals: {vocals_path}")
            try:
                from job_tracker import record_separator_info
                record_separator_info({
                    "engine": "demucs",
                    "engine_requested": sep_mode,
                    "model": model_name,
                    "device": "cuda",
                    "total_time_s": round(time.time() - demucs_start_epoch, 2),
                    "vocals_path": vocals_path,
                    "bgm_path": no_vocals_path,
                    "cache_hit": False,
                })
            except Exception:
                pass
            return vocals_path, no_vocals_path

    # Fallback kiểm tra trong output_dir nhưng bắt buộc file phải tạo sau demucs_start_epoch
    for root, dirs, files in os.walk(output_dir):
        if "vocals.wav" in files and "no_vocals.wav" in files:
            v_p = os.path.join(root, "vocals.wav")
            nv_p = os.path.join(root, "no_vocals.wav")
            if os.path.getmtime(v_p) >= demucs_start_epoch - 2.0 and os.path.getmtime(nv_p) >= demucs_start_epoch - 2.0:
                try:
                    from job_tracker import record_separator_info
                    record_separator_info({
                        "engine": "demucs",
                        "engine_requested": sep_mode,
                        "model": model_name,
                        "device": "cuda",
                        "total_time_s": round(time.time() - demucs_start_epoch, 2),
                        "vocals_path": v_p,
                        "bgm_path": nv_p,
                        "cache_hit": False,
                    })
                except Exception:
                    pass
                return v_p, nv_p

    raise RuntimeError(f"Demucs chạy xong nhưng không tìm thấy file output mới tại {demucs_out_dir}")

def merge_audio_files_with_delay(video_path, original_audio_path, dubbing_audio_files, output_video_path, original_volume=0.1, dub_volume=1.0):
    """
    Ghép các file âm thanh lồng tiếng lại theo đúng mốc thời gian, trộn với âm thanh gốc đã giảm âm lượng,
    sau đó ghép vào video đã được làm mờ/chèn sub (đầu vào là video câm hoặc video gốc tuỳ cấu hình).
    """
    # Create a complex filter for ffmpeg to mix all audio
    # This is a basic implementation. A more robust way is using PyDub to generate a single mixed audio track first.
    pass
    
def mix_audio_pydub(
    original_audio_path,
    dubbing_audio_files,
    output_mixed_audio_path,
    original_volume_db=None,
    dubbing_volume_db=None,
    ducking_mode=None,
    strict=False,
    explicit=False,
    **kwargs,
):
    """
    Trộn âm thanh bằng PyDub. Điều chỉnh âm lượng nhạc nền và giọng đọc AI theo cấu hình mixer.
    """
    if strict and not dubbing_audio_files:
        raise ValueError("strict=True: Danh sách file lồng tiếng (dubbing_audio_files) rỗng.")

    try:
        from audio_settings import get_audio_settings
        _cfg = get_audio_settings()
        if not explicit and (original_volume_db is None or original_volume_db in (-2, -5)):
            original_volume_db = _cfg.get("bgm_volume_db", -2.0)
        if not explicit and (dubbing_volume_db is None or dubbing_volume_db == 1):
            dubbing_volume_db = _cfg.get("dubbing_volume_db", 1.0)
        if not ducking_mode:
            ducking_mode = _cfg.get("ducking_mode", "soft")
    except Exception:
        if original_volume_db is None: original_volume_db = -2.0
        if dubbing_volume_db is None: dubbing_volume_db = 1.0
        if not ducking_mode: ducking_mode = "soft"

    # Kiểm tra frozen config nếu chưa có ducking_mode cụ thể
    if not ducking_mode or not explicit:
        try:
            from job_config_service import get_frozen_config
            frozen = get_frozen_config(output_mixed_audio_path) or get_frozen_config(original_audio_path)
            if frozen and "ducking_mode" in frozen.get("effective_config", {}):
                ducking_mode = frozen["effective_config"]["ducking_mode"]
        except Exception:
            pass

    print(f"Mixing audio tracks using adaptive pydub (BGM={original_volume_db}dB, Dubbing={dubbing_volume_db}dB, Ducking={ducking_mode})...")
    original_popen = None
    try:
        import subprocess
        # Ngăn pydub nháy màn hình đen ffmpeg liên tục trên Windows
        original_popen = subprocess.Popen
        class PopenNoWindow(original_popen):
            def __init__(self, *args, **kwargs):
                if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                    kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
                super().__init__(*args, **kwargs)
        subprocess.Popen = PopenNoWindow
        
        from v1_audio_mixer import mix_adaptive_audio
        result = mix_adaptive_audio(
            bgm_path=original_audio_path,
            dubbing_audio_files=dubbing_audio_files,
            output_path=output_mixed_audio_path,
            base_bgm_gain_db=original_volume_db,
            base_voice_gain_db=dubbing_volume_db,
            ducking_mode=ducking_mode,
        )
        try:
            from job_tracker import record_mixer_info
            record_mixer_info({
                "ducking_mode": ducking_mode,
                "bgm_volume_db": original_volume_db,
                "dubbing_volume_db": dubbing_volume_db,
            })
        except Exception:
            pass
        return result
    except Exception as e:
        # A background-only file must never masquerade as a successful dub.
        raise RuntimeError("Adaptive mix failed; dubbed audio was not published") from e
    finally:
        if original_popen is not None:
            subprocess.Popen = original_popen

def clean_stale_temp_files(max_age_hours: float = 24.0) -> int:
    """
    Dọn dẹp các tệp tạm thời tồn đọng quá max_age_hours trong temp_subs và các thư mục cache.
    Trả về số lượng file đã dọn dẹp.
    """
    cleaned = 0
    now = time.time()
    max_age_sec = max_age_hours * 3600.0

    # 1. Quét dọn temp_subs
    base_dir = os.path.dirname(os.path.abspath(__file__))
    temp_subs_dir = os.path.join(base_dir, "temp_subs")
    if os.path.isdir(temp_subs_dir):
        try:
            for item in os.listdir(temp_subs_dir):
                item_path = os.path.join(temp_subs_dir, item)
                if os.path.isfile(item_path):
                    try:
                        if (now - os.path.getmtime(item_path)) > max_age_sec:
                            os.remove(item_path)
                            cleaned += 1
                    except OSError:
                        pass
        except Exception:
            pass

    # 2. Quét dọn .v1_ocr_cache trong các ổ đĩa và workspace phổ biến
    candidate_cache_dirs = [
        r"D:\workspace\downloads\.v1_ocr_cache",
        r"D:\video phôi\.v1_ocr_cache",
        os.path.join(base_dir, "workspace", ".v1_ocr_cache"),
    ]
    for c_dir in candidate_cache_dirs:
        if os.path.isdir(c_dir):
            try:
                for item in os.listdir(c_dir):
                    item_path = os.path.join(c_dir, item)
                    if os.path.isfile(item_path):
                        try:
                            if (now - os.path.getmtime(item_path)) > max_age_sec:
                                os.remove(item_path)
                                cleaned += 1
                        except OSError:
                            pass
            except Exception:
                pass

    if cleaned > 0:
        logger.info(f"[CLEANUP] Đã dọn dẹp {cleaned} tệp tạm cũ (> {max_age_hours}h).")
    return cleaned


@stage("render")
def process_video(
    video_path,
    srt_path,
    mixed_audio_path,
    output_video_path,
    font_name="Arial",
    font_color="&H00000000",
    font_weight=2,
    main_y_pct=0.75,
    delogo=True,
    timeout_seconds=None,
    **kwargs,
):
    """
    Dùng ffmpeg để chèn hardsub, xóa sạch watermark gốc và ghép âm thanh mới.
    """
    import shared_state
    if getattr(shared_state, 'stop_requested', False):
        print("Lệnh /stop đã được yêu cầu. Hủy render video.")
        return False

    video_path = os.fspath(video_path)
    srt_path = os.fspath(srt_path)
    mixed_audio_path = os.fspath(mixed_audio_path)
    output_video_path = os.fspath(output_video_path)

    render_deadline = None
    if timeout_seconds is not None:
        timeout_seconds = float(timeout_seconds)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        render_deadline = time.monotonic() + timeout_seconds

    print("Processing final video with styled subtitles, auto-delogo and hardware encoder...")
    
    # Tự động dọn dẹp các tệp tạm cũ trước khi xử lý
    try:
        clean_stale_temp_files(max_age_hours=24.0)
    except Exception:
        pass

    # Tạo bản copy an toàn ASCII ở thư mục temp_subs để FFmpeg filter subtitles không bị dính ký tự Unicode
    import shutil
    import uuid
    base_dir = os.path.dirname(os.path.abspath(__file__))
    temp_subs_dir = os.path.join(base_dir, "temp_subs")
    try:
        os.makedirs(temp_subs_dir, exist_ok=True)
    except Exception:
        temp_subs_dir = base_dir

    unique_sub_name = f"temp_burn_{int(time.time())}_{uuid.uuid4().hex[:6]}" + (".ass" if srt_path.endswith('.ass') else ".srt")
    safe_sub_path = os.path.join(temp_subs_dir, unique_sub_name)
    try:
        shutil.copy2(srt_path, safe_sub_path)
        srt_to_use = safe_sub_path
    except Exception:
        srt_to_use = srt_path
        
    srt_escaped = srt_to_use.replace('\\', '/').replace(':', '\\:')
    
    bold_val = -1 if font_weight > 1 else 0
    style_str = f"FontName={font_name},FontSize=14,PrimaryColour={font_color},Bold={bold_val},Outline=2,Shadow=1,MarginV=40,BorderStyle=1"
        
    try:
        filter_parts = []
        try:
            from .v1_media_streams import probe_main_video, validate_video_output
        except ImportError:
            from v1_media_streams import probe_main_video, validate_video_output
        source_video = probe_main_video(video_path)
        original_w, original_h = source_video.width, source_video.height
        w, h = original_w, original_h
        if w <= 0 or h <= 0:
            raise ValueError("Invalid video dimensions")
        logger.info("V1 render size source=%dx%d output=%dx%d stream=%s cover_excluded=%s fps=%.6f",
                    original_w, original_h, w, h, source_video.index, source_video.has_cover, source_video.fps)
        
        # NVENC H.264 hardware limit is 4096px per dimension. Downscale oversized/8K/4.3K to standard 1080p bounds.
        orig_w, orig_h = w, h
        target_w, target_h = w, h
        max_dim = max(w, h)
        if max_dim > 3840 or min(w, h) > 2160 or max_dim > 4096:
            ratio = min(1080.0 / min(w, h), 1920.0 / max_dim)
            target_w = int(w * ratio) // 2 * 2
            target_h = int(h * ratio) // 2 * 2
            filter_parts.append(f"scale={target_w}:{target_h}")
            w, h = target_w, target_h
            logger.info("NVENC H.264 bounds enforced: scaled %dx%d -> %dx%d", orig_w, orig_h, w, h)

        # Xóa sạch toàn bộ watermark ở cả 4 góc video (Logo Tiểu Hồng Thư và ID tác giả nhảy trên/dưới)
        if delogo:
            try:
                
                if w > 0 and h > 0:
                    # 1. Góc dưới phải (Logo đáy)
                    br_w = max(int(w * 0.19), 10)
                    br_h = max(int(h * 0.065), 10)
                    br_x = w - br_w - int(w * 0.01)
                    br_y = h - br_h - int(h * 0.01)
                    
                    # 2. Góc dưới trái (ID/Avatar đáy)
                    bl_w = max(int(w * 0.38), 10)
                    bl_h = max(int(h * 0.065), 10)
                    bl_x = int(w * 0.01)
                    bl_y = h - bl_h - int(h * 0.01)
                    
                    # 3. Góc trên trái (Logo nhảy lên đỉnh)
                    tl_w = max(int(w * 0.20), 10)
                    tl_h = max(int(h * 0.055), 10)
                    tl_x = int(w * 0.01)
                    tl_y = int(h * 0.01)
                    
                    # 4. Góc trên phải (ID/Avatar nhảy lên đỉnh)
                    tr_w = max(int(w * 0.32), 10)
                    tr_h = max(int(h * 0.055), 10)
                    tr_x = w - tr_w - int(w * 0.01)
                    tr_y = int(h * 0.01)
                    
                    filter_parts.append(f"delogo=x={br_x}:y={br_y}:w={br_w}:h={br_h}")
                    filter_parts.append(f"delogo=x={bl_x}:y={bl_y}:w={bl_w}:h={bl_h}")
                    filter_parts.append(f"delogo=x={tl_x}:y={tl_y}:w={tl_w}:h={tl_h}")
                    filter_parts.append(f"delogo=x={tr_x}:y={tr_y}:w={tr_w}:h={tr_h}")
            except Exception as d_err:
                print(f"Lưu ý: Không thể cấu hình delogo ({d_err})")

        if srt_to_use.endswith('.ass'):
            srt_filter_str = f"subtitles='{srt_escaped}'"
        else:
            filter_parts.append(f"subtitles='{srt_escaped}':force_style='{style_str}'")
            
        # Convert to 8-bit YUV420P so 10-bit HEVC/HDR source does not cause NVENC "10 bit encode not supported"
        filter_parts.append("format=yuv420p")
        filter_complex = ",".join(filter_parts)
        
        video_bitrate_kbps = 8000
        b_v = f"{video_bitrate_kbps}k"
        
        # NVIDIA encoding only. No MediaFoundation/software CPU fallback.
        encoders_to_try = [
            ['h264_nvenc', '-preset', 'p4', '-tune', 'hq', '-b:v', b_v, '-spatial-aq', '1'],
            ['h264_nvenc', '-preset', 'fast', '-b:v', b_v],
        ]
        
        for enc_idx, enc_args in enumerate(encoders_to_try):
            encoder_name = enc_args[0]
            encoder_started = time.monotonic()
            logger.info("V1 render start encoder=%s", encoder_name)
            # Su dung GPU NVDEC phan cung de decode truc tiep tren VRAM GPU, giam tai CPU va tang toc gap 3-4x
            hw_args = ['-hwaccel', 'cuda'] if enc_idx == 0 else []
            cmd = [
                'ffmpeg',
                '-loglevel', 'warning',
                '-nostats',
                '-y',
            ] + hw_args + [
                # Bound 4K decoder/filter frame pools: auto threading can
                # allocate >10 GB and push a 16 GB machine into paging.
                '-threads', '4',
                '-filter_threads', '2',
                '-i', video_path,
                '-i', mixed_audio_path,
                '-vf', filter_complex,
                '-map', source_video.input_map,
                '-map', '1:a:0',
                '-sn', '-dn',
                '-c:v', encoder_name
            ] + enc_args[1:] + [
                '-pix_fmt', 'yuv420p',
                '-fps_mode', 'passthrough',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', '+faststart',
                '-map_metadata', '-1',
                '-fflags', '+bitexact',
                '-shortest',
                # Default shortest buffering can retain seconds of raw 4K60
                # frames; audio is continuous, so a one-second window suffices.
                '-shortest_buf_duration', '1',
                output_video_path
            ]
            
            try:
                command_timeout = None
                if render_deadline is not None:
                    command_timeout = render_deadline - time.monotonic()
                    if command_timeout <= 0:
                        print("Đã hết thời gian render trước khi thử encoder tiếp theo.")
                        break
                proc = run_batch_subprocess(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                    encoding='utf-8',
                    errors='ignore',
                    timeout=command_timeout,
                )
                if proc.returncode == 0 and os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 10000:
                    validate_video_output(output_video_path, source_video, require_audio=True, expected_size=(w, h))
                    logger.info("V1 render complete encoder=%s seconds=%.2f bytes=%d",
                                encoder_name, time.monotonic()-encoder_started,
                                os.path.getsize(output_video_path))
                    print(f"Render video thành công bằng encoder: {encoder_name}")
                    return True
                else:
                    error_text = proc.stderr or "FFmpeg did not produce a valid output file."
                    error_file = output_video_path + f".render-{enc_args[2]}-error.log"
                    try:
                        from pathlib import Path
                        Path(error_file).write_text(error_text, encoding='utf-8')
                    except OSError:
                        logger.exception("Unable to save full FFmpeg diagnostic")
                    logger.warning("V1 render fallback encoder=%s seconds=%.2f exit=%s reason=%s",
                                   encoder_name, time.monotonic()-encoder_started,
                                   proc.returncode, error_text[:6000])
                    print(f"Encoder {encoder_name} không thành công ({proc.returncode}). Chi tiết: {error_file}")
                    lower_error = error_text.lower()
                    can_retry = (any(token in lower_error for token in ('spatial-aq', 'unsupported', 'invalid param', 'hwaccel', 'cuda', 'cuvid'))
                                 and not any(token in lower_error for token in (
                                     'error reinitializing filters', 'failed to inject frame',
                                     'error initializing filter', 'no capable devices',
                                     'cannot load nvcuda', 'out of memory')))
                    if not can_retry:
                        break
            except subprocess.TimeoutExpired as enc_err:
                print(f"Encoder {encoder_name} vượt quá deadline render ({enc_err}).")
                break
            except Exception as enc_err:
                logger.exception("V1 render failed; retain job checkpoint, GPU-only policy remains active")
                print(f"Encoder {encoder_name} gặp ngoại lệ ({enc_err}); không chuyển sang CPU.")
                break
                
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("FFmpeg process video error:", e)
        return False
    finally:
        if os.path.exists(safe_sub_path):
            try: os.remove(safe_sub_path)
            except: pass
