# -*- coding: utf-8 -*-
"""Watermark & Logo Removal Service for Tool V2.

Adapted and enhanced from Akai1Shuichi/Editor-AI-App.
Features:
1. Mathematical Inverse-Alpha restoration for Google Gemini / Imagen star watermarks.
2. Inverse-Alpha restoration for Google Veo 3 text wordmarks.
3. Dual-mode ('both') to remove both Gemini star and Veo wordmarks in a single pass.
4. Custom ROI Inpainting (OpenCV Telea / Navier-Stokes) for arbitrary logos & watermarks (TikTok, Douyin, CapCut, etc.).
5. Lossless streaming video pipeline with FFmpeg pipes (preserving original audio and high CRF 16 video quality).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, Optional[int], float], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: Optional[float]
    total_frames: Optional[int]


def get_veo_video_watermark_info(width: int, height: int) -> Dict[str, int]:
    """Calculate the Gemini/Veo video watermark box anchor coordinates."""
    min_dim = min(width, height)
    size = max(24, min(int(round(min_dim / 15.0)), min_dim))
    margin = int(round(min_dim / 10.0))
    return {
        "size": size,
        "x": max(0, width - margin - size),
        "y": max(0, height - margin - size),
        "width": size,
        "height": size,
    }


def get_veo3_text_box(width: int, height: int) -> Dict[str, int]:
    """Calculate Google Veo 3 wordmark box anchor coordinates (bottom-right)."""
    factor = min(width, height) / 720.0
    box_width = max(8, round(34 * factor))
    box_height = max(8, round(15 * factor))
    right_margin = max(1, round(16 * factor))
    bottom_margin = max(1, round(16 * factor))
    return {
        "x": max(0, width - right_margin - box_width),
        "y": max(0, height - bottom_margin - box_height),
        "width": min(box_width, width - 1),
        "height": min(box_height, height - 1),
    }


def resolve_video_box(
    anchor: Dict[str, int],
    width: int,
    height: int,
    scale: float = 1.01,
    offset_x: int = -24,
    offset_y: int = -24,
) -> Dict[str, int]:
    """Match center-based position and scale adjustment."""
    size = max(8, min(int(round(anchor["size"] * scale)), min(width, height)))
    center_x = anchor["x"] + anchor["size"] / 2.0 + round(offset_x)
    center_y = anchor["y"] + anchor["size"] / 2.0 + round(offset_y)
    return {
        "size": size,
        "x": max(0, min(int(round(center_x - size / 2.0)), width - size)),
        "y": max(0, min(int(round(center_y - size / 2.0)), height - size)),
        "width": size,
        "height": size,
    }


def heal_upscaled_video_edge_seam(
    frame: np.ndarray, box: Dict[str, int], border: int = 1
) -> np.ndarray:
    """Soften the boundary seam where the cleaned ROI meets the untouched frame."""
    if border < 1:
        return frame

    height, width, _ = frame.shape
    x, y = box["x"], box["y"]
    right, bottom = x + box["width"], y + box["height"]
    source = frame.copy()
    healed = frame.copy()
    x_start, x_end = max(0, x - border), min(width, right + border)
    y_start, y_end = max(0, y - border), min(height, bottom + border)

    for row in range(y_start, y_end):
        for col in range(x_start, x_end):
            inside_core = (
                x + border <= col < right - border
                and y + border <= row < bottom - border
            )
            outside_band = col < x or col >= right or row < y or row >= bottom
            on_inner_boundary = not outside_band and not inside_core
            if not (outside_band or on_inner_boundary):
                continue

            neighbours = source[
                max(0, row - 1) : min(height, row + 2),
                max(0, col - 1) : min(width, col + 2),
            ]
            average = neighbours.astype(np.float32).mean(axis=(0, 1))
            healed[row, col] = np.clip(
                source[row, col].astype(np.float32) * 0.35 + average * 0.65,
                0,
                255,
            ).astype(np.uint8)
    return healed


class WatermarkRemovalService:
    """Core service for video and image watermark removal."""

    def __init__(self, assets_dir: Optional[Union[str, Path]] = None):
        if assets_dir is None:
            self.assets_dir = Path(__file__).resolve().parent.parent / "assets"
        else:
            self.assets_dir = Path(assets_dir)

        bg96_path = self.assets_dir / "bg_96.png"
        bg48_path = self.assets_dir / "bg_48.png"
        veo3_path = self.assets_dir / "veo3_text_720.png"

        if bg96_path.is_file():
            self.bg96_mask = Image.open(bg96_path).convert("RGBA")
        else:
            logger.warning(f"Không tìm thấy mask bg_96.png tại {bg96_path}")
            self.bg96_mask = None

        if bg48_path.is_file():
            self.bg48_mask = Image.open(bg48_path).convert("RGBA")
        else:
            self.bg48_mask = None

        if veo3_path.is_file():
            self.veo3_mask = Image.open(veo3_path).convert("RGBA")
        else:
            logger.warning(f"Không tìm thấy mask veo3_text_720.png tại {veo3_path}")
            self.veo3_mask = None

    def remove_gemini_frame(
        self,
        frame: np.ndarray,
        gain: float = 0.6,
        scale: float = 1.01,
        offset_x: int = -24,
        offset_y: int = -24,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Inverse-alpha removal of Gemini star watermark on a single frame."""
        if self.bg96_mask is None:
            return frame, {}

        height, width, _ = frame.shape
        box = resolve_video_box(
            get_veo_video_watermark_info(width, height),
            width,
            height,
            scale=scale,
            offset_x=offset_x,
            offset_y=offset_y,
        )
        x, y, bw, bh = box["x"], box["y"], box["width"], box["height"]

        mask_resized = self.bg96_mask.resize((bw, bh), Image.Resampling.BICUBIC)
        mask_arr = np.asarray(mask_resized, dtype=np.float32)

        alpha = np.clip(np.max(mask_arr[:, :, :3], axis=2) / 255.0 * gain, 0.0, 0.99)
        active = alpha >= 0.002

        result = frame.copy()
        crop = result[y : y + bh, x : x + bw].astype(np.float32)
        restored = (crop - 255.0 * alpha[:, :, None]) / (1.0 - alpha[:, :, None])
        restored = np.clip(restored + 0.5, 0, 255).astype(np.uint8)

        result[y : y + bh, x : x + bw] = np.where(
            active[:, :, None], restored, result[y : y + bh, x : x + bw]
        )
        return result, {"box": box}

    def remove_veo3_frame(
        self, frame: np.ndarray
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Inverse-alpha removal of Veo 3 wordmark on a single frame."""
        if self.veo3_mask is None:
            return frame, {}

        height, width, _ = frame.shape
        box = get_veo3_text_box(width, height)
        x, y, bw, bh = box["x"], box["y"], box["width"], box["height"]

        mask_resized = self.veo3_mask.resize((bw, bh), Image.Resampling.BICUBIC)
        alpha = np.asarray(mask_resized.getchannel("A"), dtype=np.float32) / 255.0

        result = frame.copy()
        crop = frame[y : y + bh, x : x + bw].astype(np.float32)
        restored = (crop - 255.0 * alpha[:, :, None]) / (1.0 - alpha[:, :, None])
        result[y : y + bh, x : x + bw] = np.clip(restored + 0.5, 0, 255).astype(np.uint8)
        return result, {"box": box}

    def remove_custom_box_frame(
        self,
        frame: np.ndarray,
        box: Dict[str, int],
        method: str = "inpaint",
    ) -> np.ndarray:
        """Inpaint or blur arbitrary ROI box on frame using OpenCV."""
        h, w, _ = frame.shape
        x = max(0, min(int(box.get("x", 0)), w - 1))
        y = max(0, min(int(box.get("y", 0)), h - 1))
        bw = max(1, min(int(box.get("width", 50)), w - x))
        bh = max(1, min(int(box.get("height", 50)), h - y))

        result = frame.copy()
        if method == "blur":
            roi = result[y : y + bh, x : x + bw]
            ksize = (max(3, (bw // 4) * 2 + 1), max(3, (bh // 4) * 2 + 1))
            blurred = cv2.GaussianBlur(roi, ksize, 0)
            result[y : y + bh, x : x + bw] = blurred
        else:
            # OpenCV inpaint
            mask = np.zeros((h, w), dtype=np.uint8)
            mask[y : y + bh, x : x + bw] = 255
            # Dilate mask slightly for smooth boundary
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            mask = cv2.dilate(mask, kernel, iterations=1)
            result = cv2.inpaint(result, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)

        return result

    def probe_video(self, input_path: Union[str, Path]) -> VideoInfo:
        """Probe video dimensions, frame rate, duration, and frame count."""
        source = Path(input_path).resolve()
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,duration,nb_frames",
            "-of",
            "json",
            str(source),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"FFprobe không thể đọc video: {res.stderr}")

        try:
            data = json.loads(res.stdout)
            stream = data["streams"][0]
            w = int(stream["width"])
            h = int(stream["height"])
            r_num, r_den = stream.get("avg_frame_rate", "30/1").split("/")
            fps = float(r_num) / float(r_den) if float(r_den) != 0 else 30.0
            dur_str = stream.get("duration")
            dur = float(dur_str) if dur_str and dur_str != "N/A" else None
            nb_frames = stream.get("nb_frames")
            total_frames = int(nb_frames) if nb_frames and nb_frames.isdigit() else None
            if total_frames is None and dur is not None:
                total_frames = int(round(dur * fps))
            return VideoInfo(
                width=w, height=h, fps=fps, duration=dur, total_frames=total_frames
            )
        except Exception as e:
            raise RuntimeError(f"Không thể giải mã dữ liệu video: {e}")

    def process_video(
        self,
        input_path: Union[str, Path],
        output_path: Union[str, Path],
        mode: str = "both",
        gain: float = 0.6,
        scale: float = 1.01,
        offset_x: int = -24,
        offset_y: int = -24,
        custom_box: Optional[Dict[str, int]] = None,
        custom_method: str = "inpaint",
        progress_callback: Optional[ProgressCallback] = None,
        is_cancelled: Optional[CancelCallback] = None,
    ) -> Dict[str, Any]:
        """Frame-by-frame streaming watermark removal via FFmpeg pipes.

        Modes:
        - 'gemini': Google Gemini / Imagen star watermark
        - 'veo3': Google Veo 3 wordmark
        - 'both': Removes both Gemini star and Veo 3 wordmark
        - 'custom': Inpaint arbitrary bounding box (custom_box={"x", "y", "width", "height"})
        """
        source = Path(input_path).resolve()
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)

        info = self.probe_video(source)
        frame_bytes = info.width * info.height * 3
        total_frames = info.total_frames or (
            round(info.duration * info.fps) if info.duration else 100
        )

        temp_out = output.with_name(f"{output.stem}.temp_wm{output.suffix}")

        decode_cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]

        encode_cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{info.width}x{info.height}",
            "-r",
            f"{info.fps:.6f}",
            "-i",
            "-",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "medium",
            "-crf",
            "16",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(temp_out),
        ]

        flags = (
            subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        )
        decoder = subprocess.Popen(
            decode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags
        )
        encoder = subprocess.Popen(
            encode_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags
        )

        processed = 0
        success = False

        try:
            while True:
                if is_cancelled and is_cancelled():
                    return {
                        "success": False,
                        "cancelled": True,
                        "message": "Đã hủy bởi người dùng",
                    }

                raw = decoder.stdout.read(frame_bytes)
                if not raw:
                    break
                if len(raw) != frame_bytes:
                    break

                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    info.height, info.width, 3
                )

                if mode in ("gemini", "both"):
                    frame, meta = self.remove_gemini_frame(
                        frame,
                        gain=gain,
                        scale=scale,
                        offset_x=offset_x,
                        offset_y=offset_y,
                    )
                    if "box" in meta:
                        frame = heal_upscaled_video_edge_seam(frame, meta["box"])

                if mode in ("veo3", "both"):
                    frame, _ = self.remove_veo3_frame(frame)

                if mode == "custom" and custom_box:
                    frame = self.remove_custom_box_frame(
                        frame, custom_box, method=custom_method
                    )

                encoder.stdin.write(frame.tobytes())
                processed += 1

                if progress_callback:
                    pct = round(
                        (processed / total_frames * 100.0) if total_frames else 0.0, 1
                    )
                    progress_callback(processed, total_frames, min(pct, 99.9))

            encoder.stdin.close()
            dec_ret = decoder.wait()
            enc_ret = encoder.wait()

            if enc_ret != 0:
                err = encoder.stderr.read().decode(errors="replace")
                raise RuntimeError(f"Lỗi mã hóa video: {err}")

            if temp_out.is_file() and temp_out.stat().st_size > 100:
                if output.exists():
                    output.unlink()
                shutil.move(str(temp_out), str(output))
                success = True

            return {
                "success": success,
                "input_path": str(source),
                "output_path": str(output),
                "frames_processed": processed,
                "total_frames": total_frames,
                "mode": mode,
            }
        finally:
            for p in (decoder, encoder):
                if p and p.poll() is None:
                    p.terminate()
            if not success and temp_out.exists():
                try:
                    temp_out.unlink()
                except OSError:
                    pass

    def process_image(
        self,
        input_path: Union[str, Path],
        output_path: Union[str, Path],
        mode: str = "gemini",
        gain: float = 0.6,
        custom_box: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Remove watermark from a static image file (PNG, JPG, WEBP)."""
        source = Path(input_path).resolve()
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)

        img_pil = Image.open(source).convert("RGB")
        frame = np.asarray(img_pil, dtype=np.uint8)

        if mode == "gemini":
            frame, meta = self.remove_gemini_frame(frame, gain=gain)
            if "box" in meta:
                frame = heal_upscaled_video_edge_seam(frame, meta["box"])
        elif mode == "custom" and custom_box:
            frame = self.remove_custom_box_frame(frame, custom_box)

        res_pil = Image.fromarray(frame)
        res_pil.save(output)
        return {
            "success": True,
            "input_path": str(source),
            "output_path": str(output),
            "size": res_pil.size,
        }


# Global singleton instance
_watermark_service_instance: Optional[WatermarkRemovalService] = None


def get_watermark_service() -> WatermarkRemovalService:
    global _watermark_service_instance
    if _watermark_service_instance is None:
        _watermark_service_instance = WatermarkRemovalService()
    return _watermark_service_instance
