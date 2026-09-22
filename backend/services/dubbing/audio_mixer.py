import asyncio
import logging
import os
import shutil

from backend.core.config import ROOT

logger = logging.getLogger("dubbing.mixer")


def resolve_ffmpeg_binary(ffmpeg_bin: str | None = None) -> str | None:
    """Resolves FFmpeg binary, prioritizing local portable bin directory."""
    if ffmpeg_bin and os.path.isfile(ffmpeg_bin):
        return ffmpeg_bin
    local_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local_bin = ROOT / "bin" / local_name
    if local_bin.is_file() and os.access(local_bin, os.X_OK | os.R_OK):
        return str(local_bin)
    return shutil.which("ffmpeg")


class AudioMixer:
    """Muxes voiceover audio with video or creates an independent audio asset."""

    def __init__(self, ffmpeg_bin: str | None = None):
        self.ffmpeg = resolve_ffmpeg_binary(ffmpeg_bin)

    async def mix(
        self,
        voiceover_audio_path: str,
        source_video_path: str | None = None,
        output_dir: str = "data/dubbing",
        bgm_volume_percent: int = 15,
        ducking: bool = True,
    ) -> tuple[str, str | None]:
        """Mixes voiceover with video if possible.

        Returns (audio_url_or_path, video_url_or_path).
        """
        os.makedirs(output_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(voiceover_audio_path))[0]

        # If source video does not exist or ffmpeg is not found, return audio asset only
        if not source_video_path or not os.path.isfile(source_video_path) or not self.ffmpeg:
            logger.info("Source video or ffmpeg not present; producing audio-only asset: %s", voiceover_audio_path)
            return voiceover_audio_path, None

        output_video_path = os.path.join(output_dir, f"{base_name}_dubbed.mp4")

        try:
            # Construct ffmpeg filter for audio ducking
            bgm_gain = max(0.01, min(1.0, bgm_volume_percent / 100.0))

            if ducking:
                # Dynamic sidechain ducking: voiceover [1:a] ducks source audio [0:a]
                filter_complex = (
                    f"[0:a]volume=1.0[bgm_full];"
                    f"[bgm_full][1:a]sidechaincompress=threshold=0.12:ratio=4:attack=15:release=250[bgm_ducked];"
                    f"[bgm_ducked]volume={max(0.2, bgm_gain * 2.0)}[bgm_final];"
                    f"[bgm_final][1:a]amix=inputs=2:duration=longest:dropout_transition=2[aout]"
                )
            else:
                filter_complex = "[1:a]volume=1.0[aout]"

            cmd = [
                self.ffmpeg,
                "-y",
                "-i", source_video_path,
                "-i", voiceover_audio_path,
                "-filter_complex", filter_complex,
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                output_video_path,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0 and "matches no streams" in stderr.decode(errors="ignore"):
                logger.info("Source video has no audio stream; falling back to direct mux without ducking filter")
                cmd_fallback = [
                    self.ffmpeg,
                    "-y",
                    "-i", source_video_path,
                    "-i", voiceover_audio_path,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    output_video_path,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd_fallback,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

            if proc.returncode == 0 and os.path.isfile(output_video_path):
                logger.info("Successfully mixed dubbed video: %s", output_video_path)
                return voiceover_audio_path, output_video_path
            else:
                logger.warning("ffmpeg mix failed (code %d): %s; returning audio only", proc.returncode, stderr.decode(errors="ignore"))
                return voiceover_audio_path, None
        except Exception as exc:
            logger.warning("Audio mixing exception: %s; returning audio only", exc)
            return voiceover_audio_path, None
