import os
import sys
import subprocess
import time
import uuid
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
    """
    Tách vocal bằng BS-RoFormer, tự động fallback về Demucs fine-tuned.

    Tên hàm cũ được giữ để không phá API V1/V2.
    Trả về (vocals_path, no_vocals_path)
    """
    from ai.source_separation import separate_vocals

    return separate_vocals(
        input_audio_path,
        output_dir,
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
    original_volume_db=-5,
    dubbing_volume_db=1,
    strict=False,
    **kwargs,
):
    """
    Trộn âm thanh bằng PyDub. Giảm âm lượng nhạc nền (-15dB, tức khoảng 15-20%) và chèn giọng đọc AI vào đúng vị trí.
    """
    print("Mixing audio tracks using pydub...")
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
    
    # Tạo bản copy an toàn ASCII ở thư mục gốc backend để FFmpeg filter subtitles không bị dính ký tự Unicode
    import shutil
    import uuid
    base_dir = os.path.dirname(os.path.abspath(__file__))
    unique_sub_name = f"temp_burn_{int(time.time())}_{uuid.uuid4().hex[:6]}" + (".ass" if srt_path.endswith('.ass') else ".srt")
    safe_sub_path = os.path.join(base_dir, unique_sub_name)
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
        
        # Xóa sạch toàn bộ watermark ở cả 4 góc video (Logo Tiểu Hồng Thư và ID tác giả nhảy trên/dưới)
        if delogo:
            try:
                import cv2
                cap = cv2.VideoCapture(video_path)
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                cap.release()
                
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
            filter_parts.append(f"subtitles='{srt_escaped}'")
        else:
            filter_parts.append(f"subtitles='{srt_escaped}':force_style='{style_str}'")
            
        filter_complex = ",".join(filter_parts)
        
        video_bitrate_kbps = 8000
        b_v = f"{video_bitrate_kbps}k"
        
        # Danh sách các bộ mã hóa video theo thứ tự ưu tiên tốc độ cao nhất:
        # 1. h264_nvenc (NVIDIA GPU Hardware)
        # 2. h264_mf (Windows MediaFoundation Hardware)
        # 3. libx264 (CPU Đa nhân tối ưu veryfast)
        encoders_to_try = [
            ['h264_nvenc', '-preset', 'p4', '-tune', 'hq', '-b:v', b_v, '-spatial-aq', '1'],
            ['h264_nvenc', '-preset', 'fast', '-b:v', b_v],
            ['h264_mf', '-b:v', b_v],
            ['libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '0']
        ]
        
        for enc_args in encoders_to_try:
            encoder_name = enc_args[0]
            cmd = [
                'ffmpeg',
                '-y',
                '-threads', '0',
                '-i', video_path,
                '-i', mixed_audio_path,
                '-vf', filter_complex,
                '-map', '0:v',
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
                    print(f"Render video thành công bằng encoder: {encoder_name}")
                    return True
                else:
                    err_snippet = proc.stderr[-400:] if proc.stderr else ""
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
