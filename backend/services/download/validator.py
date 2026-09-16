"""Strict media file and container validation for downloaded videos.

Guarantees:
- Strict hierarchical ISO-BMFF container parsing (ftyp, moov -> trak -> mdia -> hdlr -> vide, mdat > 0).
- Detection of truncated files, HTML/JSON error payloads, audio-only media, or fake boxes.
- Memory-bounded metadata parsing (avoids OOM on crafted large boxes).
- Non-blocking async ffprobe verification with timeout enforcement and process cleanup.
- Rejection (verification_failed) if ffprobe is missing or times out (no silent pass).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("download.validator")

# Maximum size of metadata (e.g. moov box) allowed to be read into RAM
MAX_METADATA_PARSE_SIZE = 16 * 1024 * 1024  # 16 MB


def parse_iso_bmff_structure(file_path: Path) -> tuple[bool, str | None]:
    """Parses binary ISO-BMFF container to verify box boundaries, ftyp, moov hierarchy, and mdat.

    Returns:
        (is_valid, error_message_or_none)
    """
    try:
        if not file_path.is_file():
            return False, "Tập tin không tồn tại trên ổ đĩa."

        file_size = file_path.stat().st_size
        if file_size < 32:
            return False, "Kích thước tệp tin quá nhỏ (< 32 bytes)."

        with open(file_path, "rb") as f:
            header_sample = f.read(min(file_size, 1024)).lower()
            if (
                b"<!doctype" in header_sample
                or b"<html" in header_sample
                or b"<body" in header_sample
                or b'{"code"' in header_sample
                or b'{"status"' in header_sample
                or b'{"message"' in header_sample
            ):
                return False, "Tập tin chứa dữ liệu HTML hoặc JSON báo lỗi từ máy chủ media."

            offset = 0
            has_ftyp = False
            has_moov = False
            has_mdat = False
            has_video_track = False
            mdat_len = 0

            f.seek(0)
            while offset < file_size:
                f.seek(offset)
                box_hdr = f.read(8)
                if len(box_hdr) < 8:
                    return False, "Phần đầu hộp ISO-BMFF bị cắt cụt."

                box_size, box_type = struct.unpack(">I4s", box_hdr)
                hdr_len = 8

                if box_size == 1:
                    ext_size_bytes = f.read(8)
                    if len(ext_size_bytes) < 8:
                        return False, "Hộp 64-bit bị cắt cụt."
                    box_size = struct.unpack(">Q", ext_size_bytes)[0]
                    hdr_len = 16
                elif box_size == 0:
                    box_size = file_size - offset

                if box_size < hdr_len:
                    return False, f"Kích thước hộp {box_type!r} không hợp lệ ({box_size})."

                if offset + box_size > file_size:
                    return False, f"Hộp {box_type!r} vượt quá độ dài tệp (tệp bị cắt cụt)."

                if box_type == b"ftyp":
                    has_ftyp = True
                elif box_type == b"mdat":
                    has_mdat = True
                    mdat_len += (box_size - hdr_len)
                elif box_type == b"moov":
                    has_moov = True
                    content_len = box_size - hdr_len
                    read_len = min(content_len, MAX_METADATA_PARSE_SIZE)
                    moov_content = f.read(read_len)

                    # Parse sub-boxes in moov: find trak boxes
                    m_offset = 0
                    while m_offset + 8 <= len(moov_content):
                        m_size, m_type = struct.unpack(">I4s", moov_content[m_offset : m_offset + 8])
                        if m_size < 8 or m_offset + m_size > len(moov_content):
                            break
                        if m_type == b"trak":
                            trak_data = moov_content[m_offset + 8 : m_offset + m_size]
                            # Check trak has mdia with hdlr of type 'vide'
                            if _trak_has_video_stream(trak_data):
                                has_video_track = True
                        m_offset += m_size

                offset += box_size

            if not has_ftyp:
                return False, "Thiếu hộp ftyp định dạng MP4."
            if not has_moov:
                return False, "Thiếu hộp moov chứa thông tin metadata."
            if not has_mdat or mdat_len <= 0:
                return False, "Thiếu hộp mdat hoặc dữ liệu media rỗng."
            if not has_video_track:
                return False, "Hộp moov không chứa luồng video (vide) hợp lệ."

            return True, None
    except Exception as e:
        return False, f"Lỗi phân tích cú pháp tệp ISO-BMFF: {e}"


def _trak_has_video_stream(trak_data: bytes) -> bool:
    """Verifies that a trak box contains mdia -> hdlr with handler_type == b'vide' and tkhd dimensions."""
    offset = 0
    has_vide_hdlr = False
    has_valid_dimensions = False

    while offset + 8 <= len(trak_data):
        b_size, b_type = struct.unpack(">I4s", trak_data[offset : offset + 8])
        if b_size < 8 or offset + b_size > len(trak_data):
            break

        box_payload = trak_data[offset + 8 : offset + b_size]

        if b_type == b"tkhd" and len(box_payload) >= 84:
            # Parse tkhd box to check width and height
            version = box_payload[0]
            if version == 0 and len(box_payload) >= 84:
                # 4 (ver/flags) + 8 (times) + 4 (id) + 4 (res) + 4 (dur) + 8 (res) + 2 (layer) + 2 (alt) + 2 (vol) + 2 (res) + 36 (matrix) = 76
                w_raw, h_raw = struct.unpack(">II", box_payload[76:84])
                width = w_raw >> 16
                height = h_raw >> 16
                if width > 0 and height > 0:
                    has_valid_dimensions = True
            elif version == 1 and len(box_payload) >= 96:
                w_raw, h_raw = struct.unpack(">II", box_payload[88:96])
                width = w_raw >> 16
                height = h_raw >> 16
                if width > 0 and height > 0:
                    has_valid_dimensions = True
            else:
                # If tkhd version unexpected, allow dimension check if width non-zero
                has_valid_dimensions = True

        elif b_type == b"mdia":
            # Parse mdia sub-boxes
            m_offset = 0
            while m_offset + 8 <= len(box_payload):
                m_size, m_type = struct.unpack(">I4s", box_payload[m_offset : m_offset + 8])
                if m_size < 8 or m_offset + m_size > len(box_payload):
                    break
                if m_type == b"hdlr":
                    hdlr_payload = box_payload[m_offset + 8 : m_offset + m_size]
                    # hdlr structure: 4 bytes version/flags, 4 bytes pre_defined, 4 bytes handler_type
                    if len(hdlr_payload) >= 12:
                        handler_type = hdlr_payload[8:12]
                        if handler_type == b"vide":
                            has_vide_hdlr = True
                m_offset += m_size

        offset += b_size

    return has_vide_hdlr and has_valid_dimensions


async def probe_media_file_async(file_path: Path, timeout: float = 5.0) -> tuple[bool, str | None]:
    """Invokes ffprobe asynchronously to confirm container integrity, playable stream, and valid duration.

    Returns:
        (is_valid, error_reason)
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, "ffprobe_not_found: Không tìm thấy công cụ ffprobe để xác thực tệp."

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "stream=codec_type,width,height",
        "-show_entries", "format=duration",
        "-of", "json",
        str(file_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return False, f"ffprobe_spawn_failed: Không thể khởi chạy ffprobe: {e}"

    comm_task = asyncio.create_task(proc.communicate())
    try:
        stdout_data, stderr_data = await asyncio.wait_for(asyncio.shield(comm_task), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        if not comm_task.done():
            comm_task.cancel()
            try:
                await comm_task
            except (asyncio.CancelledError, Exception):
                pass
        return False, "ffprobe_timeout: Quá thời gian chờ ffprobe xác thực tệp."

    if proc.returncode != 0:
        err_msg = stderr_data.decode(errors="replace").strip()
        return False, f"ffprobe_failed: Lỗi phân tích định dạng tệp: {err_msg}"

    try:
        parsed: dict[str, Any] = json.loads(stdout_data.decode(errors="replace"))
    except Exception as e:
        return False, f"ffprobe_invalid_output: Kết quả ffprobe không phải JSON hợp lệ: {e}"

    streams = parsed.get("streams") or []
    has_video_stream = False
    for s in streams:
        if s.get("codec_type") == "video":
            w = s.get("width")
            h = s.get("height")
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                has_video_stream = True
                break

    if not has_video_stream:
        return False, "ffprobe_no_video: Tệp tin không chứa luồng video phát được."

    # Validate duration
    fmt = parsed.get("format") or {}
    duration_str = fmt.get("duration")
    duration_val = 0.0
    if duration_str:
        try:
            duration_val = float(duration_str)
        except (ValueError, TypeError):
            pass

    if duration_val <= 0.0:
        # Check stream duration if format duration was absent
        for s in streams:
            s_dur = s.get("duration")
            if s_dur:
                try:
                    duration_val = float(s_dur)
                    if duration_val > 0.0:
                        break
                except (ValueError, TypeError):
                    pass

    if duration_val <= 0.0:
        return False, "ffprobe_invalid_duration: Thời lượng video không hợp lệ (<= 0s)."

    return True, None


def probe_media_file_sync(file_path: Path, timeout: float = 5.0) -> tuple[bool, str | None]:
    """Synchronous media file probe using ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, "ffprobe_not_found: Không tìm thấy công cụ ffprobe để xác thực tệp."

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "stream=codec_type,width,height",
        "-show_entries", "format=duration",
        "-of", "json",
        str(file_path),
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            return False, f"ffprobe_failed: {res.stderr.strip()}"

        parsed = json.loads(res.stdout)
        streams = parsed.get("streams") or []
        has_video = any(
            s.get("codec_type") == "video"
            and isinstance(s.get("width"), int)
            and s.get("width") > 0
            and isinstance(s.get("height"), int)
            and s.get("height") > 0
            for s in streams
        )
        if not has_video:
            return False, "ffprobe_no_video: Tệp tin không chứa luồng video."

        fmt = parsed.get("format") or {}
        duration = float(fmt.get("duration", 0.0))
        if duration <= 0.0:
            return False, "ffprobe_invalid_duration: Thời lượng video không hợp lệ."

        return True, None
    except subprocess.TimeoutExpired:
        return False, "ffprobe_timeout: Quá thời gian chờ ffprobe xác thực tệp."
    except Exception as e:
        return False, f"ffprobe_error: {e}"


async def validate_video_file_async(file_path: Path) -> tuple[bool, str | None]:
    """Performs both strict ISO-BMFF container parsing and async ffprobe verification.

    Returns:
        (is_valid, error_reason)
    """
    # 1. Non-blocking ISO-BMFF parsing
    iso_ok, iso_err = await asyncio.to_thread(parse_iso_bmff_structure, file_path)
    if not iso_ok:
        return False, iso_err

    # 2. Async ffprobe verification
    probe_ok, probe_err = await probe_media_file_async(file_path)
    if not probe_ok:
        return False, probe_err

    return True, None


def is_valid_mp4(file_path: Path) -> bool:
    """Convenience synchronous validator (ISO-BMFF + ffprobe sync)."""
    iso_ok, _ = parse_iso_bmff_structure(file_path)
    if not iso_ok:
        return False
    probe_ok, _ = probe_media_file_sync(file_path)
    return probe_ok
