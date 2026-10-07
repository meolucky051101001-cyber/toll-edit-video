"""
v1_hierarchical_mixer.py - Bộ trộn âm thanh phân cụm Hierarchical Audio Mixer cho video dài (Tool V1 - Giai đoạn 4).
Khắc phục giới hạn dòng lệnh Windows Win32 (8191 ký tự) khi xử lý hàng trăm đến hàng nghìn câu thoại.
Tối ưu bộ nhớ RAM/VRAM, phân cụm 300s (5 phút), ghép nối an toàn bằng FFmpeg Concat Demuxer.
Hỗ trợ EBU R128 Loudness Normalization và Localized Soft Peak Limiter.
"""

from __future__ import annotations

import os
import sys
import math
import shutil
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydub import AudioSegment
import soundfile as sf
from batch_control import run

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_SIZE_S = 300.0  # Phân cụm 5 phút mỗi block


def _check_cancel():
    from batch_control import stop_check
    predicate = stop_check.get()
    if predicate and predicate():
        raise RuntimeError("Hierarchical mixing stop requested")


def partition_audio_clusters(
    total_duration_s: float,
    dubbing_audio_files: Sequence[Mapping[str, Any]],
    cluster_size_s: float = DEFAULT_CLUSTER_SIZE_S,
) -> List[Dict[str, Any]]:
    """
    Phân chia timeline thành các cụm (clusters) độc lập để trộn song song hoặc nối tiếp:
    Mỗi cluster có: index, start_s, end_s, dubs (các câu thoại có start_s nằm trong cụm).
    """
    if not math.isfinite(cluster_size_s) or cluster_size_s <= 0:
        raise ValueError("cluster_size_s must be positive and finite")
    if total_duration_s <= 0.0:
        return []

    num_clusters = max(1, math.ceil(total_duration_s / cluster_size_s))
    clusters: List[Dict[str, Any]] = []

    sorted_dubs = sorted(
        [d for d in dubbing_audio_files if d],
        key=lambda d: float(d.get("start", 0.0))
    )

    for i in range(num_clusters):
        c_start = i * cluster_size_s
        c_end = min(total_duration_s, (i + 1) * cluster_size_s)

        # Lọc các câu thoại bắt đầu trong cụm này
        c_dubs = []
        for dub in sorted_dubs:
            d_start = float(dub.get("start", 0.0))
            duration = float(dub.get("duration", 0.0) or dub.get("actual_audio_duration", 0.0))
            if duration <= 0:
                duration = max(0.000001, float(dub.get("end", d_start)) - d_start)
            if d_start < c_end and d_start + duration > c_start:
                c_dubs.append(dub)

        clusters.append({
            "index": i,
            "start_s": c_start,
            "end_s": c_end,
            "duration_s": c_end - c_start,
            "dubs": c_dubs,
        })

    return clusters


