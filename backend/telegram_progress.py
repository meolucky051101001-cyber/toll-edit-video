"""Mirror Telegram pipeline status to the shared dashboard."""
import contextvars
import logging
import job_tracker
current_job = contextvars.ContextVar("telegram_dashboard_job", default=None)
current_worker_id = contextvars.ContextVar("telegram_worker_id", default=1)


def report(text, worker_id=None):
    if current_job.get() is None:
        return
    try:
        _report(text, worker_id=worker_id)
    except Exception:
        logging.getLogger(__name__).exception("Dashboard progress update failed")


def _report(text, worker_id=None):
    clean = str(text).replace("*", "")
    lower = clean.lower()
    w_id = worker_id or current_worker_id.get() or 1
    if clean.startswith("❌"):
        job_tracker.set_error(job_tracker.get_status().get("video_name", ""), clean, worker_id=w_id)
        return
    # Bảng phân nhóm 8 bước pipeline chuẩn V1 Orchestrator & Dashboard
    stages = [
        (2, ("bước 2/8", "demucs", "roformer", "bóc tách giọng nói", "tách giọng nhân vật", "bước 2.5/6")),
        (4, ("bước 4/8", "ocr", "quét vị trí phụ đề", "quét vùng phụ đề", "phụ đề chữ cứng", "bước 3.5/6")),
        (3, ("bước 3/8", "whisper", "nhận dạng từ vocal", "nhận dạng giọng nói", "bước 3/6")),
        (5, ("bước 5/8", "đang dịch", "dịch phụ đề", "gemini ai để dịch", "dịch chuẩn ngữ cảnh", "bước 4/6")),
        (6, ("bước 6/8", "đang lồng tiếng", "lồng tiếng ai", "tts", "rvc", "bước 5/6")),
        (7, ("bước 7/8", "trộn nhạc nền", "trộn âm", "hòa âm", "adaptive mixer")),
        (8, ("bước 8/8", "đang render", "render video", "nvenc", "xuất video", "bước 6/6")),
        (1, ("bước 1/8", "trích xuất âm thanh", "tách âm thanh gốc", "bước 1/6", "bước 2/6:")),
    ]
    for step, terms in stages:
        if any(term in lower for term in terms):
            job_tracker.update_step(step, clean.split("\n")[0], worker_id=w_id)
            return
