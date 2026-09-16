"""Mirror Telegram pipeline status to the shared dashboard."""
import contextvars
import logging
import job_tracker
current_job = contextvars.ContextVar("telegram_dashboard_job", default=None)


def report(text):
    if current_job.get() is None:
        return
    try:
        _report(text)
    except Exception:
        logging.getLogger(__name__).exception("Dashboard progress update failed")


def _report(text):
    clean = str(text).replace("*", "")
    lower = clean.lower()
    if clean.startswith("❌"):
        job_tracker.set_error(job_tracker.get_status().get("video_name", ""), clean)
        return
    # Order matters: Demucs is labelled 2.5 in Telegram but step 2 in dashboard.
    stages = [
        (2, ("demucs", "tách giọng nhân vật")),
        (3.5, ("ocr", "quét vùng phụ đề")),
        (3, ("whisper",)),
        (4, ("đang dịch", "bước 4/6")),
        (5, ("đang lồng tiếng",)),
        (6, ("đang render", "bước 6/6")),
        (1, ("trích xuất âm thanh", "tách âm thanh")),
    ]
    for step, terms in stages:
        if any(term in lower for term in terms):
            job_tracker.update_step(step, clean.split("\n")[0])
            return
