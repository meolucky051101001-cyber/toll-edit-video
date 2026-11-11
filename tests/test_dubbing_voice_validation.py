import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.core.config import settings
from backend.core.database import Base
from backend.models import Video
from backend.models.dubbing_job import DubbingJob
from backend.schemas.dubbing import DEFAULT_VOICE_ID, DubbingRequest
from backend.services.dubbing.service import DubbingService
from backend.services.dubbing.tts_adapter import EdgeTTSAdapter, OfflineFallbackTTSAdapter

BAD_VOICES = ["../../evil", "..\\..\\evil", "en-US-EvilNeural", ""]


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.mark.parametrize("voice_id", BAD_VOICES)
def test_request_schema_rejects_unknown_voice(voice_id):
    with pytest.raises(ValidationError, match="voice_id"):
        DubbingRequest(video_id="v-1", voice_id=voice_id)


@pytest.mark.parametrize("voice_id", BAD_VOICES)
def test_api_rejects_traversal_voice_without_writing_files(voice_id):
    dubbing_dir = settings.dubbing_dir
    before = {p.name for p in dubbing_dir.iterdir()} if dubbing_dir.exists() else set()

    with TestClient(app) as client:
        res = client.post("/api/dubbing/jobs", json={"video_id": "v-1", "voice_id": voice_id})

    assert res.status_code == 422
    after = {p.name for p in dubbing_dir.iterdir()} if dubbing_dir.exists() else set()
    assert after == before


@pytest.mark.anyio
@pytest.mark.parametrize("voice_id", ["../../evil", "en-US-EvilNeural"])
async def test_offline_adapter_rejects_unknown_voice_before_writing(tmp_path, voice_id):
    adapter = OfflineFallbackTTSAdapter()
    with pytest.raises(ValueError, match="voice_id"):
        await adapter.synthesize(
            text="Xin chào",
            voice_id=voice_id,
            output_path=str(tmp_path / "evil.wav"),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_edge_adapter_rejects_unknown_voice_before_writing(tmp_path):
    adapter = EdgeTTSAdapter(allow_fallback=True)
    with pytest.raises(ValueError, match="voice_id"):
        await adapter.synthesize(
            text="Xin chào",
            voice_id="../../evil",
            output_path=str(tmp_path / "evil.mp3"),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_offline_adapter_accepts_catalog_voice(tmp_path):
    adapter = OfflineFallbackTTSAdapter()
    path, duration = await adapter.synthesize(
        text="Xin chào các bạn",
        voice_id=DEFAULT_VOICE_ID,
        output_path=str(tmp_path / "ok.wav"),
    )
    assert Path(path).is_file()
    assert duration > 0
    with wave.open(path, "rb") as wav_file:
        assert wav_file.getnframes() > 0


class RecordingTTS(OfflineFallbackTTSAdapter):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def synthesize(
        self,
        text: str,
        voice_id: str = DEFAULT_VOICE_ID,
        speed: float = 1.0,
        pitch: float = 1.0,
        output_path: str = "",
    ) -> tuple[str, float]:
        self.calls.append((voice_id, output_path))
        return await super().synthesize(text, voice_id, speed, pitch, output_path)


@pytest.mark.anyio
async def test_legacy_db_row_with_unknown_voice_falls_back_to_default(memory_db, tmp_path, monkeypatch):
    dubbing_dir = tmp_path / "dubbing"
    monkeypatch.setattr(settings, "dubbing_dir", dubbing_dir)

    with memory_db() as db:
        db.add(
            Video(
                id="v-legacy-1",
                platform="douyin",
                platform_video_id="dy_legacy_1",
                url="https://www.douyin.com/video/legacy1",
                canonical_url="https://www.douyin.com/video/legacy1",
                fingerprint="fp_legacy",
                title="Legacy voice row",
                search_query="test",
                search_job_id="job-legacy",
            )
        )
        db.add(
            DubbingJob(
                id="job-legacy-1",
                video_id="v-legacy-1",
                status="pending",
                voice_id="../../evil",
                script_text="Xin chào các bạn",
            )
        )
        db.commit()

    tts = RecordingTTS()
    service = DubbingService(sessions=memory_db, tts=tts)
    await service.process_dubbing("job-legacy-1")

    assert len(tts.calls) == 1
    used_voice, used_path = tts.calls[0]
    assert used_voice == DEFAULT_VOICE_ID
    assert Path(used_path).resolve().is_relative_to(dubbing_dir.resolve())

    with memory_db() as db:
        job = db.get(DubbingJob, "job-legacy-1")
        assert job is not None
        assert job.status == "completed"
        assert job.error_message is None

    assert not (tmp_path / "evil.mp3").exists()