def mix_hierarchical_audio(
    bgm_path: str,
    dubbing_audio_files: Sequence[Mapping[str, Any]],
    output_path: str,
    cluster_size_s: float = DEFAULT_CLUSTER_SIZE_S,
    base_bgm_gain_db: float = -2.0,
    base_voice_gain_db: float = 1.0,
    ducking_mode: str = "soft",
    apply_ebu_r128: bool = False,
) -> str:
    """
    Trộn âm thanh phân cụm (Hierarchical Mix):
    1. Nếu video ngắn (<= cluster_size_s và <= 100 câu), gọi trực tiếp mix_adaptive_audio.
    2. Nếu video dài, chia nhỏ BGM thành từng cụm, trộn từng cụm bằng mix_adaptive_audio.
    3. Ghép các cụm lại bằng FFmpeg Concat Demuxer qua file list (tránh Win32 command length limit).
    4. Tùy chọn chuẩn hóa âm lượng theo chuẩn EBU R128 (loudnorm).
    """
    from v1_audio_mixer import mix_adaptive_audio
    _check_cancel()

    bgm_p = Path(bgm_path)
    if not bgm_p.is_file():
        raise FileNotFoundError(f"Không tìm thấy file nhạc nền: {bgm_path}")

    # Lấy thời lượng BGM
    bgm_info = sf.info(str(bgm_p))
    total_dur_s = bgm_info.duration

    # Nếu tổng thời lượng nhỏ hoặc số lượng câu ít -> trộn trực tiếp
    if total_dur_s <= cluster_size_s and len(dubbing_audio_files) <= 100:
        return mix_adaptive_audio(
            bgm_path=bgm_path,
            dubbing_audio_files=dubbing_audio_files,
            output_path=output_path,
            base_bgm_gain_db=base_bgm_gain_db,
            base_voice_gain_db=base_voice_gain_db,
            ducking_mode=ducking_mode,
            cluster_dispatch=False,
        )

    logger.info(
        f"[HIERARCHICAL_MIXER] Kích hoạt phân cụm ({total_dur_s:.1f}s, {len(dubbing_audio_files)} câu thoại, cụm={cluster_size_s}s)..."
    )

    from v1_speech_guard import validate_speech_timeline
    measured_dubs = validate_speech_timeline(dubbing_audio_files, total_duration=total_dur_s)
    clusters = partition_audio_clusters(total_dur_s, measured_dubs, cluster_size_s)
    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        cluster_wavs: List[Path] = []

        for c in clusters:
            _check_cancel()
            c_idx = c["index"]
            c_start = c["start_s"]
            c_end = c["end_s"]
            c_dubs = c["dubs"]

            # 1. Trích xuất lát cắt BGM
            c_bgm_file = tmp_path / f"bgm_cluster_{c_idx:03d}.wav"
            with sf.SoundFile(str(bgm_p)) as stream:
                start_frame = round(c_start * stream.samplerate)
                end_frame = round(c_end * stream.samplerate)
                stream.seek(start_frame)
                frames = stream.read(end_frame - start_frame, dtype="int16", always_2d=True)
                sf.write(str(c_bgm_file), frames, stream.samplerate, subtype="PCM_16")
                del frames

            # 2. Offset mốc thời gian dubs về mốc 0 của cluster
            c_local_dubs = []
            for d in c_dubs:
                _check_cancel()
                loc_d = dict(d)
                start = float(d.get("start", 0.0))
                offset = max(0.0, c_start - start)
                loc_d["start"] = max(0.0, start - c_start)
                # A cue crossing a cluster boundary appears in both clusters,
                # but each receives only its own samples (no lost/repeated tail).
                from v1_speech_guard import load_audio
                clip = load_audio(d["path"])
                stop_ms = round(min(len(clip) / 1000.0, c_end - start) * 1000)
                clip = clip[round(offset * 1000):stop_ms]
                if not len(clip):
                    continue
                cue_file = tmp_path / f"cue_{c_idx}_{len(c_local_dubs)}.wav"
                with clip.export(str(cue_file), format="wav"):
                    pass
                loc_d.update(path=str(cue_file), duration=len(clip) / 1000.0)
                c_local_dubs.append(loc_d)

            # 3. Trộn cụm cục bộ
            c_out_wav = tmp_path / f"mixed_cluster_{c_idx:03d}.wav"
            mix_adaptive_audio(
                bgm_path=str(c_bgm_file),
                dubbing_audio_files=c_local_dubs,
                output_path=str(c_out_wav),
                base_bgm_gain_db=base_bgm_gain_db,
                base_voice_gain_db=base_voice_gain_db,
                ducking_mode=ducking_mode,
                cluster_dispatch=False,
            )
            cluster_wavs.append(c_out_wav)

        # 4. Tạo file list concat cho FFmpeg
        file_list_txt = tmp_path / "concat_list.txt"
        with open(file_list_txt, "w", encoding="utf-8") as fl:
            for cw in cluster_wavs:
                clean_path = str(cw.resolve()).replace("\\", "/")
                fl.write(f"file '{clean_path}'\n")

        # 5. Ghép nối các cụm bằng FFmpeg concat demuxer
        raw_concat_wav = tmp_path / "raw_concat.wav"
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(file_list_txt),
            "-c", "copy",
            str(raw_concat_wav)
        ]
        run(
            concat_cmd,
            timeout=max(60, total_dur_s),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True
        )

        # 6. EBU R128 Normalization nếu được bật
        if apply_ebu_r128:
            logger.info("[HIERARCHICAL_MIXER] Áp dụng EBU R128 loudness normalization (I=-16, TP=-1.5)...")
            norm_cmd = [
                "ffmpeg", "-y",
                "-i", str(raw_concat_wav),
                "-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "pcm_s16le",
                str(output_path)
            ]
            run(
                norm_cmd,
                timeout=max(60, total_dur_s * 2),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True
            )
        else:
            shutil.copy2(str(raw_concat_wav), str(output_path))

    return output_path
