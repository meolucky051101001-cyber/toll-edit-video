"""Pure job-path and completion-message helpers for the Telegram adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TelegramJobPaths:
    """All stable paths for one Telegram video, derived in one place."""

    base_name: str
    job_directory: Path
    original_audio: Path
    original_srt: Path
    translated_srt: Path
    dubbing_directory: Path
    mixed_audio: Path
    final_video: Path
    delivery_copy: Path

    @classmethod
    def create(cls, workspace: str, output_directory: str, base_name: str):
        job_directory = Path(workspace).resolve() / base_name
        output_root = Path(output_directory).resolve()
        return cls(
            base_name=base_name,
            job_directory=job_directory,
            original_audio=job_directory / "original.wav",
            original_srt=job_directory / "original.srt",
            translated_srt=job_directory / "translated.srt",
            dubbing_directory=job_directory / "dubbing",
            mixed_audio=job_directory / "mixed.wav",
            final_video=job_directory / "final_{}.mp4".format(base_name),
            delivery_copy=output_root / "Dubbed_{}.mp4".format(base_name),
        )

    def prepare_directories(self) -> None:
        self.job_directory.mkdir(parents=True, exist_ok=True)
        self.delivery_copy.parent.mkdir(parents=True, exist_ok=True)


def format_elapsed_time(elapsed_seconds: float) -> str:
    elapsed = max(0, int(elapsed_seconds))
    minutes, seconds = divmod(elapsed, 60)
    return (
        "{} phút {} giây".format(minutes, seconds)
        if minutes
        else "{} giây".format(seconds)
    )


def build_v2_completion_caption(
    title: str,
    output_directory: str,
    elapsed_seconds: float,
    remaining_jobs: int,
) -> str:
    queue_status = (
        "\n⏳ Phía sau còn {} video đang chờ xử lý...".format(remaining_jobs)
        if remaining_jobs > 0
        else "\n🎉 Đã hoàn tất toàn bộ hàng đợi!"
    )
    return (
        "✅ *Video đã lồng tiếng Tiếng Việt (Pipeline v2 - Âm thanh Studio)!*\n\n"
        "🎬 Video: `{}`\n"
        "💾 Đã tự động lưu vào máy: `{}`\n"
        "⏱️ Thời gian xử lý: {}{}"
    ).format(
        title,
        str(Path(output_directory).resolve()),
        format_elapsed_time(elapsed_seconds),
        queue_status,
    )
