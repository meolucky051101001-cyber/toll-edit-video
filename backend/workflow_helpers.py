"""Pure helpers moved out of workflow_api.py (no workspace state, no routes)."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
import cv2
import numpy as np
from fastapi import HTTPException
from pipeline_v2.atomic_io import atomic_copy_file

logger = logging.getLogger("workflow_api")


_STEM_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_STEM_MAX_LEN = 180
_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


def safe_stem(value: Any, field: str = "mã nhận diện video (stem)") -> str:
    """Kiểm tra tên video/job trước khi ghép vào đường dẫn.

    Giữ nguyên khoảng trắng và Unicode (tên tệp thật), nhưng chặn dấu phân cách,
    ký tự điều khiển, tên rỗng và '.', '..' để không thể thoát khỏi thư mục gốc.
    """
    stem = str(value or "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail=f"Thiếu {field}")
    if len(stem) > _STEM_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"{field} quá dài (tối đa {_STEM_MAX_LEN} ký tự)")
    if stem in (".", "..") or any(ch in _STEM_FORBIDDEN_CHARS or ord(ch) < 32 for ch in stem):
        raise HTTPException(status_code=400, detail=f"{field} chứa ký tự không hợp lệ")
    if Path(stem).name != stem or Path(stem).drive:
        raise HTTPException(status_code=400, detail=f"{field} không được chứa đường dẫn")
    return stem


def artifact_stem(stem: str, max_len: Optional[int] = None) -> str:
    """Tên rút gọn an toàn cho thumbnail/tên file xuất (space và ký tự lạ → '_')."""
    cleaned = re.sub(r"[^\w\-_.]", "_", stem or "").strip("._")
    if max_len:
        cleaned = cleaned[:max_len].strip("._")
    return cleaned or "video"


def safe_version(value: Any, field: str = "phiên bản video") -> str:
    version = str(value or "").strip().lower()
    if version not in ("final", "raw"):
        raise HTTPException(status_code=400, detail=f"{field} không hợp lệ (chỉ nhận final/raw)")
    return version


def safe_hex_color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if _HEX_COLOR_RE.fullmatch(text):
        digits = text.lstrip("#")
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return "#" + digits.lower()
    return fallback


def safe_font_name(value: Any, fallback: str = "Arial") -> str:
    name = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._\-]{0,63}", name):
        return name
    return fallback


def resolve_output_dir(value: Any, fallback: Path) -> Path:
    """Validate thư mục xuất: tuyệt đối, không phải tệp, tạo được và trả về đường dẫn resolve."""
    raw = str(value or "").strip().strip('"').strip("'")
    target = Path(raw).expanduser() if raw else Path(fallback)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="Thư mục xuất phải là đường dẫn tuyệt đối")
    try:
        if target.exists() and not target.is_dir():
            raise HTTPException(status_code=400, detail="Đường dẫn xuất đang trỏ tới một tệp, không phải thư mục")
        target = target.resolve(strict=False)
        target.mkdir(parents=True, exist_ok=True)
    except HTTPException:
        raise
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Thư mục xuất không hợp lệ: {e}")
    return target


def detect_letterbox_crop(video_path: str, padding: int = 0) -> Optional[Dict[str, int]]:
    """
    Tự động phát hiện viền đen trên và dưới của video (letterboxing).
    Bảo toàn nội dung tuyệt đối: Quét đa khung hình (10%, 25%, 50%, 75%, 90%),
    lấy ranh giới an toàn nhất (min(top), max(bottom)).
    Áp dụng bù lề an toàn padding:
      - padding < 0 (ví dụ -6px): Nới rộng vùng video ra ngoài, đảm bảo 0% che vào nội dung.
      - padding > 0 (ví dụ +4px): Gọt nhẹ vào trong để loại bỏ vệt xám mờ cạnh viền đen cũ.
    Trả về dict {'w': cw, 'h': ch, 'x': cx, 'y': cy, 'top': top, 'bottom': bottom, 'orig_w': w, 'orig_h': h}
    """
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0 or total_frames <= 0:
            cap.release()
            return None

        sample_indices = [int(total_frames * p) for p in [0.10, 0.25, 0.50, 0.75, 0.90]]
        crops = []
        for idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            row_max = np.max(gray, axis=1)
            top = 0
            while top < (h // 2 - 60) and row_max[top] < 22:
                top += 1
            bottom = h
            while bottom > (h // 2 + 60) and row_max[bottom - 1] < 22:
                bottom -= 1
            crops.append((top, bottom, 0, w))
        cap.release()
        if not crops:
            return None
        # Conservative: Lấy mép cao nhất của top, thấp nhất của bottom để bảo toàn toàn bộ nội dung
        top = min(c[0] for c in crops)
        bottom = max(c[1] for c in crops)

        # Chỉ áp dụng nếu có viền đen trên hoặc dưới đáng kể (>= 16px)
        if top < 16 and (h - bottom) < 16:
            return None

        # Áp dụng bù lề an toàn padding do người dùng kiểm soát
        if padding != 0:
            top = max(0, min(h // 2 - 40, top + padding))
            bottom = min(h, max(h // 2 + 40, bottom - padding))

        # Luôn giữ 100% chiều ngang (2 bên sát mép, không thu nhỏ hay cắt ngang)
        cw = (w // 2) * 2
        ch = ((bottom - top) // 2) * 2
        cx = 0
        cy = (top // 2) * 2
        if cw < 50 or ch < 50:
            return None
        return {
            'w': cw, 'h': ch, 'x': cx, 'y': cy,
            'top': top, 'bottom': bottom,
            'orig_w': w, 'orig_h': h
        }
    except Exception as e:
        logger.warning(f"Lỗi khi detect letterbox crop cho {video_path}: {e}")
        return None


def _hex_to_ass_style_color(hex_str: str, default_hex: str = "000000", alpha: float = 1.0) -> str:
    s = str(hex_str).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        s = default_hex
    r, g, b = s[0:2], s[2:4], s[4:6]
    a_int = max(0, min(255, int((1.0 - alpha) * 255)))
    return f"&H{a_int:02X}{b}{g}{r}"


def build_ass_force_style(cfg: Dict[str, Any], default_font_size: int = 18) -> str:
    """Xây dựng chuỗi force_style cho bộ lọc subtitles của FFmpeg từ cấu hình canvas_settings."""
    font_name = safe_font_name(cfg.get("sub_font_name", "Arial"))
    font_size = int(cfg.get("sub_font_size", default_font_size))
    bold_val = -1 if cfg.get("sub_font_bold", True) else 0

    font_color_hex = safe_hex_color(cfg.get("sub_font_color", "#000000"), "#000000")
    bg_color_hex = safe_hex_color(cfg.get("sub_bg_color", "#ffffff"), "#ffffff")
    bg_opacity = float(cfg.get("sub_bg_opacity", 1.0))
    padding_y = int(cfg.get("sub_box_padding_y", 8))

    stroke_width = int(cfg.get("sub_stroke_width", 0))
    stroke_color_hex = safe_hex_color(cfg.get("sub_stroke_color", "#000000"), "#000000")
    shadow_enabled = bool(cfg.get("sub_shadow", False))
    outline_color_ass = _hex_to_ass_style_color(stroke_color_hex, default_hex="000000", alpha=1.0)
    shadow_val = 2 if shadow_enabled else 0

    primary = _hex_to_ass_style_color(font_color_hex, default_hex="000000", alpha=1.0)
    back = _hex_to_ass_style_color(bg_color_hex, default_hex="ffffff", alpha=bg_opacity)

    pos_y_pct = float(cfg.get("sub_pos_y_pct", 0.82))
    margin_v = max(15, int(1920 * max(0.02, 1.0 - pos_y_pct))) if default_font_size >= 18 else max(10, int(1080 * max(0.02, 1.0 - pos_y_pct)))

    if bg_opacity <= 0.05:
        # Không có hộp nền: Sử dụng BorderStyle=1 (chữ có outline/stroke & shadow)
        border_style = 1
        outline_val = stroke_width if stroke_width > 0 else 0
        outline_color = outline_color_ass
    else:
        # Có hộp nền: Sử dụng BorderStyle=3 (hộp bọc văn bản)
        border_style = 3
        outline_val = max(1, padding_y // 2)
        outline_color = back

    return (
        f"FontName={font_name},"
        f"FontSize={font_size},"
        f"PrimaryColour={primary},"
        f"OutlineColour={outline_color},"
        f"BackColour={back},"
        f"BorderStyle={border_style},"
        f"Outline={outline_val},"
        f"Shadow={shadow_val},"
        f"Alignment=2,"
        f"MarginV={margin_v},"
        f"Bold={bold_val}"
    )


def _srt_source_copy(path: Path) -> Path:
    """Bản SRT chuẩn theo timeline video gốc (raw), tách khỏi bản đã retime theo timeline xuất."""
    return path.with_name(f"{path.stem}.source{path.suffix}")


def _srt_prev_copy(path: Path) -> Path:
    """Trạng thái SRT ngay trước lần retime gần nhất (để khôi phục nếu render sai)."""
    return path.with_name(f"{path.stem}.prev{path.suffix}")


def _snapshot_srt_source(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        return
    try:
        atomic_copy_file(path, _srt_source_copy(path))
    except Exception as e:
        logger.warning(f"Không lưu được bản SRT gốc cho {path.name}: {e}")


def _backup_srt_before_render(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        return
    try:
        atomic_copy_file(path, _srt_prev_copy(path))
    except Exception as e:
        logger.warning(f"Không sao lưu được {path.name} trước khi retime: {e}")


def _payload_video_version(payload: Dict[str, Any], job_dir: Path, stem: str) -> str:
    """'raw' khi timeline phụ đề bám video gốc; 'final' khi bám bản đã render."""
    ver = str(payload.get("video_version") or "").strip().lower()
    if ver in ("raw", "input", "final"):
        return "raw" if ver in ("raw", "input") else "final"
    try:
        final_names = (f"final_{stem}.mp4", f"final_{artifact_stem(stem)}.mp4")
        if any((job_dir / name).is_file() for name in final_names):
            return "final"
    except OSError as _phase6_exc:
        logger.debug('Suppressed error in workflow_api._payload_video_version (optional operation): %s', type(_phase6_exc).__name__, exc_info=False)
    return "raw"


def build_watermark_filter(cfg: Dict[str, Any], tw: int, th: int) -> str:
    """Tạo bộ lọc FFmpeg drawtext cho Watermark bản quyền mờ mờ cố định ở góc bên phải video hoặc vị trí tùy chỉnh."""
    if not cfg.get("watermark_enabled", False):
        return ""
    text = str(cfg.get("watermark_text", "")).strip()
    if not text:
        return ""

    # Thoát ký tự đặc biệt cho ffmpeg drawtext (bao gồm dấu xuống dòng)
    clean_text = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )
    try:
        opacity = float(cfg.get("watermark_opacity", 0.45))
    except (ValueError, TypeError):
        opacity = 0.45
    opacity = max(0.05, min(1.0, opacity))

    try:
        base_font_size = int(cfg.get("watermark_font_size", 22))
    except (ValueError, TypeError):
        base_font_size = 22
    scale_factor = max(0.6, min(2.5, th / 1920.0))
    font_size = max(12, int(base_font_size * scale_factor))

    # Màu chữ watermark
    color_hex = "0x" + safe_hex_color(cfg.get("watermark_color", "#ffffff"), "#ffffff")[1:]

    pos = str(cfg.get("watermark_position", "top-right")).lower()
    try:
        base_margin_x = int(cfg.get("watermark_margin_x", 25))
        base_margin_y = int(cfg.get("watermark_margin_y", 25))
    except (ValueError, TypeError):
        base_margin_x = 25
        base_margin_y = 25
    margin_x = max(10, int(base_margin_x * (tw / 1080.0)))
    margin_y = max(10, int(base_margin_y * (th / 1920.0)))

    # Vị trí hiển thị (4 góc hoặc Tự do)
    if pos == "custom":
        try:
            pos_x_pct = float(cfg.get("watermark_pos_x_pct", 0.85))
            pos_y_pct = float(cfg.get("watermark_pos_y_pct", 0.05))
        except (ValueError, TypeError):
            pos_x_pct, pos_y_pct = 0.85, 0.05
        pos_x_pct = max(0.02, min(0.98, pos_x_pct))
        pos_y_pct = max(0.02, min(0.98, pos_y_pct))
        x_expr = f"(w*{pos_x_pct:.4f})-(tw/2)"
        y_expr = f"(h*{pos_y_pct:.4f})-(th/2)"
    elif pos == "bottom-right":
        x_expr = f"w-tw-{margin_x}"
        y_expr = f"h-th-{margin_y}"
    elif pos == "top-left":
        x_expr = f"{margin_x}"
        y_expr = f"{margin_y}"
    elif pos == "bottom-left":
        x_expr = f"{margin_x}"
        y_expr = f"h-th-{margin_y}"
    else:  # Mặc định góc trên phải
        x_expr = f"w-tw-{margin_x}"
        y_expr = f"{margin_y}"

    # Tùy chọn khung huy hiệu mờ bo quanh chữ
    box_filter = ""
    if cfg.get("watermark_bg_enabled", False):
        bg_hex = "0x" + safe_hex_color(cfg.get("watermark_bg_color", "#000000"), "#000000")[1:]
        try:
            bg_op = float(cfg.get("watermark_bg_opacity", 0.40))
        except (ValueError, TypeError):
            bg_op = 0.40
        bg_op = max(0.05, min(1.0, bg_op))
        box_border_w = max(4, int(8 * scale_factor))
        box_filter = f":box=1:boxcolor={bg_hex}@{bg_op:.2f}:boxborderw={box_border_w}"

    fontfile = r"C\:/Windows/Fonts/arial.ttf"
    shadow_opacity = round(opacity * 0.75, 2)
    filt = (
        f",drawtext=fontfile='{fontfile}':text='{clean_text}':"
        f"fontsize={font_size}:fontcolor={color_hex}@{opacity:.2f}:"
        f"shadowcolor=black@{shadow_opacity:.2f}:shadowx=2:shadowy=2"
        f"{box_filter}:x={x_expr}:y={y_expr}"
    )
    return filt


def _atempo_chain(speed: float) -> List[str]:
    """
    Chuỗi atempo để giữ cao độ giọng khi đổi tốc độ.
    FFmpeg chỉ nhận atempo trong [0.5, 2.0] nên phải phân tích thành tích các hệ số.
    """
    parts: List[str] = []
    s = float(speed)
    while s > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        s *= 2.0
    if abs(s - 1.0) > 1e-6:
        parts.append(f"atempo={s:.5f}")
    return parts


def _hex_to_ass_color(hex_color: str, alpha: str = "00") -> str:
    """'#RRGGBB' → '&HAABBGGRR' (ASS dùng BGR; alpha 00 = đục)."""
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", (hex_color or "").strip())
    if not m:
        return f"&H{alpha}FFFFFF"
    v = m.group(1).upper()
    r, g, b = v[0:2], v[2:4], v[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ass_time(seconds: float) -> str:
    """Giây → 'H:MM:SS.CC' (centisecond) theo chuẩn ASS."""
    s = max(0.0, float(seconds))
    cs = int(round(s * 100))
    h, rem = divmod(cs, 360000)
    mm, rem = divmod(rem, 6000)
    ss, cc = divmod(rem, 100)
    return f"{h}:{mm:02d}:{ss:02d}.{cc:02d}"


def _timeline_to_output_t(clips_with_at: List[Dict[str, Any]], graph: Dict[str, Any], t: float) -> float:
    """
    Map giây trên TIMELINE (có gap) → giây trong FILE XUẤT (gap bị concat ghép lại).
    Dùng cho text/overlay để chữ xuất hiện đúng nội dung như preview (P3).
    """
    seg_starts = graph.get("seg_starts") or []
    seg_durs = graph.get("seg_durs") or []
    if not seg_starts:
        return max(0.0, float(t or 0.0))
    best = None
    best_at = None
    for i, c in enumerate(clips_with_at):
        at_i = max(0.0, float(c.get("at", 0.0) or 0.0))
        if at_i <= float(t or 0.0) + 1e-6 and (best_at is None or at_i >= best_at):
            best, best_at = i, at_i
    if best is None:
        return float(seg_starts[0])
    at_i = best_at or 0.0
    dur_i = float(seg_durs[best]) if best < len(seg_durs) else 0.0
    local = min(max(0.0, float(t or 0.0) - at_i), dur_i)
    return float(seg_starts[best]) + local


def _decode_overlay_assets(overlay_clips: List[Dict[str, Any]], workspace_dir: Path) -> List[Optional[Path]]:
    """Giải mã dataURL ảnh overlay → file trong <workspace>/overlay_assets (P3). None nếu hỏng."""
    import base64

    assets_dir = Path(workspace_dir) / "overlay_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Optional[Path]] = []
    for i, c in enumerate(overlay_clips or []):
        src = str(c.get("src") or "")
        m = re.match(r"data:image/(png|jpeg|jpg|webp);base64,", src)
        if not m:
            paths.append(None)
            continue
        ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
        try:
            raw = base64.b64decode(src[m.end():], validate=False)
        except Exception:
            paths.append(None)
            continue
        cid = re.sub(r"[^\w\-]", "", str(c.get("id", "")))[:40] or f"ov{i}"
        p = assets_dir / f"{cid}.{ext}"
        p.write_bytes(raw)
        paths.append(p)
    return paths


def _decode_audio_assets(audio_clips: List[Dict[str, Any]], workspace_dir: Path) -> List[Optional[Path]]:
    """P5: giải mã dataURL nhạc/SFX → file trong <workspace>/audio_assets. None nếu hỏng."""
    import base64

    assets_dir = Path(workspace_dir) / "audio_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Optional[Path]] = []
    for i, c in enumerate(audio_clips or []):
        src = str(c.get("src") or "")
        m = re.match(r"data:audio/(mpeg|mp3|wav|x-wav|ogg|aac|m4a|mp4);base64,", src)
        if not m:
            paths.append(None)
            continue
        ext = {"mpeg": "mp3", "x-wav": "wav", "mp4": "m4a"}.get(m.group(1), m.group(1))
        try:
            raw = base64.b64decode(src[m.end():], validate=False)
        except Exception:
            paths.append(None)
            continue
        cid = re.sub(r"[^\w\-]", "", str(c.get("id", "")))[:40] or f"au{i}"
        p = assets_dir / f"{cid}.{ext}"
        p.write_bytes(raw)
        paths.append(p)
    return paths


def _build_music_filter_lines(
    music_meta: List[Dict[str, Any]],
    base_tag: str,
    total_dur: float,
) -> Dict[str, Any]:
    """
    P5: sinh filter trộn các clip nhạc/SFX lên audio nền (hàm thuần để unit test).

    music_meta: [{a_idx (index input ffmpeg), at_out, dur_out, volume, fadeIn, fadeOut,
                  muted, duck}] — at_out/dur_out đã map sang trục file xuất.
    base_tag: tag audio nền (vd '[basea]') — audio đã mix sẵn trong video (giọng AI + BGM).
    Ducking: sidechaincompress nén nhạc theo mức âm của audio nền (giọng nói).
    Trả {lines, used_split} — lines kết thúc bằng [outa], độ dài bung đủ total_dur.
    """
    lines: List[str] = []
    active = [m for m in music_meta if not m.get("muted")]
    ducked = [m for m in active if m.get("duck")]
    if not active:
        # tất cả bị tắt tiếng → giữ nguyên audio nền để không mất [outa]
        lines.append(f"{base_tag}anull[outa]")
        return {"lines": lines, "used_split": False}

    used_split = bool(ducked)
    mix_inputs: List[str] = []
    if used_split:
        split_tags = "".join(f"[bs{j}]" for j in range(len(ducked) + 1))
        lines.append(f"{base_tag}asplit={len(ducked) + 1}{split_tags}")
        base_mix_tag = "[bs0]"
    else:
        base_mix_tag = base_tag
    mix_inputs.append(base_mix_tag)

    duck_i = 0
    for k, m in enumerate(active):
        chain = ["aformat=sample_rates=44100:channel_layouts=stereo"]
        chain.append(f"atrim=start=0:end={m['dur_out']:.3f}")
        if abs(float(m.get("volume", 1.0)) - 1.0) > 1e-6:
            chain.append(f"volume={float(m.get('volume', 1.0)):.5f}")
        fade_in = float(m.get("fade_in", 0.0) or 0.0)
        fade_out = float(m.get("fade_out", 0.0) or 0.0)
        if fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            chain.append(f"afade=t=out:st={max(0.0, m['dur_out'] - fade_out):.3f}:d={fade_out:.3f}")
        chain.append(f"adelay={int(round(float(m.get('at_out', 0.0)) * 1000))}:all=1")
        src = f"[{int(m.get('a_idx', 0))}:a]"
        if m.get("duck"):
            out_tag = f"[amd{k}]"
            lines.append(f"{src}{','.join(chain)}[amraw{k}]")
            lines.append(
                f"[amraw{k}][bs{duck_i + 1}]sidechaincompress=threshold=0.03:ratio=6:attack=80:release=500{out_tag}"
            )
            duck_i += 1
        else:
            out_tag = f"[am{k}]"
            lines.append(f"{src}{','.join(chain)}{out_tag}")
        mix_inputs.append(out_tag)

    lines.append(
        f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:normalize=0:duration=longest[mxout]"
    )
    lines.append(f"[mxout]apad=whole_dur={max(0.1, float(total_dur or 0.0)):.3f}[outa]")
    return {"lines": lines, "used_split": used_split}


def _build_tts_mix_filter_lines(audio_tag: str, tts_idx: int, base_volume: float = 1.0) -> str:
    """Trộn track lồng tiếng AI lên audio gốc; audio_tag là stream specifier như "0:a?"."""
    base_tag = audio_tag.replace("?", "")
    vol_str = "1.0" if abs(float(base_volume) - 1.0) < 1e-6 else f"{max(0.0, float(base_volume)):.2f}"
    return (
        f"[{base_tag}]aformat=sample_rates=44100:channel_layouts=stereo,volume={vol_str}[expbase];"
        f"[{tts_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo[exptts];"
        f"[expbase][exptts]amix=inputs=2:normalize=0:duration=first[expaout]"
    )


def _proxy_is_fresh(proxy_path: Path, src_path: Path) -> bool:
    return proxy_path.is_file() and proxy_path.stat().st_size > 1_000 and \
        proxy_path.stat().st_mtime >= src_path.stat().st_mtime - 1.0


def glob_escape(s: str) -> str:
    return s.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def _compose_bilingual_srt(orig_subs: List[Dict[str, Any]], trans_subs: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx in range(max(len(trans_subs), len(orig_subs))):
        ts = trans_subs[idx] if idx < len(trans_subs) else None
        os_sub = orig_subs[idx] if idx < len(orig_subs) else None
        s_str = ts["start_str"] if ts else os_sub["start_str"]
        e_str = ts["end_str"] if ts else os_sub["end_str"]
        viet_txt = ts["text"] if ts else ""
        orig_txt = os_sub["text"] if os_sub else ""
        lines.append(str(idx + 1))
        lines.append(f"{s_str} --> {e_str}")
        if viet_txt:
            lines.append(viet_txt)
        if orig_txt:
            lines.append(orig_txt)
        lines.append("")
    return "\n".join(lines)


def format_srt_timestamp(seconds: float) -> str:
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        seconds += 1
        millis = 0
    s = int(seconds)
    hours = s // 3600
    minutes = (s % 3600) // 60
    secs = s % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _export_stage(job_ctl: Optional[Dict[str, Any]], pct: int, message: str) -> None:
    if not job_ctl:
        return
    cb = job_ctl.get("stage_cb")
    if cb:
        try:
            cb(pct, message)
        except Exception as _phase6_exc:
            logger.debug('Suppressed error in workflow_api._export_stage (cb): %s', type(_phase6_exc).__name__, exc_info=False)


def _check_export_cancelled(job_ctl: Optional[Dict[str, Any]]) -> None:
    if job_ctl and job_ctl.get("cancelled"):
        raise HTTPException(status_code=499, detail="Đã huỷ xuất video")
