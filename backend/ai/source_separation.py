"""Source-separation policy for BS-RoFormer with the proven Demucs fallback."""

from __future__ import annotations

import os
import subprocess
import sys

from .model_policy import current_model_policy, ordered_unique
from .model_runtime import ModelRuntimeError, run_model_stage, runtime_module_available


CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def separate_vocals(
    input_audio_path,
    output_dir,
    segment_seconds=None,
    timeout_seconds=300,
):
    """Return ``(vocals, background)`` using BS-RoFormer before Demucs."""

    input_audio_path = os.path.abspath(input_audio_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.exists(input_audio_path):
        raise FileNotFoundError("File audio không tồn tại: {}".format(input_audio_path))
    os.makedirs(output_dir, exist_ok=True)
    if segment_seconds is not None and float(segment_seconds) <= 0:
        raise ValueError("segment_seconds must be positive")

    policy = current_model_policy()
    failures = []

    if policy.separator_backend in {"auto", "roformer"}:
        if runtime_module_available("audio_separator", policy):
            print(
                "Bắt đầu tách giọng bằng BS-RoFormer {}...".format(
                    policy.separator_model
                )
            )
            try:
                result = run_model_stage(
                    "separator",
                    {
                        "input_audio": input_audio_path,
                        "output_directory": os.path.join(output_dir, "bs_roformer"),
                        "model_directory": os.path.join(
                            policy.model_cache_directory, "source-separation"
                        ),
                        "model_filename": policy.separator_model,
                        "use_native_fp16": True,
                    },
                    timeout_seconds=float(timeout_seconds),
                    policy=policy,
                )
                vocals_path = str(result["vocals_path"])
                background_path = str(result["background_path"])
                if os.path.isfile(vocals_path) and os.path.isfile(background_path):
                    print(
                        "BS-RoFormer tách thành công ({}).".format(
                            result.get("effective_precision", "unknown precision")
                        )
                    )
                    return vocals_path, background_path
                raise RuntimeError("BS-RoFormer output is incomplete")
            except (ModelRuntimeError, OSError, KeyError, RuntimeError) as exc:
                failures.append("BS-RoFormer: {}".format(exc))
                print("BS-RoFormer lỗi; chuyển sang Demucs fine-tuned: {}".format(exc))
        else:
            failures.append("BS-RoFormer runtime unavailable")

    venv_python = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "venv",
        "Scripts",
        "python.exe",
    )
    if not os.path.exists(venv_python):
        venv_python = sys.executable
    cpu_jobs = max(1, (os.cpu_count() or 4) - 1)
    import torch

    device_args = (
        ["-d", "cuda"]
        if torch.cuda.is_available()
        else ["-d", "cpu", "-j", str(cpu_jobs)]
    )
    base_name = os.path.splitext(os.path.basename(input_audio_path))[0]
    for model_number, model_name in enumerate(
        ordered_unique(policy.demucs_primary_model, policy.demucs_fallback_model), 1
    ):
        try:
            fast = getattr(policy, "speed_profile", "quality") == "fast"
            print("Đang tách bằng Demucs model {}...".format(model_name))
            command = [
                venv_python,
                "-m",
                "demucs",
                input_audio_path,
                "-n",
                model_name,
                "--two-stems",
                "vocals",
                "--shifts",
                "1" if model_number == 1 and not fast else "0",
                "--overlap",
                "0.25" if model_number == 1 and not fast else "0.1",
                "-o",
                output_dir,
            ]
            if segment_seconds is not None:
                command.extend(["--segment", "{:g}".format(float(segment_seconds))])
            command += device_args
            subprocess.run(
                command,
                check=True,
                timeout=float(timeout_seconds),
                creationflags=CREATE_NO_WINDOW,
            )
            model_output = os.path.join(output_dir, model_name, base_name)
            vocals_path = os.path.join(model_output, "vocals.wav")
            background_path = os.path.join(model_output, "no_vocals.wav")
            if os.path.isfile(vocals_path) and os.path.isfile(background_path):
                print("Demucs {} tách thành công.".format(model_name))
                return vocals_path, background_path
            for root, _directories, files in os.walk(output_dir):
                if "vocals.wav" in files and "no_vocals.wav" in files:
                    return (
                        os.path.join(root, "vocals.wav"),
                        os.path.join(root, "no_vocals.wav"),
                    )
            raise RuntimeError("Demucs output files were not found")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failures.append("Demucs {}: {}".format(model_name, exc))

    raise RuntimeError("Không thể tách giọng: {}".format(" | ".join(failures)))
