import os
import sys
import subprocess
import time
import uuid
import logging
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
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        return True
    except ffmpeg.Error as e:
        print("FFmpeg extract audio error:", e)
        return False

def separate_vocals_demucs(
    input_audio_path,
    output_dir,
    segment_seconds=None,
    timeout_seconds=300,
):
    """Use V2's isolated source-separation policy; never import V1 helpers."""
    try:
        from .ai.source_separation import separate_vocals
    except ImportError:
        from ai.source_separation import separate_vocals
    return separate_vocals(
        input_audio_path, output_dir,
        segment_seconds=segment_seconds,
        timeout_seconds=timeout_seconds,
    )

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
    strict=False,
    **kwargs,
):
    """
    Trộn âm thanh bằng PyDub. Điều chỉnh âm lượng nhạc nền và giọng đọc AI theo cấu hình mixer.
    """
    try:
        from audio_settings import get_audio_settings
        _cfg = get_audio_settings()
        if original_volume_db is None or original_volume_db in (-2, -5):
            original_volume_db = _cfg.get("bgm_volume_db", -2.0)
        if dubbing_volume_db is None or dubbing_volume_db == 1:
            dubbing_volume_db = _cfg.get("dubbing_volume_db", 1.0)
    except Exception:
        if original_volume_db is None: original_volume_db = -2.0
        if dubbing_volume_db is None: dubbing_volume_db = 1.0
    try:
        from pipeline_v2.mixer import FFmpegMixSettings, mix_audio_ffmpeg
        settings = FFmpegMixSettings(
            background_gain_db=float(original_volume_db),
            voice_gain_db=float(dubbing_volume_db),
            duck_threshold=0.025,
            duck_ratio=8.0,
            duck_attack_ms=20.0,
            duck_release_ms=300.0,
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
        )
        print("Mixing audio with studio FFmpeg Sidechain Ducking & EBU R128...")
        mix_audio_ffmpeg(
            background_audio=original_audio_path,
            dubbing_audio_files=dubbing_audio_files,
            output_path=output_mixed_audio_path,
            settings=settings,
            timeout_seconds=max(600.0, len(dubbing_audio_files) * 5.0),
        )
        if os.path.isfile(output_mixed_audio_path) and os.path.getsize(output_mixed_audio_path) > 0:
            return output_mixed_audio_path
    except Exception as exc:
        print(f"FFmpeg dynamic mixer notice: {exc}. Falling back to pydub mix...")

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
        
        from pydub import AudioSegment
        
        # Load audio gốc và giảm âm lượng
        mixed = AudioSegment.from_file(original_audio_path)
        mixed = mixed + original_volume_db
        
        # Chèn từng file lồng tiếng (Khớp chính xác 100% thời gian với Subtitle)
        for dub in dubbing_audio_files:
            if not os.path.exists(dub["path"]):
                if strict:
                    raise FileNotFoundError(
                        "Missing dubbing audio: {}".format(dub["path"])
                    )
                continue
            dub_audio = AudioSegment.from_file(dub["path"])
            # Tăng âm lượng giọng đọc nếu cần
            dub_audio = dub_audio + dubbing_volume_db
            
            position_ms = int(dub["start"] * 1000)
            mixed = mixed.overlay(dub_audio, position=position_ms)
            
        mixed.export(output_mixed_audio_path, format="wav")
        return output_mixed_audio_path
    except Exception as e:
        print(f"PyDub error: {e}. Fallback to original audio.")
        if strict:
            raise RuntimeError("PyDub legacy mix failed") from e
        import shutil
        shutil.copy(original_audio_path, output_mixed_audio_path)
        return output_mixed_audio_path
    finally:
        if original_popen is not None:
            subprocess.Popen = original_popen


