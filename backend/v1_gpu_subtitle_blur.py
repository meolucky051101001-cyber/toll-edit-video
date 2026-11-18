"""GPU-accelerated localized subtitle blur engine (NVDEC + PyTorch CUDA + NVENC).

Replaces FFmpeg's O(N_regions * N_frames) CPU ``split=N`` + ``boxblur`` + ``overlay``
filtergraph with an O(N_active_frames) batched CUDA separable boxblur pass that
touches only active subtitle rectangles while keeping untouched pixels 100% bit-exact.
"""

from __future__ import annotations

import fractions
import logging
import math
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    from . import batch_control
    from .subtitle_blur import blur_regions, resolve_blur_settings
    from .v1_media_streams import MainVideo, validate_video_output
except ImportError:
    import batch_control
    from subtitle_blur import blur_regions, resolve_blur_settings
    from v1_media_streams import MainVideo, validate_video_output

logger = logging.getLogger("v1_gpu_subtitle_blur")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def gpu_localized_blur_available() -> bool:
    """Return True when CUDA GPU localized blur is enabled and available."""
    if os.getenv("V1_GPU_LOCALIZED_BLUR", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def gpu_boxblur_planes_batch(planes_3d: "Any", radius: int, power: int = 3) -> "Any":
    """Apply separable boxblur to [N, H, W] uint8 CUDA tensor matching FFmpeg vf_boxblur.c."""
    import torch
    import torch.nn.functional as F

    if radius <= 0 or power <= 0:
        return planes_3d
    kernel = 2 * int(radius) + 1
    x = planes_3d.unsqueeze(1).float()  # [N, 1, H, W]
    pad_mode = "reflect" if (x.shape[-1] > radius and x.shape[-2] > radius) else "replicate"
    for _ in range(int(power)):
        xp = F.pad(x, (radius, radius, 0, 0), mode=pad_mode)
        x = F.avg_pool2d(xp, kernel_size=(1, kernel), stride=1)
        xp = F.pad(x, (0, 0, radius, radius), mode=pad_mode)
        x = F.avg_pool2d(xp, kernel_size=(kernel, 1), stride=1)
    return torch.clamp(torch.round(x.squeeze(1)), 0, 255).to(torch.uint8)


def _probe_sorted_packet_pts(video_path: str, stream_index: int) -> List[float]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", str(int(stream_index)),
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        video_path,
    ]
    res = batch_control.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=CREATE_NO_WINDOW,
        timeout=30,
    )
    if res.returncode != 0:
        return []
    pts: List[float] = []
    for raw in res.stdout.splitlines():
        token = raw.strip().rstrip(",")
        if not token or token == "N/A":
            continue
        try:
            val = float(token)
            if math.isfinite(val):
                pts.append(val)
        except ValueError:
            continue
    pts.sort()
    if pts and pts[0] < 0.0:
        offset = -pts[0]
        pts = [p + offset for p in pts]
    return pts


def _kill_proc_tree(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=5,
                )
            if proc.poll() is None:
                proc.kill()
    except Exception:
        pass
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _read_exact(pipe: Any, mv_slice: memoryview) -> int:
    got = 0
    target = len(mv_slice)
    while got < target:
        chunk = pipe.readinto(mv_slice[got:])
        if not chunk:
            break
        got += chunk
    return got


