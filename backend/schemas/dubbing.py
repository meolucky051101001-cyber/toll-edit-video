from datetime import datetime, timezone

from pydantic import BaseModel, Field


class VoiceOption(BaseModel):
    id: str
    name: str
    gender: str = "female"
    accent: str = "northern"
    provider: str = "edge-tts"
    description: str = ""


class DubbingRequest(BaseModel):
    video_id: str
    script_text: str | None = None
    voice_id: str = "vi-VN-HoaiMyNeural"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=1.5)
    bgm_volume_percent: int = Field(default=15, ge=0, le=100)
    ducking: bool = True


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
