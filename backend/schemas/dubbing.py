from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


class VoiceOption(BaseModel):
    id: str
    name: str
    gender: str = "female"
    accent: str = "northern"
    provider: str = "edge-tts"
    description: str = ""


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

VOICE_IDS = frozenset(voice.id for voice in AVAILABLE_VOICES)
DEFAULT_VOICE_ID = "vi-VN-HoaiMyNeural"


class DubbingRequest(BaseModel):
    video_id: str
    script_text: str | None = None
    voice_id: str = DEFAULT_VOICE_ID
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=1.5)
    bgm_volume_percent: int = Field(default=15, ge=0, le=100)
    ducking: bool = True

    @field_validator("voice_id")
    @classmethod
    def voice_must_be_known(cls, value: str) -> str:
        # voice_id is interpolated into on-disk audio paths; only catalog ids
        # may reach the filesystem.
        if value not in VOICE_IDS:
            raise ValueError("voice_id không nằm trong danh sách giọng hỗ trợ")
        return value


class DubbingJobOut(BaseModel):
    id: str
    video_id: str
    status: str = Field(description="pending, synthesizing, mixing, completed, failed")
    voice_id: str
    script_text: str
    audio_url: str | None = None
    video_url: str | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None


class DubbingVoicesResponse(BaseModel):
    voices: list[VoiceOption]
