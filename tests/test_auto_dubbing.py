import os
import wave

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.core.database import Base
from backend.models import Video
from backend.schemas.dubbing import DubbingRequest
from backend.services.dubbing.service import DubbingService
from backend.services.dubbing.tts_adapter import (
    AVAILABLE_VOICES,
    EdgeTTSAdapter,
    OfflineFallbackTTSAdapter,
)


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    return session_factory


def test_voice_options_listing():
    assert len(AVAILABLE_VOICES) >= 2
    voice_ids = [v.id for v in AVAILABLE_VOICES]
    assert "vi-VN-HoaiMyNeural" in voice_ids
    assert "vi-VN-NamMinhNeural" in voice_ids

    hoai_my = next(v for v in AVAILABLE_VOICES if v.id == "vi-VN-HoaiMyNeural")
    assert hoai_my.name == "Hoài My"
    assert hoai_my.gender == "female"
    assert hoai_my.accent == "northern"


@pytest.mark.anyio
async def test_offline_fallback_tts_synthesizes_valid_audio(tmp_path):
    adapter = OfflineFallbackTTSAdapter()
    out_file = str(tmp_path / "test_speech.wav")
    text = "Xin chào các bạn, hôm nay chúng ta sẽ cùng unbox bàn phím cơ không dây mini cực đẹp."

    path, duration = await adapter.synthesize(
        text=text,
        voice_id="vi-VN-HoaiMyNeural",
        speed=1.0,
        pitch=1.0,
        output_path=out_file,
    )

    assert os.path.isfile(path)
    assert duration > 1.0

    # Verify standard WAV headers
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        n_frames = wf.getnframes()
        assert n_frames > 0


@pytest.mark.anyio
async def test_edge_tts_adapter_fallback_on_network_error(tmp_path):
    fallback = OfflineFallbackTTSAdapter()
    adapter = EdgeTTSAdapter(fallback=fallback)
    out_file = str(tmp_path / "test_fallback_switch.mp3")

    # If edge-tts network fails or is mocked, it should gracefully fall back to WAV
    path, duration = await adapter.synthesize(
        text="Thử nghiệm chuyển đổi tự động khi gặp sự cố mạng.",
        voice_id="vi-VN-HoaiMyNeural",
        output_path=out_file,
    )

    assert os.path.isfile(path)
    assert duration > 0


@pytest.mark.anyio
async def test_dubbing_service_flow(memory_db, tmp_path):
    # Seed a video with script analysis
    with memory_db() as db:
        vid = Video(
            id="v-dub-1",
            platform="douyin",
            platform_video_id="dy_dub_1",
            url="https://www.douyin.com/video/12345",
            canonical_url="https://www.douyin.com/video/12345",
            fingerprint="fp_dub_test",
            title="Unbox bàn phím cơ",
            caption="Bàn phím cơ giá rẻ cho học sinh sinh viên",
            search_query="bàn phím cơ",
            search_job_id="job-1",
            script_analysis={
                "remake_script_vi": {
                    "intro": "Đừng mua bàn phím cơ nếu chưa xem video này!",
                    "body": ["Thiết kế nhỏ gọn 68 phím", "Gõ cực êm không ồn"],
                    "cta": "Bấm lưu lại để mua nhé!",
                }
            },
        )
        db.add(vid)
        db.commit()

    tts = OfflineFallbackTTSAdapter()
    service = DubbingService(sessions=memory_db, tts=tts)

    with memory_db() as db:
        req = DubbingRequest(video_id="v-dub-1", voice_id="vi-VN-NamMinhNeural")
        job_out = service.create_job(req, db)
        assert job_out.status in {"pending", "synthesizing", "completed"}
        assert "Đừng mua bàn phím cơ" in job_out.script_text

    # Run processing directly
    await service.process_dubbing(job_out.id)

    with memory_db() as db:
        updated = service.get_job(job_out.id, db)
        assert updated is not None
        assert updated.status == "completed"
        assert updated.duration_seconds is not None
        assert updated.duration_seconds > 0
        assert updated.audio_url is not None


def test_dubbing_service_fails_on_missing_video(memory_db):
    service = DubbingService(sessions=memory_db)
    with memory_db() as db:
        req = DubbingRequest(video_id="non-existent-video")
        with pytest.raises(ValueError, match="không tồn tại"):
            service.create_job(req, db)


def test_dubbing_api_endpoints():
    with TestClient(app) as client:
        # 1. Get voices
        res = client.get("/api/dubbing/voices")
        assert res.status_code == 200
        data = res.json()
        assert "voices" in data
        assert len(data["voices"]) >= 2

        # 2. Get non-existent job
        res_404 = client.get("/api/dubbing/jobs/non-existent-job-id")
        assert res_404.status_code == 404