def render_video_with_gpu_localized_blur(
    *,
    video_path: str,
    mixed_audio_path: str,
    output_video_path: str,
    source_video: MainVideo,
    blur_boxes: Sequence[Dict[str, Any]],
    blur_settings: Optional[Dict[str, Any]],
    blur_width: int,
    blur_height: int,
    output_width: int,
    output_height: int,
    pre_blur_vf: str,
    post_blur_vf: str,
    enc_args: Sequence[str],
    filter_threads: str = "4",
    tail_pad_seconds: float = 0.0,
    deadline: Optional[float] = None,
) -> Tuple[bool, str]:
    """Stream NVDEC -> CUDA Batched BoxBlur -> NVENC without CPU split branches.

    Returns ``(True, "")`` when rendering and stream validation succeed, or
    ``(False, diagnostic_reason)`` so caller can fall back cleanly.
    """
    import torch

    if not torch.cuda.is_available():
        return False, "CUDA unavailable"

    settings = resolve_blur_settings(blur_settings)
    regions = blur_regions(blur_boxes, blur_width, blur_height, settings)
    if not regions:
        return False, "No blur regions"

    radius_cfg = int(settings["sub_blur_radius"])
    compiled_regions: List[Tuple[int, int, int, int, int, int, List[Tuple[float, float]]]] = []
    for rx, ry, rw, rh, intervals in regions:
        luma_r = min(radius_cfg, (min(rw, rh) - 1) // 2)
        chroma_r = min(luma_r, max(0, (min(rw, rh) // 2 - 1) // 2))
        compiled_regions.append((rx, ry, rw, rh, luma_r, chroma_r, list(intervals)))

    pts_list = _probe_sorted_packet_pts(video_path, source_video.index)
    fps = float(source_video.fps or 30.0)
    if fps <= 0.0 or not math.isfinite(fps):
        fps = 30.0

    is_vfr = False
    if len(pts_list) >= 4:
        base_pts = pts_list[0]
        max_drift = max(
            abs((pts_list[i] - base_pts) - (i / fps))
            for i in range(len(pts_list))
        )
        is_vfr = max_drift > 0.020

    use_nut = False
    av_mod = None
    if is_vfr:
        try:
            import av as _av
            av_mod = _av
            use_nut = True
        except Exception:
            use_nut = False

    for use_cuda_hwaccel in (True, False):
        started = time.monotonic()
        dec_proc: Optional[subprocess.Popen] = None
        enc_proc: Optional[subprocess.Popen] = None
        stderr_chunks: List[bytes] = []

        dec_cmd = ["ffmpeg", "-v", "error", "-nostdin"]
        if use_cuda_hwaccel:
            dec_cmd += ["-hwaccel", "cuda"]
        dec_cmd += ["-threads", "4", "-i", video_path, "-map", source_video.input_map]
        if pre_blur_vf:
            dec_cmd += ["-vf", pre_blur_vf]
        dec_cmd += ["-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]

        enc_cmd = ["ffmpeg", "-v", "warning", "-nostats", "-y"]
        if use_nut:
            enc_cmd += ["-f", "nut", "-i", "-"]
        else:
            enc_cmd += [
                "-f", "rawvideo",
                "-pix_fmt", "yuv420p",
                "-s", f"{blur_width}x{blur_height}",
                "-r", f"{fps:.6f}",
                "-i", "-",
            ]
        enc_cmd += ["-threads", "4", "-filter_threads", str(filter_threads), "-i", mixed_audio_path]

        filter_script_path: Optional[str] = None
        if post_blur_vf:
            if len(post_blur_vf) > 7500:
                filter_script_path = output_video_path + ".gpu_post_filters.txt"
                with open(filter_script_path, "w", encoding="utf-8") as f_scr:
                    f_scr.write(post_blur_vf)
                enc_cmd += ["-filter_script:v", filter_script_path]
            else:
                enc_cmd += ["-vf", post_blur_vf]

        enc_cmd += [
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-sn", "-dn",
            "-c:v", enc_args[0],
        ] + list(enc_args[1:]) + [
            "-pix_fmt", "yuv420p",
            "-fps_mode", "passthrough",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-map_metadata", "-1",
            "-fflags", "+bitexact",
            "-shortest",
            "-shortest_buf_duration", "1",
            output_video_path,
        ]

        y_size = blur_width * blur_height
        uv_size = (blur_width // 2) * (blur_height // 2)
        frame_bytes = y_size + 2 * uv_size
        batch_n = 24 if (blur_width * blur_height <= 1080 * 1920) else 8
        pipe_buf = max(frame_bytes * 4, 4 * 1024 * 1024)

        try:
            dec_proc = subprocess.Popen(
                dec_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=pipe_buf,
                creationflags=CREATE_NO_WINDOW,
            )
            enc_proc = subprocess.Popen(
                enc_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=pipe_buf,
                creationflags=CREATE_NO_WINDOW,
            )

            def _drain_stderr(pipe: Any, sink: List[bytes]) -> None:
                try:
                    data = pipe.read()
                    if data:
                        sink.append(data)
                except Exception:
                    pass

            dec_stderr: List[bytes] = []
            t_dec_err = threading.Thread(target=_drain_stderr, args=(dec_proc.stderr, dec_stderr), daemon=True)
            t_enc_err = threading.Thread(target=_drain_stderr, args=(enc_proc.stderr, stderr_chunks), daemon=True)
            t_dec_err.start()
            t_enc_err.start()

            nut_container = None
            nut_stream = None
            tb = fractions.Fraction(1, 1_000_000)
            if use_nut and av_mod is not None:
                nut_container = av_mod.open(enc_proc.stdin, mode="w", format="nut")
                nut_stream = nut_container.add_stream("rawvideo", rate=fractions.Fraction(1_000_000, 1))
                nut_stream.width = blur_width
                nut_stream.height = blur_height
                nut_stream.pix_fmt = "yuv420p"
                nut_stream.time_base = tb

            chunk_buf = bytearray(frame_bytes * batch_n)
            chunk_mv = memoryview(chunk_buf)
            frame_idx = 0
            blurred_count = 0
            predicate: Optional[Callable[[], bool]] = batch_control.stop_check.get()

            with torch.no_grad():
                while True:
                    if predicate is not None and predicate():
                        raise RuntimeError("Batch stop requested")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(enc_cmd, 0.0)

                    got_bytes = _read_exact(dec_proc.stdout, chunk_mv)
                    n_frames = got_bytes // frame_bytes
                    if n_frames == 0:
                        break

                    by_reg: Dict[int, List[int]] = {}
                    for b_i in range(n_frames):
                        f_i = frame_idx + b_i
                        t_sec = pts_list[f_i] if f_i < len(pts_list) else (f_i / fps)
                        for reg_idx, reg in enumerate(compiled_regions):
                            if any(a <= t_sec < b for a, b in reg[6]):
                                by_reg.setdefault(reg_idx, []).append(b_i)

                    if by_reg:
                        frames_np = np.frombuffer(
                            chunk_mv[:n_frames * frame_bytes], dtype=np.uint8
                        ).reshape(n_frames, frame_bytes)
                        active_bis = {bi for bis in by_reg.values() for bi in bis}
                        blurred_count += len(active_bis)

                        # Preserve exact deterministic order of regions (0 .. R-1)
                        for reg_idx in sorted(by_reg.keys()):
                            b_indices = by_reg[reg_idx]
                            rx, ry, rw, rh, lr, cr, _ = compiled_regions[reg_idx]
                            if lr > 0:
                                y_crops = np.stack(
                                    [
                                        frames_np[bi, :y_size].reshape(blur_height, blur_width)[ry:ry + rh, rx:rx + rw]
                                        for bi in b_indices
                                    ],
                                    axis=0,
                                )
                                y_blurred = gpu_boxblur_planes_batch(
                                    torch.from_numpy(y_crops).cuda(), lr, 3
                                ).cpu().numpy()
                                for k_i, bi in enumerate(b_indices):
                                    frames_np[bi, :y_size].reshape(blur_height, blur_width)[ry:ry + rh, rx:rx + rw] = y_blurred[k_i]
                            if cr > 0:
                                cx, cy, cw, ch = rx // 2, ry // 2, rw // 2, rh // 2
                                u_crops = np.stack(
                                    [
                                        frames_np[bi, y_size:y_size + uv_size].reshape(blur_height // 2, blur_width // 2)[cy:cy + ch, cx:cx + cw]
                                        for bi in b_indices
                                    ],
                                    axis=0,
                                )
                                v_crops = np.stack(
                                    [
                                        frames_np[bi, y_size + uv_size:].reshape(blur_height // 2, blur_width // 2)[cy:cy + ch, cx:cx + cw]
                                        for bi in b_indices
                                    ],
                                    axis=0,
                                )
                                uv_cat = np.concatenate([u_crops, v_crops], axis=0)
                                uv_blurred = gpu_boxblur_planes_batch(
                                    torch.from_numpy(uv_cat).cuda(), cr, 3
                                ).cpu().numpy()
                                m = len(b_indices)
                                for k_i, bi in enumerate(b_indices):
                                    frames_np[bi, y_size:y_size + uv_size].reshape(blur_height // 2, blur_width // 2)[cy:cy + ch, cx:cx + cw] = uv_blurred[k_i]
                                    frames_np[bi, y_size + uv_size:].reshape(blur_height // 2, blur_width // 2)[cy:cy + ch, cx:cx + cw] = uv_blurred[m + k_i]

                    if use_nut and nut_container is not None and nut_stream is not None and av_mod is not None:
                        frames_view = np.frombuffer(
                            chunk_mv[:n_frames * frame_bytes], dtype=np.uint8
                        ).reshape(n_frames, blur_height * 3 // 2, blur_width)
                        for b_i in range(n_frames):
                            f_i = frame_idx + b_i
                            t_sec = pts_list[f_i] if f_i < len(pts_list) else (f_i / fps)
                            vf = av_mod.VideoFrame.from_ndarray(frames_view[b_i], format="yuv420p")
                            vf.pts = int(round(t_sec * 1_000_000))
                            vf.time_base = tb
                            for pkt in nut_stream.encode(vf):
                                nut_container.mux(pkt)
                    else:
                        enc_proc.stdin.write(chunk_mv[:n_frames * frame_bytes])

                    frame_idx += n_frames

            if frame_idx == 0:
                dec_err_txt = b"".join(dec_stderr).decode("utf-8", errors="replace")
                _kill_proc_tree(dec_proc)
                _kill_proc_tree(enc_proc)
                if use_cuda_hwaccel:
                    logger.warning("NVDEC yielded 0 frames (%s); retrying GPU blur with software decode", dec_err_txt[:200])
                    continue
                return False, f"Decoder produced 0 frames: {dec_err_txt[:400]}"

            if use_nut and nut_container is not None and nut_stream is not None:
                for pkt in nut_stream.encode(None):
                    nut_container.mux(pkt)
                nut_container.close()
            if enc_proc.stdin and not enc_proc.stdin.closed:
                enc_proc.stdin.close()

            dec_rc = dec_proc.wait(timeout=30)
            enc_rc = enc_proc.wait(timeout=120)
            t_dec_err.join(timeout=2)
            t_enc_err.join(timeout=2)

            if dec_rc != 0 or enc_rc != 0:
                err_txt = (b"".join(dec_stderr) + b"\n" + b"".join(stderr_chunks)).decode("utf-8", errors="replace")
                if use_cuda_hwaccel and dec_rc != 0:
                    logger.warning("NVDEC exited with %s; retrying GPU blur with software decode", dec_rc)
                    continue
                return False, f"GPU blur pipe failed (dec={dec_rc}, enc={enc_rc}): {err_txt[-1000:]}"

            if not os.path.exists(output_video_path) or os.path.getsize(output_video_path) <= 10000:
                return False, "GPU blur output file missing or empty"

            validate_kwargs: Dict[str, Any] = {
                "require_audio": True,
                "expected_size": (output_width, output_height),
            }
            if tail_pad_seconds > 0.0:
                validate_kwargs["allowed_tail_pad"] = tail_pad_seconds
            validate_video_output(output_video_path, source_video, **validate_kwargs)

            elapsed = time.monotonic() - started
            logger.info(
                "V1 GPU CUDA localized blur render complete encoder=%s seconds=%.2f regions=%d frames=%d blurred=%d mode=%s",
                enc_args[0],
                elapsed,
                len(compiled_regions),
                frame_idx,
                blurred_count,
                "vfr_nut" if use_nut else "cfr_raw",
            )
            return True, ""

        except RuntimeError as exc:
            if "stop requested" in str(exc).lower():
                raise
            return False, f"{type(exc).__name__}: {exc}"
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:
            if use_cuda_hwaccel:
                logger.warning("GPU localized blur attempt (hwaccel=cuda) error: %s; retrying", exc)
                continue
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            _kill_proc_tree(dec_proc)
            _kill_proc_tree(enc_proc)
            if filter_script_path and os.path.exists(filter_script_path):
                try:
                    os.remove(filter_script_path)
                except OSError:
                    pass
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    return False, "GPU localized blur exhausted attempts"
