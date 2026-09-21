import logging
import math
import os
import struct
import wave
from abc import ABC, abstractmethod

from backend.schemas.dubbing import VoiceOption

logger = logging.getLogger("dubbing")

AVAILABLE_VOICES: list[VoiceOption] = [
    VoiceOption(
        id="vi-VN-HoaiMyNeural",
        name="Hoài My",
        gender="female",
        accent="northern",
        provider="edge-tts",
        description="Nữ miền Bắc truyền cảm, tự nhiên, phù hợp review & vlog",
    ),
    VoiceOption(
        id="vi-VN-NamMinhNeural",
        name="Nam Minh",
        gender="male",
        accent="northern",
        provider="edge-tts",
        description="Nam miền Bắc ấm áp, tin cậy, phù hợp tin tức & hướng dẫn",
    ),
]


class BaseTTSAdapter(ABC):
    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str = "vi-VN-HoaiMyNeural",
        speed: float = 1.0,
        pitch: float = 1.0,
        output_path: str = "",
    ) -> tuple[str, float]:
        """Synthesizes text to speech.

        Returns (output_path, duration_seconds).
        """
        pass


class OfflineFallbackTTSAdapter(BaseTTSAdapter):
    """Generates synthetic audio locally without requiring internet or cloud credentials."""

    async def synthesize(
        self,
        text: str,
        voice_id: str = "vi-VN-HoaiMyNeural",
        speed: float = 1.0,
        pitch: float = 1.0,
        output_path: str = "",
    ) -> tuple[str, float]:
        words = len(text.split())
        # Estimate ~2.5 words per second scaled by speed
        duration = max(1.0, round((words / (2.5 * max(speed, 0.5))), 2))

        if not output_path:
            output_path = "data/dubbing/sample_fallback.wav"

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        sample_rate = 24000
        num_samples = int(sample_rate * duration)
        freq = 440.0 * max(pitch, 0.5)

        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for i in range(num_samples):
                # Gentle chime fade in/out to avoid popping
                envelope = min(1.0, i / 2400.0) * min(1.0, (num_samples - i) / 2400.0)
                val = int(8000.0 * envelope * math.sin(2.0 * math.pi * freq * (i / sample_rate)))
                frames.extend(struct.pack("<h", val))
            wav_file.writeframes(frames)

        logger.info("Generated offline TTS audio: %s (duration: %.2fs)", output_path, duration)
        return output_path, duration


class TTSUnavailableError(Exception):
    """Raised when the neural TTS service is unavailable and fallback is not allowed."""


class EdgeTTSAdapter(BaseTTSAdapter):
    """Microsoft Edge Neural TTS synthesis adapter."""

    def __init__(self, fallback: BaseTTSAdapter | None = None, allow_fallback: bool = False):
        self.fallback = fallback or OfflineFallbackTTSAdapter()
        self.allow_fallback = allow_fallback

    async def synthesize(
        self,
        text: str,
        voice_id: str = "vi-VN-HoaiMyNeural",
        speed: float = 1.0,
        pitch: float = 1.0,
        output_path: str = "",
    ) -> tuple[str, float]:
        if not output_path:
            output_path = f"data/dubbing/speech_{voice_id}.mp3"
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        try:
            import edge_tts

            # Format rate & pitch adjustments e.g. "+10%", "-5Hz"
            rate_pct = int((speed - 1.0) * 100)
            rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
            pitch_hz = int((pitch - 1.0) * 50)
            pitch_str = f"+{pitch_hz}Hz" if pitch_hz >= 0 else f"{pitch_hz}Hz"

            communicate = edge_tts.Communicate(text, voice_id, rate=rate_str, pitch=pitch_str)
            await communicate.save(output_path)

            file_size = os.path.getsize(output_path)
            # Rough MP3 48kbps estimation if header parsing not present
            duration = max(1.0, round(file_size / (48000 / 8), 2))
            logger.info("Synthesized neural TTS: %s (%d bytes, ~%.1fs)", output_path, file_size, duration)
            return output_path, duration
        except Exception as exc:
            if self.allow_fallback:
                logger.warning("edge-tts synthesis failed (%s); switching to offline fallback", exc)
                fallback_path = os.path.splitext(output_path)[0] + ".wav"
                return await self.fallback.synthesize(text, voice_id, speed, pitch, fallback_path)
            logger.error("edge-tts synthesis failed: %s", exc)
            raise TTSUnavailableError(f"Dịch vụ Microsoft Neural TTS không khả dụng: {exc}") from exc


def get_tts_adapter(use_fallback: bool = False, allow_fallback: bool = False) -> BaseTTSAdapter:
    if use_fallback:
        return OfflineFallbackTTSAdapter()
    return EdgeTTSAdapter(allow_fallback=allow_fallback)
