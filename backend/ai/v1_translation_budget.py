"""Bound caller wait across providers; at most one outstanding network fallback."""
import contextvars
import functools
import time
from .v1_bounded_fallback import run_fallback

deadline = contextvars.ContextVar('v1_translation_deadline', default=None)


def remaining():
    end = deadline.get()
    seconds = 180 if end is None else end - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('Đã hết ngân sách 180 giây cho bước dịch; chưa chấp nhận bản dịch thiếu.')
    return seconds


def translation_budget(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        token = deadline.set(deadline.get() or (time.monotonic() + 180))
        segments = args[0] if args else kwargs.get('srt_segments', [])
        original = [(s, s.content, hasattr(s, 'orig_content'), getattr(s, 'orig_content', None)) for s in segments]
        try:
            return fn(*args, **kwargs)
        except BaseException:
            for segment, content, had_original, old_original in original:
                segment.content = content
                if had_original:
                    segment.orig_content = old_original
                elif hasattr(segment, 'orig_content'):
                    delattr(segment, 'orig_content')
            raise
        finally:
            deadline.reset(token)
    return wrapped


def bounded_call(fn, *args, **kwargs):
    import re
    import logging
    model = (kwargs.get('json') or {}).get('model')
    if not model and args and isinstance(args[0], str):
        match = re.search(r'/models/([\w.-]+):', args[0])
        model = match.group(1) if match else None
    model = model or type(getattr(fn, '__self__', None)).__name__
    def event(outcome):
        from v1_resource_control import current_video_id
        logging.getLogger('ai.translation').info('Model=%s | kết quả=%s | video_id=%s', model, outcome, current_video_id())
        try:
            import job_tracker
            job_tracker.record_translation_attempt(model, outcome)
        except Exception:
            pass
    event('calling')
    try:
        value = run_fallback(functools.partial(fn, *args, **kwargs), timeout=min(40, remaining()))
        status = getattr(value, 'status_code', None)
        outcome = ('authentication' if status in (401, 403) else 'rate_limit' if status == 429
                   else 'temporary_service' if status in (500, 502, 503, 504)
                   else 'model_unavailable' if status == 404 else 'response')
        event(outcome)
        return value
    except Exception as error:
        event('timeout' if isinstance(error, TimeoutError) else type(error).__name__)
        raise


def valid_translation(value, count, target_lang='vi'):
    import re
    return (isinstance(value, list) and len(value) == count
            and all(isinstance(item, str) and item.strip()
                    and not (target_lang.lower().startswith('vi') and re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', item))
                    for item in value))