def build_canvas_render_graph(
    video_path: str,
    original_w: int,
    original_h: int,
    delogo_parts: list,
    srt_filter_str: str,
):
    """
    Xây dựng filter FFmpeg chuyển đổi tỷ lệ (9:16, 16:9, 1:1) và lồng background chống re-up.
    """
    try:
        from canvas_settings import get_canvas_settings
        cfg = get_canvas_settings()
    except Exception:
        cfg = {"aspect_ratio": "original", "bg_type": "none"}

    aspect = str(cfg.get("aspect_ratio", "original")).lower()
    bg_type = str(cfg.get("bg_type", "blur")).lower()
    video_scale = float(cfg.get("video_scale", 0.88))
    mirror = bool(cfg.get("mirror", False))
    blur_sigma = int(cfg.get("blur_sigma", 25))
    darken = float(cfg.get("darken_bg", 0.35))
    rimax = round(max(0.1, 1.0 - darken), 2)
    bg_color = cfg.get("bg_color", "#0a0e17")
    bg_image = cfg.get("bg_image", "")

    anti_reup_enabled = bool(cfg.get("anti_reup_enabled", False))
    # Nếu tắt chế độ chống re-up, hoặc giữ nguyên tỷ lệ gốc và không dùng nền/lật/scale:
    if not anti_reup_enabled or (aspect == "original" and (bg_type == "none" or (video_scale >= 0.99 and not mirror and bg_type not in ("blur", "video_motion")))):
        filter_parts = list(delogo_parts)
        filter_parts.append(srt_filter_str)
        return ["-vf", ",".join(filter_parts)], [], "0:v"

    # Tính toán kích thước khung hình đích (tw, th)
    if aspect == "9:16":
        tw, th = 1080, 1920
    elif aspect == "16:9":
        tw, th = 1920, 1080
    elif aspect == "1:1":
        tw, th = 1080, 1080
    else:  # "original"
        tw, th = original_w, original_h

    tw = int(tw) & ~1
    th = int(th) & ~1

    fw = max(100, int(tw * video_scale)) & ~1
    fh = max(100, int(th * video_scale)) & ~1
    flip_str = "hflip," if mirror else ""

    # Delogo xử lý trên luồng video gốc trước khi tách nền/tiền cảnh
    delogo_chain = ",".join(delogo_parts)
    if delogo_chain:
        base_stage = f"[0:v]{delogo_chain}[v_clean];"
        v_clean = "[v_clean]"
    else:
        base_stage = ""
        v_clean = "[0:v]"

    # Hardsub xử lý trực tiếp trên luồng video gốc (v_clean) để đảm bảo
    # tọa độ OCR bounding box che khít 100% phụ đề gốc tiếng Trung
    # trước khi video được scale hoặc lồng ghép vào canvas nền.
    if srt_filter_str:
        sub_stage = f"{v_clean}{srt_filter_str}[fg_subbed];"
        fg_in = "[fg_subbed]"
    else:
        sub_stage = ""
        fg_in = v_clean

    auto_crop_black_bars = bool(cfg.get("auto_crop_black_bars", True))
    crop_padding = int(cfg.get("crop_padding", 0))
    crop_prefix = ""
    if auto_crop_black_bars:
        try:
            from workflow_api import detect_letterbox_crop
            crop_box = detect_letterbox_crop(video_path, padding=crop_padding)
            if crop_box:
                crop_prefix = f"crop={crop_box['w']}:{crop_box['h']}:{crop_box['x']}:{crop_box['y']},"
        except Exception:
            crop_prefix = ""

    extra_inputs = []

    if bg_type == "blur":
        # Nền mờ Gaussian phủ kín khung [bg] từ video gốc sạch (chưa burn sub), video chính ở giữa [fg] có sub
        fc = (
            f"{base_stage}"
            f"{v_clean}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur={blur_sigma}:5,setsar=1,colorlevels=rimax={rimax}:gimax={rimax}:bimax={rimax}[bg];"
            f"{sub_stage}"
            f"{fg_in}{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
        )
    elif bg_type == "image":
        try:
            from canvas_settings import resolve_image_bg_path
            bg_image_file = cfg.get("bg_image_file", "tia_sang_vang_ngoi_sao.jpg")
            real_image_path = resolve_image_bg_path(bg_image_file or bg_image)
        except Exception:
            real_image_path = bg_image

        if real_image_path and os.path.isfile(real_image_path):
            extra_inputs = ["-loop", "1", "-i", real_image_path]
            fc = (
                f"{base_stage}"
                f"[2:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                f"{sub_stage}"
                f"{fg_in}{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
            )
        else:
            safe_color = bg_color.replace("#", "0x")
            fc = (
                f"{base_stage}"
                f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                f"{sub_stage}"
                f"{fg_in}{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
            )
    elif bg_type == "video_motion":
        # Nền video chuyển động (sóng biển, biển xanh, rừng cây,...) lặp vô tận chống quét re-up
        try:
            from canvas_settings import resolve_motion_bg_path
            bg_motion_file = cfg.get("bg_motion_file", "song_bien.mp4")
            real_motion_path = resolve_motion_bg_path(bg_motion_file)
        except Exception:
            real_motion_path = ""
        if real_motion_path and os.path.isfile(real_motion_path):
            extra_inputs = ["-stream_loop", "-1", "-i", real_motion_path]
            fc = (
                f"{base_stage}"
                f"[2:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                f"{sub_stage}"
                f"{fg_in}{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
            )
        else:
            # Fallback nền mờ nếu chưa tải xong file motion
            fc = (
                f"{base_stage}"
                f"{v_clean}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur={blur_sigma}:5,setsar=1,colorlevels=rimax={rimax}:gimax={rimax}:bimax={rimax}[bg];"
                f"{sub_stage}"
                f"{fg_in}{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
            )
    else:
        # Nền màu (đen hoặc mã màu hex tùy chỉnh)
        safe_color = bg_color.replace("#", "0x")
        fc = (
            f"{base_stage}"
            f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
            f"{sub_stage}"
            f"{fg_in}{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
        )

    return ["-filter_complex", fc], extra_inputs, "[outv]"


