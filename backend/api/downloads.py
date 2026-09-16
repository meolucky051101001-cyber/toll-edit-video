
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from backend.core.database import SessionLocal
from backend.models.download_job import DownloadJob
from backend.schemas.downloads import BatchDownloadRequest, DownloadJobOut

router = APIRouter(tags=["downloads"])


@router.post(
    "/api/downloads/batch",
    response_model=list[DownloadJobOut],
    status_code=202,
    summary="Yêu cầu tải hàng loạt video",
)
async def request_batch_download(payload: BatchDownloadRequest, request: Request):
    service = getattr(request.app.state, "download", None)
    if not service:
        raise HTTPException(503, "Dịch vụ tải chưa được khởi tạo.")
    return service.request_batch_downloads(payload.video_ids, force=payload.force)


@router.post(
    "/api/videos/{video_id}/downloads",
    response_model=DownloadJobOut,
    status_code=202,
    summary="Yêu cầu tải video",
)
async def request_video_download(video_id: str, request: Request, force: bool = False):
    service = getattr(request.app.state, "download", None)
    if not service:
        raise HTTPException(503, "Dịch vụ tải chưa được khởi tạo.")
    try:
        return service.request_download(video_id, force=force)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get(
    "/api/videos/{video_id}/downloads/active",
    response_model=DownloadJobOut | None,
    summary="Lấy tiến trình tải hiện tại của video",
)
def get_video_active_download(video_id: str, request: Request):
    service = getattr(request.app.state, "download", None)
    if not service:
        raise HTTPException(503, "Dịch vụ tải chưa được khởi tạo.")
    return service.get_active_job_for_video(video_id)


@router.get(
    "/api/downloads/{download_id}",
    response_model=DownloadJobOut,
    summary="Kiểm tra trạng thái tiến trình tải",
)
def get_download_status(download_id: str, request: Request):
    service = getattr(request.app.state, "download", None)
    if not service:
        raise HTTPException(503, "Dịch vụ tải chưa được khởi tạo.")
    job = service.get_job_by_id(download_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy tiến trình tải này.")
    return job


@router.post(
    "/api/downloads/{download_id}/cancel",
    response_model=DownloadJobOut,
    summary="Hủy tiến trình tải",
)
async def cancel_download(download_id: str, request: Request):
    service = getattr(request.app.state, "download", None)
    if not service:
        raise HTTPException(503, "Dịch vụ tải chưa được khởi tạo.")
    try:
        return service.cancel_download(download_id)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@router.get(
    "/api/downloads/{download_id}/file",
    summary="Lưu tập tin video hoàn tất về máy",
)
def download_file(download_id: str, request: Request):
    service = getattr(request.app.state, "download", None)
    if not service:
        raise HTTPException(503, "Dịch vụ tải chưa được khởi tạo.")

    with SessionLocal() as db:
        job = db.get(DownloadJob, download_id)
        if not job:
            raise HTTPException(404, "Không tìm thấy tiến trình tải này.")
        if job.status != "completed" or not job.relative_path:
            raise HTTPException(400, "Tập tin video chưa sẵn sàng hoặc tiến trình tải chưa hoàn tất.")

        base_dir = service.download_dir.resolve()
        target_path = (service.download_dir / job.relative_path).resolve()

        # Path traversal guard
        try:
            target_path.relative_to(base_dir)
        except ValueError:
            raise HTTPException(403, "Đường dẫn tập tin không an toàn.")

        if not target_path.is_file():
            raise HTTPException(404, "Tập tin không tồn tại trên hệ thống lưu trữ.")

        return FileResponse(
            path=str(target_path),
            filename=job.relative_path,
            media_type="video/mp4",
        )
