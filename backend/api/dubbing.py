import os

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.core.database import SessionLocal, get_db
from backend.core.events import event_bus
from backend.schemas.dubbing import DubbingJobOut, DubbingRequest, DubbingVoicesResponse
from backend.services.dubbing import DubbingService

router = APIRouter(prefix="/api/dubbing", tags=["dubbing"])

# Shared service instance with event bus
dubbing_service = DubbingService(sessions=SessionLocal, event_bus=event_bus)


@router.get("/voices", response_model=DubbingVoicesResponse)
def get_voices():
    """Returns available Vietnamese and multilingual neural TTS voices."""
    return DubbingVoicesResponse(voices=dubbing_service.list_voices())


@router.post("/jobs", response_model=DubbingJobOut, status_code=status.HTTP_201_CREATED)
async def create_dubbing_job(payload: DubbingRequest, db: Session = Depends(get_db)):
    """Creates a new automated dubbing job for a video and schedules async execution."""
    try:
        return await dubbing_service.create_job_async(payload, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/jobs/{job_id}", response_model=DubbingJobOut)
def get_dubbing_job(job_id: str, db: Session = Depends(get_db)):
    """Retrieves the status and media outputs of a dubbing job."""
    job = dubbing_service.get_job(job_id, db)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy tác vụ lồng tiếng.")
    return job


@router.get("/files/{filename}")
def stream_dubbing_file(filename: str):
    """Streams generated dubbing audio or video files."""
    # Prevent path traversal attacks
    clean_filename = os.path.basename(filename)
    file_path = os.path.join("data", "dubbing", clean_filename)

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tệp âm thanh/video không tồn tại.")

    media_type = "audio/mpeg" if clean_filename.endswith(".mp3") else "audio/wav" if clean_filename.endswith(".wav") else "video/mp4"
    return FileResponse(file_path, media_type=media_type, filename=clean_filename)