def process_video(
    video_path,
    srt_path,
    mixed_audio_path,
    output_video_path,
    font_name="Arial",
    font_color="&H00FFFFFF",
    font_weight=1,
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
        import cv2
        cap = cv2.VideoCapture(video_path)
        try:
            original_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            original_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()
        w, h = original_w, original_h
        if w <= 0 or h <= 0:
            raise ValueError("Invalid video dimensions")
        logger.info("V2 render size source=%dx%d output=%dx%d", original_w, original_h, w, h)
        
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
            srt_filter_str = f"subtitles='{srt_escaped}':force_style='{style_str}'"

        filter_args, extra_inputs, video_map = build_canvas_render_graph(
            video_path, w, h, filter_parts, srt_filter_str
        )
        
        video_bitrate_kbps = 8000
        b_v = f"{video_bitrate_kbps}k"
        
        # GPU-first NVENC with CPU libx264 fallback for long video resilience (e.g. NVENC session limit on RTX 4050)
        encoders_to_try = [
            ['h264_nvenc', '-preset', 'p4', '-tune', 'hq', '-b:v', b_v, '-spatial-aq', '1'],
            ['h264_nvenc', '-preset', 'fast', '-b:v', b_v],
            ['libx264', '-preset', 'veryfast', '-crf', '22'],
        ]
        
        for enc_args in encoders_to_try:
            encoder_name = enc_args[0]
            encoder_started = time.monotonic()
            logger.info("V2 render start encoder=%s", encoder_name)
            cmd = [
                'ffmpeg',
                '-y',
                '-threads', '4',
                '-filter_threads', '2',
            ]
            if encoder_name == 'h264_nvenc':
                cmd += ['-hwaccel', 'cuda']
            cmd += [
                '-i', video_path,
                '-i', mixed_audio_path,
            ] + extra_inputs + filter_args + [
                '-map', video_map,
                '-map', '1:a',
                '-c:v', encoder_name
            ] + enc_args[1:] + [
                '-pix_fmt', 'yuv420p',
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
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                    encoding='utf-8',
                    errors='ignore',
                    timeout=command_timeout,
                )
                if proc.returncode == 0 and os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 10000:
                    logger.info("V2 render complete encoder=%s seconds=%.2f bytes=%d",
                                encoder_name, time.monotonic()-encoder_started,
                                os.path.getsize(output_video_path))
                    print(f"Render video thành công bằng encoder: {encoder_name}")
                    return True
                else:
                    err_snippet = proc.stderr[-400:] if proc.stderr else ""
                    logger.warning("V2 render fallback encoder=%s seconds=%.2f exit=%s reason=%s",
                                   encoder_name, time.monotonic()-encoder_started,
                                   proc.returncode, err_snippet)
                    print(f"Encoder {encoder_name} không thành công ({proc.returncode}): {err_snippet}")
            except subprocess.TimeoutExpired as enc_err:
                print(f"Encoder {encoder_name} vượt quá deadline render ({enc_err}).")
                break
            except Exception as enc_err:
                print(f"Encoder {encoder_name} gặp ngoại lệ ({enc_err}), chuyển sang encoder dự phòng...")
                
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
