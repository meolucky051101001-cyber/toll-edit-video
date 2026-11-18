"""Tests for GPU CUDA localized subtitle blur and multi-stage GPU speedups."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import torch

from backend.v1_gpu_subtitle_blur import (
    gpu_boxblur_planes_batch,
    gpu_localized_blur_available,
    render_video_with_gpu_localized_blur,
)
from backend.v1_media_streams import probe_main_video


def test_gpu_boxblur_planes_batch_matches_ffmpeg_vf_boxblur(tmp_path: Path):
    if not torch.cuda.is_available() or not shutil.which("ffmpeg"):
        pytest.skip("Requires CUDA and FFmpeg")

    h, w = 66, 318
    yy, xx = np.indices((h, w))
    plane = ((xx * 23 + yy * 17) % 220 + 16).astype(np.uint8)
    batched = np.stack([plane, np.flip(plane, axis=1)], axis=0)

    gpu_res = gpu_boxblur_planes_batch(torch.from_numpy(batched).cuda(), radius=12, power=3).cpu().numpy()
    assert gpu_res.shape == (2, h, w)

    raw_in = tmp_path / "in.yuv"
    raw_out = tmp_path / "out.yuv"
    raw_in.write_bytes(plane.tobytes() + bytes([128]) * (h * w // 2))
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-i", str(raw_in),
            "-vf", "boxblur=luma_radius=12:luma_power=3:chroma_radius=6:chroma_power=3",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", str(raw_out),
        ],
        check=True,
    )
    ff_plane = np.frombuffer(raw_out.read_bytes()[: h * w], dtype=np.uint8).reshape(h, w)
    diff = np.abs(gpu_res[0].astype(int) - ff_plane.astype(int))
    assert diff.max() <= 2
    assert diff.mean() < 0.6


def test_gpu_localized_blur_end_to_end_render_validates_streams(tmp_path: Path):
    if not gpu_localized_blur_available() or not shutil.which("ffmpeg"):
        pytest.skip("Requires CUDA GPU + FFmpeg NVENC")

    src_mp4 = tmp_path / "src.mp4"
    mix_wav = tmp_path / "mix.wav"
    out_mp4 = tmp_path / "out.mp4"

    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src_mp4),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
            "-ac", "2", str(mix_wav),
        ],
        check=True,
    )

    info = probe_main_video(str(src_mp4))
    boxes = [
        {"start": 0.1 * i, "end": 0.1 * i + 0.08, "x_pct": 0.1 + 0.02 * (i % 5), "max_x_pct": 0.85, "y_pct": 0.75, "max_y_pct": 0.84}
        for i in range(12)
    ]
    ok, reason = render_video_with_gpu_localized_blur(
        video_path=str(src_mp4),
        mixed_audio_path=str(mix_wav),
        output_video_path=str(out_mp4),
        source_video=info,
        blur_boxes=boxes,
        blur_settings={"sub_blur_radius": 12, "sub_blur_padding_x": 0.01, "sub_blur_padding_y": 0.01},
        blur_width=360,
        blur_height=640,
        output_width=360,
        output_height=640,
        pre_blur_vf="",
        post_blur_vf="format=yuv420p",
        enc_args=["h264_nvenc", "-preset", "p4", "-b:v", "2500k"],
    )
    assert ok, reason
    assert out_mp4.is_file() and out_mp4.stat().st_size > 10000


def test_ocr_worker_cuda_session_injects_cudnn_heuristic():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "backend" / "model_workers" / "v1_ocr_worker.py"
    spec = importlib.util.spec_from_file_location("ocr_heuristic_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured = {}

    class FakeSession:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def disable_fallback(self):
            pass

        def get_providers(self):
            return ["CUDAExecutionProvider"]

    class FakeOpts:
        def __init__(self):
            self.graph_optimization_level = None

    fake_ort = SimpleNamespace(
        InferenceSession=FakeSession,
        SessionOptions=FakeOpts,
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=99),
        preload_dlls=lambda **kw: None,
    )
    with patch.dict("sys.modules", {"onnxruntime": fake_ort}):
        module._configure_cuda()
        # Case 1: provider_options is None
        fake_ort.InferenceSession("model.onnx", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        assert ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}) in captured["providers"]
        assert captured["sess_options"].graph_optimization_level == 99

        # Case 2: PaddleX passes parallel provider_options=[{"device_id": 0}, {}]
        captured.clear()
        fake_ort.InferenceSession(
            "model.onnx",
            sess_options=FakeOpts(),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            provider_options=[{"device_id": 0}, {}],
        )
        assert captured["providers"] == ["CUDAExecutionProvider", "CPUExecutionProvider"]
        assert captured["provider_options"][0] == {"device_id": 0, "cudnn_conv_algo_search": "HEURISTIC"}
        assert captured["provider_options"][1] == {}
        assert captured["sess_options"].graph_optimization_level == 99


def test_conditional_asr_retranscribe_uses_in_memory_float32_slice(tmp_path: Path):
    from pydub.generators import Sine
    from backend.ai.v1_conditional_asr import retranscribe_gap_snippet

    seg_audio = Sine(300).to_audio_segment(duration=2000, volume=-12)
    fake_model = MagicMock()
    fake_seg = SimpleNamespace(
        text="你好世界",
        start=0.1,
        end=1.2,
        no_speech_prob=0.05,
        avg_logprob=-0.2,
        compression_ratio=1.1,
    )
    fake_model.transcribe.return_value = ([fake_seg], None)

    res = retranscribe_gap_snippet(fake_model, seg_audio, 0.2, 1.6, str(tmp_path))
    assert len(res) == 1
    passed_input = fake_model.transcribe.call_args.args[0]
    assert isinstance(passed_input, np.ndarray)
    assert passed_input.dtype == np.float32
    # No temporary WAV needed on disk when ndarray succeeds
    assert list(tmp_path.glob("*.wav")) == []
